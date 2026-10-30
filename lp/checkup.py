# -*- coding: utf-8 -*-
"""
系统体检 + 启动项管理（借鉴微软电脑管家的「一键体检 / 启动项管理」形态）

设计约束（跟整个项目一致）：
  · 零第三方依赖：只用 winreg / os / ctypes / xml
  · 快：注册表枚举 0.1ms 级，体检整轮控制在 100ms 内（进程采样可选 0.6s）
  · 可逆：禁用启动项 = 改名（不删数据），随时能一键恢复
  · 静默安全：HKLM / 系统计划任务需要管理员，没权限时不报错、只标"需要管理员"

禁用/启用的实现方式
  注册表 Run 值   -> 值名加前缀 lppa_disabled_（数据原样保留）
  「启动」文件夹  -> 文件名加后缀 .lppa_disabled（系统不再把它当快捷方式）
  计划任务        -> 任务文件名加后缀 .lppa_disabled（需管理员）
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import time
import winreg

DISABLE_PREFIX = "lppa_disabled_"
DISABLE_SUFFIX = ".lppa_disabled"
SELF_TAGS = ("laptoppower", "笔记本电源自适应")

# 顺序即 id 里的索引，不要随意调整（已保存的开关记录会跟着变）
RUN_KEYS = (
    ("HKCU\\Run", winreg.HKEY_CURRENT_USER,
     r"Software\Microsoft\Windows\CurrentVersion\Run", False),
    ("HKLM\\Run", winreg.HKEY_LOCAL_MACHINE,
     r"Software\Microsoft\Windows\CurrentVersion\Run", True),
    ("HKLM\\Run(32)", winreg.HKEY_LOCAL_MACHINE,
     r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", True),
)


# ------------------------------------------------------------------ 基础读取
def _reg_values(hive, path: str):
    """枚举某个键下所有值；打不开返回 None"""
    out = []
    try:
        k = winreg.OpenKey(hive, path)
    except Exception:
        return None
    try:
        i = 0
        while i < 200:
            try:
                n, v, _t = winreg.EnumValue(k, i)
            except OSError:
                break
            out.append((n, v))
            i += 1
    except Exception:
        pass
    try:
        winreg.CloseKey(k)
    except Exception:
        pass
    return out


def _startup_dirs():
    appdata = os.environ.get("APPDATA") or ""
    pd = os.environ.get("PROGRAMDATA") or ""
    return (
        ("启动(用户)", os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup"), False),
        ("启动(所有用户)", os.path.join(pd, r"Microsoft\Windows\Start Menu\Programs\Startup"), True),
    )


def _tasks_dir():
    w = os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(w, "System32", "Tasks")


def _task_info(path: str):
    """读计划任务 XML：是否启用 + 命令行。

    这里刻意用文本搜索而不是 XML 解析：任务文件是 UTF-16，逐个 ET 解析要
    250ms 级（36 个任务），而体检是点击即时反馈，直接找标签只要几毫秒。"""
    enabled, cmd = True, ""
    try:
        with open(path, "rb") as f:
            txt = f.read(131072).decode("utf-16", "replace")
    except Exception:
        return enabled, cmd
    try:
        if re.search(r"<Enabled>\s*false\s*</Enabled>", txt, re.I):
            enabled = False
        m = re.search(r"<Command>(.*?)</Command>", txt, re.S | re.I)
        if m:
            cmd = m.group(1).strip()
    except Exception:
        pass
    return enabled, cmd


# ------------------------------------------------------------------ 启动项清单
def startup_items() -> list:
    """返回统一结构：id / name / cmd / where / enabled / kind / need_admin"""
    items = []

    for idx, (label, hive, path, admin) in enumerate(RUN_KEYS):
        for name, data in (_reg_values(hive, path) or []):
            off = name.startswith(DISABLE_PREFIX)
            items.append({
                "id": "reg:%d:%s" % (idx, name),
                "name": name[len(DISABLE_PREFIX):] if off else name,
                "cmd": str(data),
                "where": label,
                "enabled": not off,
                "kind": "reg",
                "need_admin": admin,
            })

    for label, d, admin in _startup_dirs():
        try:
            for fn in os.listdir(d):
                if fn.lower() == "desktop.ini":
                    continue
                off = fn.endswith(DISABLE_SUFFIX)
                items.append({
                    "id": "dir:%s" % os.path.join(d, fn),
                    "name": fn[:-len(DISABLE_SUFFIX)] if off else fn,
                    "cmd": os.path.join(d, fn),
                    "where": label,
                    "enabled": not off,
                    "kind": "dir",
                    "need_admin": admin,
                })
        except Exception:
            continue

    td = _tasks_dir()
    try:
        for fn in sorted(os.listdir(td)):
            if fn.lower() in ("desktop.ini",) or fn.startswith("."):
                continue
            full = os.path.join(td, fn)
            if not os.path.isfile(full):
                continue
            off = fn.endswith(DISABLE_SUFFIX)
            en, cmd = (False, "") if off else _task_info(full)
            items.append({
                "id": "task:%s" % full,
                "name": fn[:-len(DISABLE_SUFFIX)] if off else fn,
                "cmd": cmd,
                "where": "计划任务",
                "enabled": (not off) and en,
                "kind": "task",
                "need_admin": True,
            })
    except Exception:
        pass

    # 标出本程序自己的启动项，免得用户误关了还以为程序坏了
    for it in items:
        blob = (it["name"] + " " + it["cmd"]).lower()
        it["self"] = any(t in blob for t in SELF_TAGS)

    # 禁用的排后面，同级按来源+名字
    items.sort(key=lambda x: (not x["self"], not x["enabled"], x["where"], x["name"].lower()))
    return items


# ------------------------------------------------------------------ 启停操作
def _set_reg(idx: int, name: str, on: bool):
    label, hive, path, _admin = RUN_KEYS[idx]
    off = name.startswith(DISABLE_PREFIX)
    target = name[len(DISABLE_PREFIX):] if off else name
    new = target if on else DISABLE_PREFIX + target
    if (off and not on) or ((not off) and on):  # 已经是目标状态
        return True, "already"
    try:
        k = winreg.OpenKey(hive, path, 0, winreg.KEY_ALL_ACCESS)
    except Exception as e:
        # 判权限只认 winerror。旧写法 `"5" in str(e)` 是子串匹配：错误信息里
        # 任何位置出现一个 5（路径、句柄号、中文编码）都会被误报成「需要管理员」。
        return False, ("需要管理员" if getattr(e, "winerror", 0) == 5 else str(e))
    try:
        try:
            v, t = winreg.QueryValueEx(k, name)
        except Exception:
            return False, "该项已不存在"
        winreg.SetValueEx(k, new, 0, t, v)
        winreg.DeleteValue(k, name)
        return True, "ok"
    except Exception as e:
        return False, ("需要管理员" if getattr(e, "winerror", 0) == 5 else str(e))
    finally:
        try:
            winreg.CloseKey(k)
        except Exception:
            pass


def _set_file(path: str, on: bool):
    """文件型（快捷方式 / 计划任务）启停：加/去后缀"""
    if not os.path.exists(path):
        return False, "文件已不存在"          # 别对空气报成功
    base = os.path.dirname(path)
    fn = os.path.basename(path)
    off = fn.endswith(DISABLE_SUFFIX)
    target = fn[:-len(DISABLE_SUFFIX)] if off else fn
    new = target if on else target + DISABLE_SUFFIX
    dst = os.path.join(base, new)
    if os.path.abspath(dst) == os.path.abspath(path):
        return True, "already"
    try:
        os.rename(path, dst)
        return True, "ok"
    except Exception as e:
        return False, ("需要管理员" if getattr(e, "winerror", 0) in (5, 13) else str(e))


def _new_id(sid: str, on: bool) -> str:
    """启停之后这条记录的新 id。

    改名式启停会改变 id 本身（注册表值名加/去前缀、文件加/去后缀）。
    前端若拿旧 id 再点一次，白名单校验会判它「已变化，请刷新」——
    在界面上看就是「点了没反应」。所以把新 id 一并返回，让面板就地更新。
    """
    try:
        kind, rest = sid.split(":", 1)
        if kind == "reg":
            idx_s, name = rest.split(":", 1)
            off = name.startswith(DISABLE_PREFIX)
            base = name[len(DISABLE_PREFIX):] if off else name
            return "reg:%s:%s" % (idx_s, base if on else DISABLE_PREFIX + base)
        if kind in ("dir", "task"):
            off = rest.endswith(DISABLE_SUFFIX)
            base = rest[:-len(DISABLE_SUFFIX)] if off else rest
            return "%s:%s" % (kind, base if on else base + DISABLE_SUFFIX)
    except Exception:
        pass
    return sid


def set_startup(sid: str, on: bool) -> dict:
    """启用/禁用一条启动项。返回 {ok, msg, id, enabled, name}

    id/enabled 是**操作后**的取值（失败时原样返回旧值），面板用它们就地
    更新按钮，不必整表重拉 —— 重拉会在用户连点第二个开关时把列表整个换掉。
    """
    try:
        kind, rest = sid.split(":", 1)
    except Exception:
        return {"ok": False, "msg": "非法 id", "id": sid, "enabled": None, "name": ""}
    if kind not in ("reg", "dir", "task"):
        return {"ok": False, "msg": "未知类型", "id": sid, "enabled": None, "name": ""}
    # 必须与**当前扫描结果**对上才动手。
    #
    # 这条接口吃的是任意字符串，而 kind=dir/task 时 rest 就是绝对路径，
    # `_set_file` 会直接给它改名 —— 等于暴露一个「改任意文件名」的原语。
    # HTTP 服务虽然只绑 127.0.0.1，但网页可以跨站 POST 过来（见 webui 的
    # Host/Origin 校验），两条防线都要有。顺带解决「拿着上一次快照里的
    # 旧 id 反复点」（禁用后 id 会带前缀变化）导致的误操作。
    try:
        known = {it["id"]: it for it in startup_items()}
    except Exception:
        known = {}
    item = known.get(sid)
    if item is None:
        return {"ok": False, "msg": "该项已变化，请刷新列表后重试",
                "id": sid, "enabled": None, "name": ""}
    try:
        if kind == "reg":
            idx_s, name = rest.split(":", 1)
            ok, msg = _set_reg(int(idx_s), name, on)
        else:
            ok, msg = _set_file(rest, on)
    except Exception as e:
        return {"ok": False, "msg": str(e), "id": sid,
                "enabled": item.get("enabled"), "name": item.get("name", "")}
    return {"ok": ok, "msg": msg,
            "id": _new_id(sid, on) if ok else sid,
            # 失败时如实返回原状态：前端若照着 on 翻转，界面会跟注册表对不上
            "enabled": bool(on) if ok else item.get("enabled"),
            "name": item.get("name", "")}


# ------------------------------------------------------------------ 体检项
def _mem_info():
    class MEMSTAT(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPage", ctypes.c_ulonglong), ("ullAvailPage", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtended", ctypes.c_ulonglong)]
    try:
        st = MEMSTAT()
        st.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return {"load": st.dwMemoryLoad,
                "total_gb": round(st.ullTotalPhys / 1073741824, 1),
                "free_gb": round(st.ullAvailPhys / 1073741824, 1)}
    except Exception:
        return {}


def _disk_free():
    try:
        u = shutil.disk_usage("C:\\")
        return {"free_gb": round(u.free / 1073741824, 1), "total_gb": round(u.total / 1073741824, 1)}
    except Exception:
        return {}


def run_checks(mgr=None, deep: bool = False) -> dict:
    """跑一轮体检。deep=True 时多做一次 0.6s 的进程采样（找后台吃电的程序）"""
    t0 = time.perf_counter()
    items = []

    def add(key, title, value, ok, advice="", fix=None):
        items.append({"key": key, "title": title, "value": value,
                      "ok": ok, "advice": advice})
        if fix:
            items[-1]["fix"] = fix

    # 1) 启动项
    st = startup_items()
    n_on = sum(1 for x in st if x["enabled"])
    # ok=None 只用在「确实没什么可说」的情形。旧实现不管几项都记 None，
    # 于是这一项永远停在「中性」：分数被固定扣 3 分、摘要里的「待优化」
    # 又只数 False 项 ⇒ 面板上既不算好也不算坏，还不给建议。
    add("startup", "开机启动项", "%d 项启用 / 共 %d 项" % (n_on, len(st)),
        n_on < 8,
        "" if n_on < 8 else "启动项偏多，拖慢开机、常驻后台也耗电，可在下面关掉不常用的")

    # 2) 内存 / 磁盘
    mem = _mem_info()
    if mem:
        add("mem", "内存", "%.1f / %.1f GB（占用 %d%%）" % (mem["free_gb"], mem["total_gb"], mem["load"]),
            mem["load"] < 85, "" if mem["load"] < 85 else "内存吃紧，关掉不用的程序会明显更跟手")
    dk = _disk_free()
    if dk:
        add("disk", "系统盘 C:", "剩余 %.1f / %.1f GB" % (dk["free_gb"], dk["total_gb"]),
            dk["free_gb"] > 15, "" if dk["free_gb"] > 15 else "剩余空间不足，休眠/更新会失败，建议清理")

    # 3) 游戏相关三键
    #    读数走 hw.gaming_states() 的扁平口径（None = 键缺失 = 系统默认）。
    #    旧代码拿 gaming_env() 的返回值 .get("hags")，而那个 dict 只有
    #    items/hints —— HAGS 和游戏模式恒显「关」被扣分、录制恒显「关」
    #    直接通过，全是假读数。
    try:
        from . import hw
        gs = hw.gaming_states() or {}
        hags = gs.get("hags")
        if hags is None:
            add("hags", "硬件加速 GPU 调度", "系统默认", None,
                "Win11 24H2 起支持的显卡默认开启，一般无需更改")
        else:
            add("hags", "硬件加速 GPU 调度",
                "开" if hags else "关", bool(hags),
                "" if hags else "开着能降低游戏延迟（需重启生效）",
                fix=None if hags else "hags")
        gm = gs.get("game_mode")
        if gm is None:
            add("game_mode", "Windows 游戏模式", "系统默认", None,
                "未手动改过；Win11 默认开启")
        else:
            add("game_mode", "Windows 游戏模式",
                "开" if gm else "关", bool(gm),
                "" if gm else "开着系统会优先把资源给游戏",
                fix=None if gm else "game_mode")
        dvr = gs.get("game_dvr")
        # 只有「显式打开」才算开；键缺失 = 默认就是关的
        add("dvr", "后台游戏录制",
            "开" if dvr else "关", not dvr,
            "" if not dvr else "后台录制会一直占 GPU，平时建议关掉",
            fix="game_dvr" if dvr else None)
        cl = hw.asus_charge_limit() or {}
        if cl.get("percent") is not None:
            add("charge_limit", "充电阈值", "%d%%" % cl["percent"], True,
                "华硕养护模式，插电常驻时建议 60~80%")
    except Exception:
        pass

    # 4) 程序自身状态（需要 manager）
    #    每一项**单独**兜异常：旧实现整段一个 try，任何一项抛异常都会让后面
    #    所有项永久消失（「电池健康」就因此从不出现、体检分数跟着虚高）。
    #    单点故障不该连坐 —— 这是本项目反复踩到的一类 bug。
    if mgr is not None:
        try:
            s = mgr.status() or {}
        except Exception:
            s = {}

        def _item(fn):
            try:
                title, value, ok, advice = fn()
            except Exception:
                return                 # 这一项算不出来就跳过，不影响其它项
            add(fn.__name__.lstrip("_"), title, value, ok, advice)

        def plan():
            return ("当前电源计划", str(s.get("scheme_name") or s.get("scheme") or "—"),
                    True, "")

        def proc_max():
            # status() 没有顶层 proc_max 键，它在 knobs.proc_max.value 里。
            pm = ((s.get("knobs") or {}).get("proc_max") or {}).get("value")
            if pm is None:
                raise ValueError("no proc_max")     # 拿不到就不显示（_item 会跳过）
            pmv = int(float(pm))
            return ("CPU 最大状态", "%s%%" % pmv, pmv >= 100,
                    "" if pmv >= 100 else "正在限频（省电档/PD 弱电源时的正常现象）")

        def gpu_eco():
            g = s.get("gpu_eco")
            eco = g.get("eco") if isinstance(g, dict) else None
            if eco is None:
                raise ValueError("no eco")
            return ("独显状态", "已断电（Eco）" if eco else "正常", None,
                    "离电时断电可以省 3~5W")

        def battery():
            # status()["battery"] 是**整数电量**，不是字典 —— 对它调 .get 必然抛
            # AttributeError。健康度在 battery_info 里。
            hp = (s.get("battery_info") or {}).get("health_pct")
            if not hp:
                raise ValueError("no health")
            hp = float(hp)
            return ("电池健康", "%.1f%%" % hp, hp >= 75,
                    "" if hp >= 75 else "健康度偏低，续航会明显变短")

        for fn in (plan, proc_max, gpu_eco, battery):
            _item(fn)

    # 5) 后台吃电进程（可选，0.6s 采样）
    top = []
    if deep:
        try:
            from . import hw
            top = hw.top_cpu_procs(window=0.6, top_n=5) or []
        except Exception:
            top = []

    bad = [x for x in items if x["ok"] is False]
    return {
        "items": items,
        "startups": st,
        "top_procs": top,
        "score": max(0, 100 - 12 * len(bad) - 3 * sum(1 for x in items if x["ok"] is None)),
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def summary_line(res: dict) -> str:
    bad = [x for x in res.get("items", []) if x["ok"] is False]
    n_on = sum(1 for x in res.get("startups", []) if x["enabled"])
    return "体检 %d 分 · %d 项待优化 · 启动项 %d" % (
        res.get("score", 0), len(bad), n_on)


# ------------------------------------------------------------------ 一键优化
# 可修复项的白名单。「一键优化」只碰这三个系统设置，写什么值在下面写死，
# 不接受任何来自前端的数值参数 —— 这条接口跟 startup_set 一样暴露在
# 本地 HTTP 上，参数越少越安全。
# 写入目标放模块级 dict：测试把条目替换成 HKCU 临时键即可验证读写闭环，
# 不用真改用户的系统设置。
FIX_KEYS = ("hags", "game_mode", "game_dvr")

FIX_TARGETS = {
    # key: (hive, path, name, value, 成功文案)
    "hags": (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers",
             "HwSchMode", 2, "已开启，重启后生效"),
    "game_mode": (winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\GameBar",
                  "AutoGameModeEnabled", 1, "已开启"),
    # 关后台录制：主开关 + 旧键名一起写，两处都可能被读（见 hw._GAME_ALT）
    "game_dvr": (winreg.HKEY_CURRENT_USER,
                 r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
                 "AppCaptureEnabled", 0, "已关闭"),
}


def _reg_write(hive, path: str, name: str, value: int):
    try:
        k = winreg.CreateKey(hive, path)      # 键不存在就创建（GameBar 常见）
    except Exception as e:
        return False, ("需要管理员" if getattr(e, "winerror", 0) == 5 else str(e))
    try:
        winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(value))
        return True, "ok"
    except Exception as e:
        return False, ("需要管理员" if getattr(e, "winerror", 0) == 5 else str(e))
    finally:
        try:
            winreg.CloseKey(k)
        except Exception:
            pass


def apply_fix(key: str) -> dict:
    """把某个体检项调到推荐状态（一键优化的一步）。返回 {ok, msg}

    hags 在 HKLM，普通权限写不进去 —— 如实报「需要管理员」，
    让用户自己去设置里开，而不是假装成功。
    """
    if key not in FIX_KEYS:
        return {"ok": False, "msg": "不支持的项目"}
    hive, path, name, value, okmsg = FIX_TARGETS[key]
    ok, msg = _reg_write(hive, path, name, value)
    if key == "game_dvr" and ok:
        # 旧键名一并关掉：HW._GAME_ALT 里 HistoricalCaptureEnabled 任一为真
        # 都会被当成开着，只写主键可能白写
        _reg_write(hive, path, "HistoricalCaptureEnabled", 0)
    return {"ok": ok, "msg": okmsg if ok else msg, "key": key}
