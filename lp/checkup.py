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
        return False, "需要管理员" if "5" in str(e) or "拒绝" in str(e) else str(e)
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


def set_startup(sid: str, on: bool) -> dict:
    """启用/禁用一条启动项。返回 {ok, msg}"""
    try:
        kind, rest = sid.split(":", 1)
    except Exception:
        return {"ok": False, "msg": "非法 id"}
    try:
        if kind == "reg":
            idx_s, name = rest.split(":", 1)
            ok, msg = _set_reg(int(idx_s), name, on)
        elif kind in ("dir", "task"):
            ok, msg = _set_file(rest, on)
        else:
            return {"ok": False, "msg": "未知类型"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    return {"ok": ok, "msg": msg}


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

    def add(key, title, value, ok, advice=""):
        items.append({"key": key, "title": title, "value": value,
                      "ok": ok, "advice": advice})

    # 1) 启动项
    st = startup_items()
    n_on = sum(1 for x in st if x["enabled"])
    add("startup", "开机启动项", "%d 项启用 / 共 %d 项" % (n_on, len(st)),
        None if n_on else True,
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
    try:
        from . import hw
        ge = hw.gaming_env() or {}
        add("hags", "硬件加速 GPU 调度",
            "开" if ge.get("hags") else "关", bool(ge.get("hags")),
            "" if ge.get("hags") else "开着能降低游戏延迟（需重启生效）")
        add("game_mode", "Windows 游戏模式",
            "开" if ge.get("game_mode") else "关", bool(ge.get("game_mode")),
            "" if ge.get("game_mode") else "开着系统会优先把资源给游戏")
        add("dvr", "后台游戏录制",
            "关" if not ge.get("dvr") else "开", not ge.get("dvr"),
            "" if not ge.get("dvr") else "后台录制会一直占 GPU，平时建议关掉")
        cl = hw.asus_charge_limit() or {}
        if cl.get("percent") is not None:
            add("charge_limit", "充电阈值", "%d%%" % cl["percent"], True,
                "华硕养护模式，插电常驻时建议 60~80%")
    except Exception:
        pass

    # 4) 程序自身状态（需要 manager）
    if mgr is not None:
        try:
            s = mgr.status() or {}
            add("plan", "当前电源计划", str(s.get("scheme_name") or s.get("scheme") or "—"),
                True, "")
            # status() 没有顶层 proc_max 键，它在 knobs.proc_max.value 里。
            # 照旧写 s.get("proc_max") 恒为 None，这一项就永远不显示。
            pm = ((s.get("knobs") or {}).get("proc_max") or {}).get("value")
            if pm is not None:
                add("proc_max", "CPU 最大状态", "%s%%" % pm,
                    None if int(pm) < 100 else True,
                    "" if int(pm) >= 100 else "正在限频（省电档/PD 弱电源时的正常现象）")
            eco = (s.get("gpu_eco") or {}).get("eco") if isinstance(s.get("gpu_eco"), dict) else None
            if eco is not None:
                add("gpu_eco", "独显状态", "已断电（Eco）" if eco else "正常",
                    None, "离电时断电可以省 3~5W")
            # status()["battery"] 是**整数电量百分比**，不是字典 —— 对它调
            # .get("health") 必然抛 AttributeError，被下面的 except 吞掉后
            # 「电池健康」这一项从不出现（体检分数也跟着虚高）。
            bt = s.get("battery_info") or {}
            hp = bt.get("health_pct")
            if hp:
                hp = float(hp)
                add("battery", "电池健康", "%.1f%%" % hp, hp >= 75,
                    "" if hp >= 75 else "健康度偏低，续航会明显变短")
        except Exception:
            pass

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
