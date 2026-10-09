# -*- coding: utf-8 -*-
"""
实时功耗 / 温度 / 频率监测（PDH + NVML，零子进程、零依赖）

为什么不用 PowerShell：
  每次 Get-Counter 要 ~1 秒且会拉起子进程（本机历史上正是"幽灵终端窗"的源头）。
  PDH(Performance Data Helper) 是 Windows 原生计数器 API，实测一次全量读取
  只需 1~5 毫秒，且完全在当前进程内，不产生任何窗口。

数据来源
--------
PDH 性能计数器（英文名，中文系统同样可用 PdhAddEnglishCounterW）:
    \\Energy Meter(*)\\Power
        Socket Power  AMD SoC 总功耗（CPU 核心 + 核显 + NPU + SoC 周边），单位 mW
        CPU Power     CPU 核心总功耗
        GPU Power     核显(Radeon 890M) 功耗
        NPU Power     NPU 功耗
        System Power  平台侧功耗估算
    \\Thermal Zone Information(*)\\Temperature        CPU 热区温度（开尔文）
    \\Processor Information(*)\\% Processor Time      每逻辑核占用（含 _Total）
    \\Processor Information(_Total)\\% Processor Utility  归一化占用（含频率影响）
    \\Processor Information(_Total)\\Processor Frequency 标称频率 MHz
    \\Processor Information(_Total)\\% Processor Performance 相对标称的性能比

NVML（独显）:
    温度 / 功耗 / SM 时钟 / 利用率 / 降频原因 / 温度阈值

温度单位说明（2026-10-04 实测标定）:
    TZ01 空载 351、CPU 负载 42W 时 366，即 +15 对应 +23W，
    等效热阻 ≈ 0.65 K/W —— 符合笔记本散热常识。
    若按「0.1℃」解释则等效 0.065 K/W，优于任何笔记本散热器，物理上不可能。
    故判定该计数器单位为开尔文，℃ = 原始值 − 273.15。
    （同时保留自动判别：原始值 > 150 视为开尔文，否则视为已是摄氏度。）
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes, POINTER, byref, c_void_p, c_double, c_uint32, Structure

# ------------------------------------------------------------------ PDH 绑定
_pdh = ctypes.WinDLL("pdh", use_last_error=True)

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2


class PDH_FMT_COUNTERVALUE(Structure):
    _fields_ = [("CStatus", c_uint32), ("doubleValue", c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", PDH_FMT_COUNTERVALUE)]


_pdh.PdhOpenQueryW.restype = c_uint32
_pdh.PdhOpenQueryW.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
_pdh.PdhAddEnglishCounterW.restype = c_uint32
_pdh.PdhAddEnglishCounterW.argtypes = [c_void_p, ctypes.c_wchar_p, c_void_p, POINTER(c_void_p)]
_pdh.PdhCollectQueryData.restype = c_uint32
_pdh.PdhCollectQueryData.argtypes = [c_void_p]
_pdh.PdhGetFormattedCounterArrayW.restype = c_uint32
_pdh.PdhGetFormattedCounterArrayW.argtypes = [c_void_p, c_uint32,
                                              POINTER(c_uint32), POINTER(c_uint32), c_void_p]
_pdh.PdhCloseQuery.argtypes = [c_void_p]

# 计数器路径
C_ENERGY = r"\Energy Meter(*)\Power"
C_TZ = r"\Thermal Zone Information(*)\Temperature"
C_PROC_TIME = r"\Processor Information(*)\% Processor Time"
C_PROC_UTIL = r"\Processor Information(_Total)\% Processor Utility"
C_PROC_FREQ = r"\Processor Information(_Total)\Processor Frequency"
C_PROC_PERF = r"\Processor Information(_Total)\% Processor Performance"

_NOMINAL_MHZ = None   # 由标称频率计数器给出


def _norm_temp(raw):
    """把热区原始值归一成摄氏度"""
    if raw is None:
        return None
    try:
        v = float(raw)
    except Exception:
        return None
    if v > 150.0:          # 开尔文
        return v - 273.15
    return v               # 已经是摄氏度


def _sane(v, hi, lo=0.0):
    """物理合理性钳制：超出范围的读数视为传感器坏值，宁缺毋滥。

    背景：Energy Meter / NVML 偶发吐出天量瞬时值（实测见过独显 588W，
    而 TGP 上限才 100W），不拦住就会把「整机 594W/110W」这种离谱
    数字画到面板上。
    """
    try:
        v = float(v)
    except Exception:
        return None
    if v != v or v < lo or v > hi:      # 含 NaN
        return None
    return v


# 各通路的物理上限（W），按本机规格留足余量
_W_LIMITS = {
    "cpu_w": 120,      # CPU 实测满载 42~47W
    "igpu_w": 80,      # 核显 890M
    "npu_w": 60,
    "soc_w": 150,      # SoC 总和（含上面所有）
    "system_w": 250,
    "gpu_w": 140,      # 独显 TGP 100W + Dynamic Boost 余量
}


class PowerMonitor:
    """一次性打开 PDH 查询，之后每次 sample() 只需 1~5 ms"""

    def __init__(self, nvml=None):
        self.available = False
        self.nvml = nvml
        self._hq = c_void_p()
        self._c = {}
        self._err = ""
        self._proc_freq_nominal = None
        # CPU 温度备份源（见 set_temp_backup）。缺省 None = 不启用。
        self._temp_backup = None
        self._cross_check_every = 180.0   # 交叉校验间隔秒；0 = 关闭校验
        self._tz_fail_streak = 0          # 主源连续读不到的次数
        self.temp_note = ""               # 供面板/体检显示：当前用哪个源、有无异常
        self.temp_fallbacks = 0           # 发生降级的次数，判断主源是否长期失效
        self._temp_check_at = 0.0
        self._temp_diverged = False       # 交叉校验是否发现两源不一致
        self._prime()

    def _prime(self):
        try:
            if _pdh.PdhOpenQueryW(None, None, byref(self._hq)) != 0:
                self._err = "PdhOpenQuery 失败"
                return
            for path in (C_ENERGY, C_TZ, C_PROC_TIME, C_PROC_UTIL, C_PROC_FREQ, C_PROC_PERF):
                h = c_void_p()
                if _pdh.PdhAddEnglishCounterW(self._hq, path, None, byref(h)) == 0:
                    self._c[path] = h
            if C_ENERGY not in self._c and C_TZ not in self._c:
                self._err = "关键计数器不可用"
                return
            _pdh.PdhCollectQueryData(self._hq)      # 第一帧（速率/通配实例的基线）
            time.sleep(0.25)
            _pdh.PdhCollectQueryData(self._hq)      # 第二帧后各通配实例才稳定
            self.available = True
        except Exception as e:
            self._err = str(e)

    def close(self):
        try:
            if self._hq:
                _pdh.PdhCloseQuery(self._hq)
        except Exception:
            pass
        self._hq = c_void_p()
        self.available = False

    # ------------------------------------------------------------ 低层读取
    def _read(self, path):
        """返回 [(实例名, 数值), ...]；偶发首帧无效时自动补采一次"""
        for attempt in (0, 1):
            out = self._read_once(path)
            if out:
                return out
            if attempt == 0:
                try:
                    _pdh.PdhCollectQueryData(self._hq)
                except Exception:
                    return []
        return []

    def _read_once(self, path):
        h = self._c.get(path)
        if not h:
            return []
        size = c_uint32(0)
        cnt = c_uint32(0)
        rc = _pdh.PdhGetFormattedCounterArrayW(
            h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, byref(size), byref(cnt), None)
        if size.value == 0 or rc not in (PDH_MORE_DATA, 0):
            return []
        buf = ctypes.create_string_buffer(size.value)
        if _pdh.PdhGetFormattedCounterArrayW(
                h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, byref(size), byref(cnt), buf) != 0:
            return []
        items = ctypes.cast(buf, POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        return [(items[i].szName or "", float(items[i].FmtValue.doubleValue))
                for i in range(cnt.value)]

    @staticmethod
    def _pick(pairs, *names):
        low = {str(n).lower(): v for n, v in pairs}
        for want in names:
            for k in (want, want.replace(" ", "")):
                if k.lower() in low:
                    return low[k.lower()]
        return None

    # ------------------------------------------------------------ 采样
    def set_temp_backup(self, fn, cross_check_every: float = 180.0):
        """注册 CPU 温度的第二数据源，主源失效时自动兜底。

        为什么要这个
        ------------
        游戏加加那类工具会在界面上让你「选择传感器」——同一项硬件可能存在多个
        数据源，而且**源会悄悄失效**。本项目被这类事坑过太多次：单一数据源一旦
        静默失灵，上层完全看不出来（日志干净、测试全绿、结论却是错的）。

        本机实测（2026-10-07，tools/probe_temp_sources.py，8 次采样）：
            PDH Thermal Zone 与 华硕 ATKACPI 的 CPU 温度差值恒定 +0.1℃，
            极差 0.00℃ —— 两个源读的是同一个传感器，可以互为备份。

        设计要点
        --------
        - **惰性**：正常情况一次都不碰备份源，零额外调用、零额外功耗。
          只在主源真的读不到时才降级。（本机是在电池上跑的程序，任何
          额外的硬件调用都要算到自身开销里。）
        - **不新建句柄**：备份 fn 内部应复用 lp.atkacpi 的全局单例 。
          ATKACPI 是独占内核句柄，多开一个就等于多一条死锁路径。
        - **低频交叉校验**：每隔 cross_check_every 秒额外读一次备份源做比对，
          差值超过阈值就置 _temp_diverged 让体检能报出来 —— 用来发现
          "某个源在悄悄漂移"，而不是等它彻底死掉才发现。
        """
        self._temp_backup = fn
        self._cross_check_every = max(0.0, float(cross_check_every))

    def _read_backup_temp(self):
        """读备份温度源。任何异常都吞掉 —— 备份源自己坏了不能把采样搞崩。"""
        if not self._temp_backup:
            return None
        try:
            v = self._temp_backup()
        except Exception:
            return None
        try:
            v = float(v)
        except Exception:
            return None
        if v != v or v <= 0 or v > 130:      # 含 NaN 与明显坏值
            return None
        return v


    def sample(self, want_cores: bool = True, want_gpu: bool = True) -> dict:
        """一次完整采样。任何字段取不到都是 None，绝不抛异常。

        want_gpu=False：跳过独显（NVML）。**离电时默认跳过** —— 本机离电走核显，
        连 NVML 都不必调，免得把独显驱动拉在 active 状态、妨碍它进 D3 深度省电。
        """
        out = {
            "ok": False, "ts": time.time(),
            "cpu_w": None, "igpu_w": None, "npu_w": None, "soc_w": None, "system_w": None,
            "cpu_temp": None, "cpu_temp_raw": None,
            "cpu_util": None, "cpu_util_utility": None, "core_max": None,
            "cpu_mhz": None, "cpu_mhz_nominal": None, "cpu_perf_pct": None,
            "gpu_w": None, "gpu_temp": None, "gpu_util": None,
            "gpu_clock": None, "gpu_clock_max": None, "gpu_temp_slowdown": None,
            "gpu_throttle": [],
        }
        if not self.available:
            out["err"] = self._err
            return out
        try:
            _pdh.PdhCollectQueryData(self._hq)
        except Exception as e:
            out["err"] = str(e)
            return out

        # ---- Energy Meter（mW -> W，带坏值钳制）
        em = self._read(C_ENERGY)
        if em:
            sock = self._pick(em, "Socket Power", "Apu Power")
            out["soc_w"] = None if sock is None else _sane(sock / 1000.0, _W_LIMITS["soc_w"])
            v = self._pick(em, "CPU Power")
            out["cpu_w"] = None if v is None else _sane(v / 1000.0, _W_LIMITS["cpu_w"])
            v = self._pick(em, "GPU Power")
            out["igpu_w"] = None if v is None else _sane(v / 1000.0, _W_LIMITS["igpu_w"])
            v = self._pick(em, "NPU Power")
            out["npu_w"] = None if v is None else _sane(v / 1000.0, _W_LIMITS["npu_w"])
            v = self._pick(em, "System Power")
            out["system_w"] = None if v is None else _sane(v / 1000.0, _W_LIMITS["system_w"])

        tz = self._read(C_TZ)
        tz_val = _norm_temp(tz[0][1]) if tz else None
        if tz_val is not None:
            out["cpu_temp_raw"] = tz[0][1]
            out["cpu_temp"] = tz_val
            out["cpu_temp_src"] = "tz"
            self.temp_note = ""
            self._tz_fail_streak = 0
        else:
            # 主源读不到 —— 降级到备份源，而不是让上层拿到 None
            self._tz_fail_streak = getattr(self, "_tz_fail_streak", 0) + 1
            bk = self._read_backup_temp()
            if bk is not None:
                out["cpu_temp"] = bk
                out["cpu_temp_src"] = "backup"
                self.temp_fallbacks += 1
                self.temp_note = ("Thermal Zone 读数失效，已降级到备份源"
                                  "（连续 %d 次）" % self._tz_fail_streak)
            else:
                out["cpu_temp_src"] = None
                self.temp_note = "温度源全部失效"

        # 低频交叉校验：两个源都在时不比不知道，比值才发现"谁在悄悄漂移"
        now = time.time()
        if (self._temp_backup and self._cross_check_every
                and now - self._temp_check_at >= self._cross_check_every):
            self._temp_check_at = now
            bk = self._read_backup_temp()
            if bk is not None and tz_val is not None:
                diff = abs(bk - tz_val)
                out["temp_cross_diff"] = diff
                # 本机实测差值 0.1℃，给到 6℃ 才算真异常（避开常规抖动）
                self._temp_diverged = diff > 6.0
                if self._temp_diverged:
                    self.temp_note = ("两个温度源不一致：Thermal Zone %.1f℃ / "
                                      "备份 %.1f℃（差 %.1f℃）"
                                      % (tz_val, bk, diff))

        # ---- CPU 占用 / 频率
        pt = self._read(C_PROC_TIME)
        if pt:
            core_vals = [v for n, v in pt if n and n != "_Total"]
            tot = self._pick([(n, v) for n, v in pt], "_Total")
            out["cpu_util"] = tot
            if core_vals:
                out["core_max"] = max(core_vals)
        if C_PROC_UTIL in self._c:
            u = self._read(C_PROC_UTIL)
            if u:
                out["cpu_util_utility"] = u[0][1]
        if C_PROC_FREQ in self._c:
            f = self._read(C_PROC_FREQ)
            if f:
                nom = f[0][1]
                out["cpu_mhz_nominal"] = nom
                if nom and self._proc_freq_nominal is None:
                    self._proc_freq_nominal = nom
        if C_PROC_PERF in self._c:
            p = self._read(C_PROC_PERF)
            if p:
                pct = p[0][1]
                out["cpu_perf_pct"] = pct
                nom = out["cpu_mhz_nominal"] or self._proc_freq_nominal
                if nom and pct:
                    out["cpu_mhz"] = nom * pct / 100.0

        # ---- 独显
        n = self.nvml
        if want_gpu and n is not None and getattr(n, "ready", False):
            out["gpu_temp"] = n.temperature()
            out["gpu_w"] = _sane(n.power_usage(), _W_LIMITS["gpu_w"])
            u = n.utilization()
            out["gpu_util"] = None if u is None else max(0.0, min(100.0, float(u)))
            out["gpu_clock"] = n.clock_sm()
            out["gpu_clock_max"] = n.max_clock_sm()
            out["gpu_temp_slowdown"] = n.temp_threshold(1)
            out["gpu_throttle"] = n.throttle_reasons()

        out["ok"] = out["soc_w"] is not None or out["cpu_temp"] is not None
        return out

    # ------------------------------------------------------------ 派生量
    @staticmethod
    def total_w(snap: dict, overhead_w: float = 12.0) -> float | None:
        """整机估算功耗 = SoC 功耗 + 独显功耗 + 主板/屏幕/风扇等固定开销"""
        if snap.get("soc_w") is None and snap.get("gpu_w") is None:
            return None
        return (snap.get("soc_w") or 0.0) + (snap.get("gpu_w") or 0.0) + float(overhead_w)


def _selftest():
    from .nvmlctl import Nvml
    n = Nvml()
    print("NVML ready:", n.ready, [g.name for g in n.gpus])
    if n.ready:
        print("  GPU 温度阈值: slowdown=%s shutdown=%s" % (n.temp_threshold(1), n.temp_threshold(0)))
        print("  功耗墙范围:", n.power_limit_range(), "W")
    m = PowerMonitor(n)
    print("PDH available:", m.available, m._err)
    for i in range(4):
        s = m.sample()
        print("  #%d soc=%s cpu=%s igpu=%s gpu=%s tempC=%s cpuUtil=%s coreMax=%s mhz=%s gpuClock=%s throttle=%s" % (
            i,
            None if s["soc_w"] is None else round(s["soc_w"], 1),
            None if s["cpu_w"] is None else round(s["cpu_w"], 1),
            None if s["igpu_w"] is None else round(s["igpu_w"], 1),
            None if s["gpu_w"] is None else round(s["gpu_w"], 1),
            None if s["cpu_temp"] is None else round(s["cpu_temp"], 1),
            None if s["cpu_util"] is None else round(s["cpu_util"], 1),
            None if s["core_max"] is None else round(s["core_max"], 1),
            None if s["cpu_mhz"] is None else int(s["cpu_mhz"]),
            s["gpu_clock"], s["gpu_throttle"]))
        time.sleep(1.0)
    m.close()


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _selftest()
