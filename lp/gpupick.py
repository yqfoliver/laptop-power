# -*- coding: utf-8 -*-
"""核显优先调度（GPU Pick）——核显跑得动的程序，就不必唤醒独显

=============================================================================
一、问题
=============================================================================
Optimus 机型插电时，独显即使空载也要吃几瓦；一旦被某个 3D 程序唤醒，
整机功耗立刻 +20~50 W，风扇狂转、机身发烫。但很多程序（英雄联盟、Dota2、
CS2、原神、星露谷……）核显就能跑满帧，唤醒独显纯属浪费。

二、通道（用户态、普通权限、零依赖）
=============================================================================
Windows 自带的「设置 → 系统 → 屏幕 → 显示卡 → 图形首选项」，本质是
    HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences
    值名 = exe 完整路径（REG_SZ）
    数据 = "GpuPreference=1;"  节能（核显）
           "GpuPreference=2;"  高性能（独显）
           "GpuPreference=0;"  让 Windows 决定
该偏好**优先于厂商控制面板**（NVIDIA / AMD 自己的程序设置），且只影响
下次启动该程序（已在运行的不受影响）——这点必须在界面上说清楚。

本机 2026-10-10 实测：华硕自用程序（ArmouryCrate 等）已经在往这个键里写
GpuPreference=1，说明通道在本机是活的。

三、判定依据（实证优先，不猜）
=============================================================================
1) 内置分级名单：按「游戏本身的图形强度」分 tier，而不是按本机核显。
   tier 1 极轻（2D/像素/老游戏，任何核显都够）
   tier 2 轻量电竞（需中端以上核显：780M / 890M / Iris Xe / Arc）
   tier 3 中等（需高性能核显 + FSR，低画质可玩）
   本机核显能力档位由 igpu_tier 决定，名单里 tier <= igpu_tier 才走核显。
2) 手动试探（本模块核心）：把程序临时设成核显，用 PDH 的
   「GPU Engine → Utilization Percentage」按 pid 采样它的 3D 利用率：
   吃不满 = 核显有余力 = 可固化；长期吃满 = 吃力 = 退回独显。
3) 用户手动钉住：优先级最高，压过一切自动判定。

四、安全底线
=============================================================================
· 改注册表前**先保存原值**，还原时写回原值而不是直接删（华硕/用户可能
  本来就有设置，删掉是把别人的东西弄丢）。
· 名单外的程序**一律不干预**（不写任何值），不做自动试探 —— 拿用户正在玩
  的游戏赌帧率是越界行为。
· 曾经的「手动试探」（临时设核显 + 采样 45 秒再固化）已于 2026-10-31 整体
  退役：它和手动的「用核显 / 用独显」在界面上一模一样，用户分不清差别。
  历史数据里 source 为 "probe" 的条目仍然照常生效（classify 保留该分支）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

REG_PATH = r"Software\Microsoft\DirectX\UserGpuPreferences"

PREF_IGPU = 1      # 节能 / 核显
PREF_DGPU = 2      # 高性能 / 独显
PREF_AUTO = 0      # 让 Windows 决定

# ---------------------------------------------------------------------------
# 内置名单：进程名（小写） → (tier, 中文名)
# 数据口径：Radeon 890M 1080p 实测（chipversus / minipclab，2026）
#   LOL 314fps / Dota2 226 / CS2 低画质 126 / 原神 60 / 守望先锋 115
# ---------------------------------------------------------------------------
IGPU_OK: Dict[str, Tuple[int, str]] = {
    # ---- tier 1：极轻，任何核显都够（2D / 像素 / 老游戏 / 独立小品）
    "stardew valley.exe": (1, "星露谷物语"),
    "terraria.exe": (1, "泰拉瑞亚"),
    "hollow_knight.exe": (1, "空洞骑士"),
    "slaythespire.exe": (1, "杀戮尖塔"),
    "vampiresurvivors.exe": (1, "吸血鬼幸存者"),
    "undertale.exe": (1, "Undertale"),
    "celeste.exe": (1, "蔚蓝"),
    "to the moon.exe": (1, "去月球"),
    "projectzomboid.exe": (1, "僵尸毁灭工程"),
    "projectzomboid64.exe": (1, "僵尸毁灭工程"),
    "among us.exe": (1, "Among Us"),
    "brotato.exe": (1, "土豆兄弟"),
    "deadcells.exe": (1, "死亡细胞"),
    "dave the diver.exe": (1, "潜水员戴夫"),
    "balatro.exe": (1, "小丑牌"),
    "civilizationv.exe": (1, "文明 5"),
    "ageofempires2.exe": (1, "帝国时代 2"),
    " Plants vs Zombies.exe".strip().lower(): (1, "植物大战僵尸"),
    # ---- tier 2：轻量电竞 / 网游，需中端以上核显（780M / 890M / Iris Xe / Arc）
    "league of legends.exe": (2, "英雄联盟"),
    "lol.exe": (2, "英雄联盟"),
    "dota2.exe": (2, "Dota 2"),
    "cs2.exe": (2, "CS2"),
    "valorant-win64-shipping.exe": (2, "无畏契约"),
    "yuanshen.exe": (2, "原神"),
    "genshinimpact.exe": (2, "原神（国际服）"),
    "overwatch.exe": (2, "守望先锋"),
    "rocketleague.exe": (2, "火箭联盟"),
    "wow.exe": (2, "魔兽世界"),
    "wowclassic.exe": (2, "魔兽世界怀旧服"),
    "civilizationvi.exe": (2, "文明 6"),
    "eurotrucks2.exe": (2, "欧洲卡车模拟 2"),
    "pathofexile.exe": (2, "流放之路"),
    "pathofexile_x64.exe": (2, "流放之路"),
    "pathofexilesteam.exe": (2, "流放之路"),
    "phasmophobia.exe": (2, "恐鬼症"),
    "starrail.exe": (2, "崩坏：星穹铁道"),
    "minecraft.exe": (2, "Minecraft（基岩版）"),
    # ---- tier 3：中等，需高性能核显 + FSR / 低画质
    "fortniteclient-win64-shipping.exe": (3, "堡垒之夜"),
    "r5apex.exe": (3, "Apex 英雄"),
    "tslgame.exe": (3, "绝地求生"),
    "gta5.exe": (3, "GTA 5"),
    "witcher3.exe": (3, "巫师 3"),
    "forzahorizon5.exe": (3, "极限竞速：地平线 5"),
    "ittakestwo.exe": (3, "双人成行"),
    "hogwartslegacy.exe": (3, "霍格沃茨之遗"),
    "narakabladepoint.exe": (3, "永劫无间"),
}

# 必须独显（核显任何档位都不建议）
DGPU_ONLY: Dict[str, str] = {
    "cyberpunk2077.exe": "赛博朋克 2077",
    "b1.exe": "黑神话：悟空",
    "eldenring.exe": "艾尔登法环",
    "rdr2.exe": "荒野大镖客 2",
    "bg3.exe": "博德之门 3",
    "bg3_dx11.exe": "博德之门 3",
    "starfield.exe": "星空",
    "flightsimulator.exe": "微软模拟飞行",
    "atomicheart.exe": "原子之心",
    "alanwake2.exe": "心灵杀手 2",
    "monsterhunterwilds.exe": "怪物猎人：荒野",
    "diablo iv.exe": "暗黑破坏神 4",
    "cod.exe": "使命召唤",
    "modernwarfare.exe": "使命召唤：现代战争",
    "wuthering waves.exe": "鸣潮",
    "wutheringwaves.exe": "鸣潮",
    "re4.exe": "生化危机 4 重制版",
    "sekiro.exe": "只狼",
    "darksoulsiii.exe": "黑暗之魂 3",
}


def _data_path() -> str:
    if getattr(sys, "frozen", False):
        root = os.path.dirname(os.path.abspath(sys.executable))
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "gpupick.json")


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def pick_target(fg: Optional[dict], last_fg: Optional[dict]) -> dict:
    """面板上的「钉住 / 试探」该作用在哪个程序上。

    用户点按钮时前台窗口**就是本程序的面板**，而 hw.foreground() 按设计会
    把自家人排除（返回空进程名）—— 直接拿前台用会得到空名字，按钮点了没
    反应，看起来像失效（2026-10-10 实测）。真实意图是"我刚刚在用的那个
    程序"，所以退到 last_fg（最近一次非本程序的前台窗口）并标记 stale，
    让界面如实说明操作对象是谁。
    """
    fg = dict(fg or {})
    if fg.get("process"):
        return {"process": fg.get("process", "") or "",
                "path": fg.get("path", "") or "",
                "pid": int(fg.get("pid") or 0),
                "stale": False}
    lf = dict(last_fg or {})
    if lf.get("process"):
        return {"process": lf.get("process", "") or "",
                "path": lf.get("path", "") or "",
                "pid": int(lf.get("pid") or 0),
                "stale": True}
    return {"process": "", "path": "", "pid": 0, "stale": False}


class GpuPick:
    """核显优先调度器。注册表与 PDH 均可注入（测试用假通道）。"""

    def __init__(self, reg=None, path=None) -> None:
        self.reg = reg                    # 假注册表（测试注入）
        self.enabled = True               # config: gpupick_enabled
        self.igpu_tier = 2                # config: gpupick_igpu_tier（本机核显档位）
        self.path = path or _data_path()
        self.data: dict = {"version": 1, "apps": {}, "orig": {}}
        self._last_note = ""
        self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data = {
                    "version": 1,
                    "apps": d.get("apps") or {},
                    "orig": d.get("orig") or {},
                }
        except Exception:
            pass

    def _save(self) -> None:
        try:
            _atomic_write(self.path,
                          json.dumps(self.data, ensure_ascii=False, indent=2))
        except Exception:
            pass

    def reload_cfg(self, cfg: dict) -> None:
        self.enabled = bool(cfg.get("gpupick_enabled", True))
        try:
            self.igpu_tier = int(cfg.get("gpupick_igpu_tier", 2))
        except Exception:
            self.igpu_tier = 2
        self.igpu_tier = max(1, min(3, self.igpu_tier))

    # ------------------------------------------------------------ 注册表
    def _key(self, write: bool = False):
        if self.reg is not None:
            return self.reg
        import winreg
        if write:
            return winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, REG_PATH, 0,
                winreg.KEY_READ | winreg.KEY_WRITE)
        # 只读路径用 OpenKey：CreateKeyEx 会在「读一个值」时顺手把整个键建出来，
        # 而这是用户自己都没配过的 DirectX 偏好键 —— 读操作不该有写副作用。
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ)

    def reg_get(self, exe_path: str) -> Optional[str]:
        if self.reg is not None:          # 测试通道：假注册表就是一个 dict
            return self.reg.get(exe_path)
        try:
            import winreg
            k = self._key(False)
            return winreg.QueryValueEx(k, exe_path)[0]
        except Exception:
            return None

    def reg_set(self, exe_path: str, pref: int) -> bool:
        """写偏好。**只在第一次动这个键时**记录原值（用于还原）。"""
        if not exe_path:
            return False
        try:
            cur = self.reg_get(exe_path)
            if exe_path not in self.data["orig"]:
                self.data["orig"][exe_path] = cur      # None = 原本不存在
            want = "GpuPreference=%d;" % int(pref)
            if cur == want:
                return True
            k = self._key(True)
            if self.reg is not None:
                k[exe_path] = want
            else:
                import winreg
                winreg.SetValueEx(k, exe_path, 0, winreg.REG_SZ, want)
            self._save()
            return True
        except Exception as e:
            self._last_note = "写注册表失败：%r" % e
            return False

    def reg_restore(self, exe_path: str) -> bool:
        """还原成我们介入前的值；原本没有就删除（绝不留下半截）。"""
        if not exe_path:
            return False
        orig = self.data["orig"].get(exe_path)
        try:
            k = self._key(True)
            if orig is None:
                if self.reg is not None:
                    k.pop(exe_path, None)
                else:
                    import winreg
                    try:
                        winreg.DeleteValue(k, exe_path)
                    except FileNotFoundError:
                        pass
            else:
                if self.reg is not None:
                    k[exe_path] = orig
                else:
                    import winreg
                    winreg.SetValueEx(k, exe_path, 0, winreg.REG_SZ, orig)
            # 写成功后才抹掉「介入前的原值」：失败就留着，下次还能还原
            self.data["orig"].pop(exe_path, None)
            self._save()
            return True
        except Exception as e:
            self._last_note = "还原失败：%r" % e
            return False

    def restore_all(self) -> int:
        n = 0
        for p in list(self.data["orig"].keys()):
            if self.reg_restore(p):
                n += 1
        return n

    # ------------------------------------------------------------ 判定
    @staticmethod
    def norm(name: str) -> str:
        return (name or "").strip().lower()

    def classify(self, exe_name: str) -> Tuple[Optional[str], str, str, int]:
        """返回 (mode, source, note, tier)。

        mode: 'igpu' / 'dgpu' / None（不干预）
        source: 'user' / 'probe' / 'builtin' / ''
        """
        n = self.norm(exe_name)
        if not n:
            return None, "", "", 0
        app = (self.data["apps"].get(n) or {})

        # 1) 用户手动钉住：最高优先级
        if app.get("mode") in ("igpu", "dgpu") and app.get("src") == "user":
            return app["mode"], "user", "你手动指定", 0

        # 2) 试探结论
        if app.get("mode") in ("igpu", "dgpu") and app.get("src") == "probe":
            util = app.get("util")
            note = "实测核显占用 %s" % (("%.0f%%" % util) if isinstance(util, (int, float)) else "—")
            return app["mode"], "probe", note, 0

        # 3) 必须独显名单
        if n in DGPU_ONLY:
            return "dgpu", "builtin", DGPU_ONLY[n], 0

        # 4) 核显可胜任名单（按本机核显档位过滤）
        if n in IGPU_OK:
            tier, cn = IGPU_OK[n]
            if tier <= self.igpu_tier:
                return "igpu", "builtin", cn, tier
            return None, "", "%s：核显档位不足（需 tier %d）" % (cn, tier), tier

        return None, "", "", 0

    def set_user(self, exe_name: str, mode: Optional[str]) -> None:
        """手动钉住 / 取消钉住。exe_name 用进程名作键，应用时按当前路径写入。"""
        n = self.norm(exe_name)
        if not n:
            return
        if mode in ("igpu", "dgpu"):
            self.data["apps"][n] = {
                "mode": mode, "src": "user",
                "ts": time.time(),
            }
        else:
            self.data["apps"].pop(n, None)
        self._save()

    # ------------------------------------------------------------ 应用
    def apply_for(self, exe_path: str, exe_name: str) -> Optional[str]:
        """对某个程序应用判定，返回实际写入的 pref（None=未干预）。"""
        if not self.enabled or not exe_path:
            return None
        mode, _src, _note, _tier = self.classify(exe_name)
        if mode == "igpu":
            self.reg_set(exe_path, PREF_IGPU)
            return "igpu"
        if mode == "dgpu":
            # 只在「我们之前改过」或「用户明确钉住」时才写 2，
            # 避免把用户原本没设置的程序也钉死在独显上
            known = exe_path in self.data["orig"]
            if known or (self.data["apps"].get(self.norm(exe_name)) or {}).get("src") == "user":
                self.reg_set(exe_path, PREF_DGPU)
                return "dgpu"
            return None
        return None

    # ------------------------------------------------------------ 状态
    def report(self) -> dict:
        # list() 快照：巡检线程可能在 apply_for 里插入新条目，直接遍历
        # dict.values() 会让面板线程偶发 RuntimeError（界面表现为「调度未启用」）
        apps = list(self.data["apps"].values())
        n_igpu = sum(1 for v in apps if isinstance(v, dict) and v.get("mode") == "igpu")
        return {
            "enabled": self.enabled,
            "igpu_tier": self.igpu_tier,
            "managed": len(self.data["orig"]),
            "n_igpu": n_igpu,
            "note": self._last_note,
        }


# ---------------------------------------------------------------------------
# 本机核显档位探测（用于给 igpu_tier 一个合理初值）
# ---------------------------------------------------------------------------
def detect_igpu_tier() -> int:
    """按核显型号猜一个初始档位：3=高性能核显，2=中端，1=入门。"""
    try:
        import subprocess
        # 必须 CREATE_NO_WINDOW：GUI 进程起子进程会新建控制台窗口（黑窗一闪）
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController).Name"],
            capture_output=True, text=True, encoding="utf-8",
            errors="ignore", timeout=20, creationflags=flags).stdout.lower()
    except Exception:
        return 2
    strong = ("radeon(tm) 8", "radeon 8", "radeon(tm) 7", "radeon 7",
              "arc(", "iris xe", "arc graphics")
    mid = ("vega 7", "vega 8", "vega7", "vega8", "radeon graphics",
           "uhd graphics 7", "uhd 7")
    if any(s in out for s in strong):
        return 3
    if any(s in out for s in mid):
        return 2
    return 1
