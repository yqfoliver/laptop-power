# -*- coding: utf-8 -*-
"""硬件自适应档案（hwprofile）——**让默认值不再是某一台机器的标定值**

=============================================================================
一、为什么要这个模块
=============================================================================
程序的早期版本是在一台开发机上调出来的，于是 config 的默认值里留下了大量
那台机器的实测数字：独显功耗上限 70 W、散热包络 110 W、CPU 温度上限 96 ℃、
离电降刷 60 Hz、Type-C 供电 89 W……

这些数字在那台机器上是**对的**，换一台机器就可能是**错的**，而且错得很隐蔽：
最典型的一次事故是「独显上限写大了」——判据 `已到顶就停止让渡` 变成 97 W，
而那台机器的独显实际峰值只有 68.8 W，于是分配器一直压 CPU 让瓦，帧数不涨、
CPU 性能先掉，日志里还一切正常。

结论：**标定值不能随程序分发，只能在目标机器上现测。**

=============================================================================
二、怎么做的
=============================================================================
1) 启动时**只读探测**一次（约 1 秒，任何一项失败都静默降级到通用兜底）：
   · 独显：有没有、叫什么、NVML 能报出的功耗上限约束与温度 slowdown 阈值
   · 核显：型号 → 能力档位（决定哪些游戏可以放心交给核显）
   · 屏幕：当前刷新率与**该分辨率下真实存在的刷新率列表**（绝不硬写面板
     不支持的频率，否则黑屏）
   · ATKACPI 厂商通道是否可用（不可用就关掉 GPU Eco，别每轮白试）
   · CPU 核心数（用于功耗读数的合理性钳制）
   结果落在 `hwprofile.json`（运行时数据，不入库）。

2) 长期运行**持续学习**：游戏档实测到的独显功耗峰值记下来，作为
   「让渡停止点」的真值。这样即使探测阶段拿不到 TGP，跑一局游戏也能自愈。

3) 用户手设值**永远优先**：探测只填 `None`（自动位），绝不覆盖用户填过的数。

=============================================================================
三、通用兜底的原则
=============================================================================
宁小勿大、宁保守勿激进：
  · 独显上限写小 → 早一点停止让渡，少赚几帧，不丢性能
  · 独显上限写大 → 永远判不到顶，一直白压 CPU（真出过的事故）
所以兜底取 60 W，并把「往上修」交给实测学习。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, List, Optional

PROFILE_VERSION = 1

# ---------------------------------------------------------------------------
# 通用兜底：任何机器上都成立的保守值（探测失败时用）
# ---------------------------------------------------------------------------
FALLBACK = {
    "gpu_tgp_max_w": 60.0,      # 独显功耗上限：宁可早停让渡，也不白压 CPU
    "gpu_temp_limit_c": 87.0,   # 独显温度上限（常见 slowdown 阈值 90~91 ℃ 之下）
    "power_envelope_w": 100.0,  # 整机 CPU+GPU 持续包络（仅展示用）
}

# 独显 / 核显的名称特征（Win32_VideoController.Name，大小写不敏感）
DGPU_HINTS = ("nvidia", "geforce", "rtx", "gtx", "quadro",
              "radeon rx", "rx 6", "rx 7", "rx 9", "radeon(tm) rx",
              "arc(tm) a", "arc a", "arc b")
IGPU_HINTS = ("iris", "uhd graphics", "hd graphics", "radeon graphics",
              "radeon(tm) graphics", "vega", "radeon(tm) 6", "radeon(tm) 7",
              "radeon(tm) 8", "radeon 6", "radeon 7", "radeon 8")

# 学习值：独显功耗峰值（W）的合理区间，超出即视为传感器坏值。
# 下限取 20 W 是刻意的：独显空载也有几瓦到十几瓦，若把空载值当成"峰值"，
# 到顶判据会低到离谱（刚进游戏就判定"已到顶"，让渡直接停摆）。
PEAK_LO_W, PEAK_HI_W = 20.0, 400.0
# 峰值每天衰减一点点，给「换驱动 / 换电源 / 夏天」留出回退空间
PEAK_DECAY_PER_DAY = 0.02


def _data_path() -> str:
    if getattr(sys, "frozen", False):
        root = os.path.dirname(os.path.abspath(sys.executable))
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "hwprofile.json")


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _is_dgpu(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in DGPU_HINTS)


def _is_igpu(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in IGPU_HINTS)


def video_controllers() -> List[str]:
    """列出显示适配器名称（WMI）。失败返回空列表，由调用方降级。"""
    try:
        import subprocess
        # CREATE_NO_WINDOW 是必须的：本程序是 --noconsole 的 GUI 进程，
        # 起子进程时系统会**新建控制台窗口**，黑窗一闪而过 —— 违反「完全静默」。
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController).Name"],
            capture_output=True, text=True, encoding="utf-8",
            errors="ignore", timeout=20, creationflags=flags).stdout or ""
    except Exception:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def pick_lower_hz(available: List[int], current: Optional[int]) -> Optional[int]:
    """从真实存在的刷新率里挑一个「比当前低的最高档」。

    · 只挑已经在 available 里的，绝不自造频率（面板不支持 = 黑屏）
    · 没有更低的档就返回 None（= 这台机器没有降刷空间，别白折腾）
    """
    try:
        opts = sorted({int(h) for h in (available or []) if int(h) > 0})
    except Exception:
        return None
    if not opts:
        return None
    try:
        cur = int(current or 0)
    except Exception:
        cur = 0
    lower = [h for h in opts if h < cur] if cur else []
    if not lower:
        return None
    return max(lower)


def probe(nvml=None, display=None, atkacpi_ok=None,
          gpu_names: Optional[List[str]] = None) -> dict:
    """只读探测，返回硬件档案 dict。任何一项失败都静默降级，不抛异常。"""
    p: Dict = {
        "version": PROFILE_VERSION,
        "ts": time.time(),
        "cpu_cores": os.cpu_count() or 4,
        "gpu_names": [],
        "dgpu_name": "",
        "igpu_name": "",
        "has_dgpu": False,
        "nvml_tgp_max_w": None,
        "nvml_gpu_temp_slowdown_c": None,
        "current_hz": None,
        "available_hz": [],
        "atkacpi_ok": False,
        "sources": [],           # 每一项值是从哪来的，便于排查
    }

    # ---- 显示适配器名称（NVML 优先，WMI 兜底）
    names: List[str] = []
    if gpu_names:
        names = list(gpu_names)
    else:
        # NVML 只报 NVIDIA 独显，核显要从 WMI 补 —— 两者都要，缺了核显就
        # 判断不了「这台机器能不能把某些游戏交给核显」。
        try:
            if nvml is not None and getattr(nvml, "ready", False):
                names += [g.name for g in getattr(nvml, "gpus", [])]
        except Exception:
            pass
        names += video_controllers()
    seen = set()
    p["gpu_names"] = [n for n in names
                      if n and not (n.lower() in seen or seen.add(n.lower()))]

    dgpu = [n for n in names if _is_dgpu(n)]
    igpu = [n for n in names if _is_igpu(n)]
    if dgpu:
        p["has_dgpu"] = True
        p["dgpu_name"] = dgpu[0]
        p["sources"].append("dgpu=名称匹配")
    if igpu:
        p["igpu_name"] = igpu[0]

    # ---- 独显功耗上限约束 / 温度阈值（NVML，笔记本上常 NOT_SUPPORTED）
    try:
        if nvml is not None and getattr(nvml, "ready", False):
            lo, hi = nvml.power_limit_range()
            if hi:
                hi = float(hi)
                if hi > 1000.0:            # 有的实现直接给 mW，统一成 W
                    hi /= 1000.0
                p["nvml_tgp_max_w"] = round(hi, 1)
                p["sources"].append("tgp上限=NVML约束")
            t = nvml.temp_threshold(1)          # 1 = THRESHOLD_SLOWDOWN
            if t:
                p["nvml_gpu_temp_slowdown_c"] = int(t)
                p["sources"].append("独显温度线=NVML slowdown")
    except Exception:
        pass

    # ---- 屏幕刷新率（display_probed 标记：区分「这台机器没有多档」和
    #      「上次探测时压根没传显示通道」，后者必须重测，否则会永远缺这一项）
    p["display_probed"] = display is not None
    try:
        if display is not None:
            p["current_hz"] = display.current_hz()
            p["available_hz"] = list(display.available_hz_list() or [])
            if p["available_hz"]:
                p["sources"].append("刷新率=EnumDisplaySettings")
    except Exception:
        pass

    # ---- 厂商通道（华硕 ATKACPI）：不可用就不必每轮去试
    try:
        if atkacpi_ok is None:
            from . import atkacpi
            p["atkacpi_ok"] = bool(atkacpi.get())
        else:
            p["atkacpi_ok"] = bool(atkacpi_ok)
    except Exception:
        p["atkacpi_ok"] = False

    return p


class HwProfile:
    """硬件档案 + 运行时学习。注册表/硬件通道全部可注入（测试用假通道）。"""

    SAVE_EVERY = 60.0        # 学习值落盘节流

    def __init__(self, path: Optional[str] = None, prof: Optional[dict] = None):
        self.path = path or _data_path()
        self.data: dict = {
            "version": PROFILE_VERSION,
            "profile": prof or {},
            "learned": {},
        }
        self._last_save = 0.0
        self._dirty = False
        self.last_filled: List[str] = []     # 最近一次 apply 填了哪些键（供日志）
        self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data = {
                    "version": PROFILE_VERSION,
                    "profile": d.get("profile") or {},
                    "learned": d.get("learned") or {},
                }
        except Exception:
            pass

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        try:
            _atomic_write(self.path,
                          json.dumps(self.data, ensure_ascii=False, indent=2))
            self._dirty = False
            self._last_save = time.time()
        except Exception:
            pass

    # ------------------------------------------------------------ 探测
    @property
    def profile(self) -> dict:
        return self.data.get("profile") or {}

    def refresh(self, nvml=None, display=None, atkacpi_ok=None) -> dict:
        p = probe(nvml=nvml, display=display, atkacpi_ok=atkacpi_ok)
        self.data["profile"] = p
        self._dirty = True
        self.save()
        return p

    def stale(self, max_age_s: float = 86400.0 * 7) -> bool:
        """档案缺失 / 版本变了 / 太旧 —— 都需要重新探测一次"""
        p = self.profile
        if not p or p.get("version") != PROFILE_VERSION:
            return True
        # 上次探测不完整（例如走 --hw 时没传显示通道）：重测，别让残缺档案
        # 因为"没过期"一直用下去
        if p.get("display_probed") is False:
            return True
        try:
            return (time.time() - float(p.get("ts") or 0)) > max_age_s
        except Exception:
            return True

    # ------------------------------------------------------------ 学习
    @property
    def gpu_peak_w(self) -> Optional[float]:
        v = (self.data.get("learned") or {}).get("gpu_peak_w")
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    def learn_gpu_peak(self, w) -> None:
        """喂入一个独显功耗采样：只升不降 + 每日微衰减。

        只升不降是为了「峰值」语义；微衰减是为了让换驱动 / 换电源适配器 /
        季节温差导致的旧峰值不至于把新值永久压死。
        """
        try:
            w = float(w)
        except Exception:
            return
        if w != w or w < PEAK_LO_W or w > PEAK_HI_W:      # NaN / 坏值
            return
        now = time.time()
        cur = self.gpu_peak_w
        if cur is not None:
            days = max(0.0, (now - float((self.data["learned"].get("ts") or now))) / 86400.0)
            if days > 1.0:
                cur = cur * ((1.0 - PEAK_DECAY_PER_DAY) ** days)
        if cur is None or w > cur:
            self.data["learned"]["gpu_peak_w"] = round(w, 1)
            self.data["learned"]["ts"] = now
            self._dirty = True
        if self._dirty and (now - self._last_save) >= self.SAVE_EVERY:
            self.save()

    def seed_from_csv(self, path: Optional[str] = None) -> Optional[float]:
        """首次建档时，用本机已有的功耗采样日志回填独显峰值。

        跑过游戏的机器上 `power_alloc.csv` 里就躺着真值，用它起步比 60 W
        兜底贴近得多 —— 也避免「升级后第一局游戏从零学」这段退步期。
        读不到就返回 None，一切照旧。
        """
        path = path or os.path.join(os.path.dirname(self.path) or ".",
                                    "power_alloc.csv")
        best = None
        try:
            import csv
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for row in csv.DictReader(f):
                    try:
                        v = float(row.get("gpu_w"))
                    except Exception:
                        continue
                    if PEAK_LO_W <= v <= PEAK_HI_W:
                        best = v if best is None else max(best, v)
        except Exception:
            return None
        if best is None:
            return None
        self.data.setdefault("learned", {})["gpu_peak_w"] = round(best, 1)
        self.data["learned"]["ts"] = time.time()
        self.data["learned"]["seed"] = "power_alloc.csv"
        self._dirty = True
        self.save()
        return round(best, 1)

    # ------------------------------------------------------------ 取值
    def gpu_tgp_max(self) -> float:
        """让渡停止点用的独显功耗上限（W）。

        优先级：实测学到的峰值 × 1.03 > 通用兜底 60 W。

        **刻意不用 NVML 的 nvmlDeviceGetPowerManagementLimitConstraints**：
        那是厂商留给降压/超频的「可设上限」，通常比真实 TGP 高一大截
        （某台开发机上它报 100 W，而 228 条真实游戏采样的峰值只有 68.8 W）。
        拿它当到顶判据，就会重演「判据 97 W 永远达不到 → 一直白压 CPU」
        那次事故。所以宁可从保守的 60 W 起步，靠实测峰值把它抬上去 ——
        抬升发生在**同一局游戏内**，第一局就能自愈。
        """
        peak = self.gpu_peak_w
        if peak:
            return round(peak * 1.03, 1)
        return FALLBACK["gpu_tgp_max_w"]

    def gpu_temp_limit(self) -> float:
        """独显温度上限（℃）：NVML slowdown 阈值下压 4 ℃ 留余量"""
        t = self.profile.get("nvml_gpu_temp_slowdown_c")
        if t:
            return round(float(t) - 4.0, 1)
        return FALLBACK["gpu_temp_limit_c"]

    def dc_refresh_hz(self) -> Optional[int]:
        """离电降刷目标：真实存在的、比当前低的最高档；没有就 None（不改）"""
        return pick_lower_hz(self.profile.get("available_hz") or [],
                             self.profile.get("current_hz"))

    # ------------------------------------------------------------ 应用到配置
    def apply(self, cfg: dict) -> List[str]:
        """把自动值填进 config 的「自动位」（值为 None 的键）。

        只在键为 None 时填 —— 用户手设过的数字一律不动。
        返回被填的键名（供日志/自检显示）。
        """
        p = self.profile
        filled: List[str] = []
        self.last_filled = filled
        # 上一轮填进去的自动值：**能自动更新的前提**。
        # 若只按「键为 None 才填」，自动值写进 config 后就被冻结 —— 实测峰值
        # 后来学到更高也更新不了。所以记住自己填过什么，下次仍可覆盖它；
        # 而用户手设的值（不等于上次自动值）永远不动。
        auto_prev = self.data.get("auto_filled") or {}
        auto_now: Dict = {}

        def put(key, val):
            if val is None:
                return
            cur = cfg.get(key)
            if cur is None or (key in auto_prev and cur == auto_prev[key]):
                cfg[key] = val
                auto_now[key] = val
                filled.append(key)

        if p:
            put("gpu_tgp_max_watts", self.gpu_tgp_max())
            put("gpu_temp_limit_c", self.gpu_temp_limit())
            put("power_envelope_watts", FALLBACK["power_envelope_w"])
            put("dc_refresh_hz", self.dc_refresh_hz())
            put("pd_low_hz_value", self.dc_refresh_hz())
            # 没有独显的机器：分配器 / GPU Eco / 核显优先都无从谈起
            if not p.get("has_dgpu"):
                for k in ("alloc_enabled", "gpu_eco_auto", "gpupick_enabled"):
                    if cfg.get(k) is not False and k not in filled:
                        cfg[k] = False
                        filled.append(k)
            # 厂商通道不可用：GPU Eco 每轮去开一次驱动只是白费力气
            elif not p.get("atkacpi_ok"):
                if cfg.get("gpu_eco_auto") is None:
                    cfg["gpu_eco_auto"] = False
                    filled.append("gpu_eco_auto")
        self.data["auto_filled"] = auto_now
        self._dirty = True
        self.save()
        return filled

    def watt_limits(self) -> dict:
        """给 power.py 的读数钳制用：按本机 CPU 规模 / 独显档次放宽上限。

        钳制是为了丢掉传感器坏值（实测见过 588 W），但写死一台机器的规格
        会在高端机上反过来把**真值**当坏值丢掉，所以这里按硬件放大。
        """
        cores = int(self.profile.get("cpu_cores") or os.cpu_count() or 8)
        cpu = max(65.0, min(200.0, cores * 12.0))
        gpu = 140.0
        nv = self.profile.get("nvml_tgp_max_w")
        if nv:
            gpu = max(140.0, float(nv) * 1.4)
        return {"cpu_w": round(cpu, 1), "soc_w": round(cpu + 140.0, 1),
                "gpu_w": round(gpu, 1)}

    # ------------------------------------------------------------ 展示
    def summary(self) -> dict:
        p = self.profile
        return {
            "has_dgpu": bool(p.get("has_dgpu")),
            "dgpu_name": p.get("dgpu_name") or "",
            "igpu_name": p.get("igpu_name") or "",
            "atkacpi_ok": bool(p.get("atkacpi_ok")),
            "hz": p.get("current_hz"),
            "available_hz": p.get("available_hz") or [],
            "gpu_tgp_max_w": self.gpu_tgp_max(),
            "gpu_temp_limit_c": self.gpu_temp_limit(),
            "dc_refresh_hz": self.dc_refresh_hz(),
            "gpu_peak_w": self.gpu_peak_w,
            "sources": p.get("sources") or [],
        }


def ensure(cfg: dict, nvml=None, display=None, atkacpi_ok=None,
           path: Optional[str] = None) -> HwProfile:
    """入口：档案缺失/过旧就现测一次，然后填进 config 的自动位。"""
    hp = HwProfile(path=path)
    fresh = hp.stale()
    if fresh and display is None:
        # 显示通道是纯 ctypes 查询、零副作用，没有就自己建一个，
        # 免得档案缺了刷新率这一项（离电降刷会因此永远不生效）
        try:
            from .display import DisplayCtl
            display = DisplayCtl()
        except Exception:
            pass
    if fresh:
        try:
            hp.refresh(nvml=nvml, display=display, atkacpi_ok=atkacpi_ok)
        except Exception:
            pass
        # 新档案：先拿本机历史采样日志里的峰值垫底（跑过游戏的机器才有）
        try:
            hp.seed_from_csv()
        except Exception:
            pass
    try:
        hp.apply(cfg)
    except Exception:
        pass
    return hp
