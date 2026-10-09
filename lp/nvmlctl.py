# -*- coding: utf-8 -*-
"""
NVIDIA 独显监测（ctypes 直连 nvml.dll，零依赖）

2026-10-04 修复：
  旧版 _bind() 里写了一个不存在的符号 `nvmlDeviceSetPowerLimit`
  （正确名是 nvmlDeviceSetPowerManagementLimit），WinDLL 取属性抛
  AttributeError 被 except 吞掉 -> _bind() 恒返回 False -> self._ready
  永远是 False -> 整个 NVML 通路（利用率 / 温度 / 功耗）从未生效。
  改用 _try_bind() 逐个符号绑定，缺一个不影响其余。

本机实测能力（FA401WV / RTX 4060 Laptop / 驱动 5xx）：
  可读：温度、功耗(mW)、SM 时钟、显存时钟、利用率、降频原因、温度阈值
  不可读：功耗墙 nvmlDeviceGetPowerManagementLimit -> rc=3 NOT_SUPPORTED
  不可写：nvmlDeviceSetPowerManagementLimit -> rc=4 INVALID_ARGUMENT
          nvmlDeviceSetPowerManagementMode  -> 符号不存在
  => 独显功耗无法由软件设定（笔记本 vBIOS 锁死），只能通过
     「让出 CPU 侧功耗预算给 Dynamic Boost」间接影响独显功耗。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes, POINTER, byref, c_int, c_uint, c_char_p
from typing import List, Optional

PM_MINIMUM = 0
PM_MAXIMUM = 1

# nvmlDeviceGetTemperature 的 sensorType
TEMP_GPU = 0
TEMP_MEMORY = 1

# nvmlDeviceGetTemperatureThreshold 的 thresholdType
THRESHOLD_SHUTDOWN = 0
THRESHOLD_SLOWDOWN = 1
THRESHOLD_MEM_MAX = 2
THRESHOLD_GPU_MAX = 3

_DLL_CANDIDATES = [
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll"),
    r"C:\Windows\System32\nvml.dll",
]


class _Utilization(ctypes.Structure):
    """nvmlUtilization_t：GPU / 显存 利用率（百分比）"""
    _fields_ = [("gpu", c_uint), ("memory", c_uint)]


class NvmlGPU:
    def __init__(self, handle, index, name):
        self.handle = handle
        self.index = index
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover
        return "<NvmlGPU %s>" % self.name


class Nvml:
    def __init__(self):
        self.dll = None
        self.gpus: List[NvmlGPU] = []
        self._ready = False
        for path in _DLL_CANDIDATES:
            if os.path.exists(path):
                try:
                    self.dll = ctypes.WinDLL(path)
                    break
                except OSError:
                    continue
        if self.dll is None:
            return
        self._bind()
        try:
            rc = self.dll.nvmlInit_v2()
            # 999 = NVML_ERROR_ALREADY_INITIALIZED（同一进程重复 init）
            if rc in (0, 999):
                count = c_uint(0)
                if self.dll.nvmlDeviceGetCount_v2(byref(count)) == 0:
                    for i in range(count.value):
                        h = wintypes.HANDLE()
                        if self.dll.nvmlDeviceGetHandleByIndex_v2(i, byref(h)) != 0:
                            continue
                        buf = ctypes.create_string_buffer(256)
                        if self.dll.nvmlDeviceGetName(h, buf, 256) == 0:
                            name = buf.value.decode("utf-8", "ignore")
                        else:
                            name = "GPU#%d" % i
                        self.gpus.append(NvmlGPU(h, i, name))
                    self._ready = bool(self.gpus)
        except Exception:
            self._ready = False

    # ---------------------------------------------------------------- 绑定
    def _bind(self) -> bool:
        """逐个符号绑定：缺哪个就把哪个置 None，不影响其它功能。"""
        d = self.dll
        if d is None:
            return False

        def req(name, restype, argtypes):
            f = getattr(d, name, None)
            if f is None:
                return False
            f.restype = restype
            f.argtypes = argtypes
            return True

        H = wintypes.HANDLE
        ok = req("nvmlInit_v2", c_int, [])
        ok &= req("nvmlDeviceGetCount_v2", c_int, [POINTER(c_uint)])
        ok &= req("nvmlDeviceGetHandleByIndex_v2", c_int, [c_uint, POINTER(H)])
        ok &= req("nvmlDeviceGetName", c_int, [H, c_char_p, c_uint])

        # 以下全部"可选"：绑定失败只是该字段读不到
        req("nvmlDeviceGetTemperature", c_int, [H, c_int, POINTER(c_uint)])
        req("nvmlDeviceGetTemperatureThreshold", c_int, [H, c_int, POINTER(c_uint)])
        req("nvmlDeviceGetPowerUsage", c_int, [H, POINTER(c_uint)])
        req("nvmlDeviceGetPowerManagementLimit", c_int, [H, POINTER(c_uint)])
        req("nvmlDeviceGetPowerManagementLimitConstraints", c_int,
            [H, POINTER(c_uint), POINTER(c_uint)])
        req("nvmlDeviceGetEnforcedPowerLimit", c_int, [H, POINTER(c_uint)])
        req("nvmlDeviceSetPowerManagementLimit", c_int, [H, c_uint])
        req("nvmlDeviceGetClockInfo", c_int, [H, c_int, POINTER(c_uint)])
        req("nvmlDeviceGetMaxClockInfo", c_int, [H, c_int, POINTER(c_uint)])
        req("nvmlDeviceGetClock", c_int, [H, c_int, c_int, POINTER(c_uint)])
        req("nvmlDeviceGetUtilizationRates", c_int, [H, POINTER(_Utilization)])
        req("nvmlDeviceGetFanSpeed", c_int, [H, POINTER(c_uint)])
        req("nvmlDeviceGetPerformanceState", c_int, [H, POINTER(c_uint)])
        req("nvmlDeviceGetCurrentClocksThrottleReasons", c_int, [H, ctypes.c_ulonglong])
        req("nvmlDeviceGetPowerManagementMode", c_int, [H, POINTER(c_uint)])
        req("nvmlDeviceSetPowerManagementMode", c_int, [H, c_uint])
        return bool(ok)

    # ---------------------------------------------------------------- 接口
    @property
    def ready(self) -> bool:
        return self._ready and bool(self.gpus)

    def _u32(self, fn_name, *args):
        """调用一个返回 u32 的 NVML 函数，失败返回 None"""
        if not self.ready or self.dll is None:
            return None
        f = getattr(self.dll, fn_name, None)
        if f is None:
            return None
        v = c_uint(0)
        try:
            if f(self.gpus[0].handle, *args, byref(v)) != 0:
                return None
        except Exception:
            return None
        return int(v.value)

    def mode(self) -> Optional[int]:
        return self._u32("nvmlDeviceGetPowerManagementMode")

    def set_mode(self, mode: int) -> bool:
        if not self.ready:
            return False
        f = getattr(self.dll, "nvmlDeviceSetPowerManagementMode", None)
        if f is None:
            return False
        ok = True
        for g in self.gpus:
            try:
                if f(g.handle, mode) != 0:
                    ok = False
            except Exception:
                ok = False
        return ok

    def get_power_limit(self) -> Optional[int]:
        """mW；本机返回 None（NOT_SUPPORTED）"""
        return self._u32("nvmlDeviceGetPowerManagementLimit")

    # ---- 监测类（本机全部可用）
    def temperature(self, sensor: int = TEMP_GPU) -> Optional[int]:
        """独显温度（摄氏度）"""
        return self._u32("nvmlDeviceGetTemperature", sensor)

    def temp_threshold(self, which: int = THRESHOLD_SLOWDOWN) -> Optional[int]:
        """温度阈值（摄氏度）：SLOWDOWN=开始降频 / SHUTDOWN=保护关机"""
        return self._u32("nvmlDeviceGetTemperatureThreshold", which)

    def power_usage(self) -> Optional[float]:
        """独显实时功耗（瓦特）"""
        v = self._u32("nvmlDeviceGetPowerUsage")
        return None if v is None else v / 1000.0

    def power_limit_range(self):
        """(最小值W, 最大值W)；不可用时 (None, None)。本机 max=100W"""
        if not self.ready:
            return None, None
        f = getattr(self.dll, "nvmlDeviceGetPowerManagementLimitConstraints", None)
        if f is None:
            return None, None
        lo, hi = c_uint(0), c_uint(0)
        try:
            if f(self.gpus[0].handle, byref(lo), byref(hi)) != 0:
                return None, None
        except Exception:
            return None, None
        return lo.value / 1000.0, hi.value / 1000.0

    def clock_sm(self) -> Optional[int]:
        """SM（图形）时钟 MHz — nvmlDeviceGetClockInfo(1)"""
        return self._u32("nvmlDeviceGetClockInfo", 1)

    def clock_mem(self) -> Optional[int]:
        return self._u32("nvmlDeviceGetClockInfo", 2)

    def max_clock_sm(self) -> Optional[int]:
        return self._u32("nvmlDeviceGetMaxClockInfo", 1)

    def utilization(self) -> Optional[int]:
        """独显 GPU 利用率（%）"""
        if not self.ready:
            return None
        f = getattr(self.dll, "nvmlDeviceGetUtilizationRates", None)
        if f is None:
            return None
        u = _Utilization()
        try:
            if f(self.gpus[0].handle, byref(u)) != 0:
                return None
        except Exception:
            return None
        return int(u.gpu)

    def fan_speed(self) -> Optional[int]:
        return self._u32("nvmlDeviceGetFanSpeed")

    # nvmlClocksThrottleReason 位标志
    THROTTLE_BITS = [
        (0x0000000000000001, "GPU_IDLE"),
        (0x0000000000000002, "软件限频"),
        (0x0000000000000004, "显存空闲"),
        (0x0000000000000008, "功耗墙 SLOWDOWN"),
        (0x0000000000000010, "温度墙 SW_THERMAL"),
        (0x0000000000000020, "硬件限速"),
        (0x0000000000000040, "硬件温度墙"),
        (0x0000000000000080, "功耗制动 HW_SLOWDOWN"),
        (0x0000000000000100, "同步增强"),
        (0x0000000000000200, "显示时钟设置"),
        (0x0000000000000400, "FORCED"),
        (0x0000000000020000, "功耗墙(TGP)"),
        (0x0000000000040000, "GPU 降频 DISPLAY"),
    ]

    def throttle_reasons(self) -> List[str]:
        """当前独显被限制的原因（中文简写）"""
        if not self.ready:
            return []
        f = getattr(self.dll, "nvmlDeviceGetCurrentClocksThrottleReasons", None)
        if f is None:
            return []
        try:
            f.restype = c_int
            f.argtypes = [wintypes.HANDLE, POINTER(ctypes.c_ulonglong)]
            v = ctypes.c_ulonglong(0)
            if f(self.gpus[0].handle, byref(v)) != 0:
                return []
        except Exception:
            return []
        bits = int(v.value)
        out = []
        for mask, label in self.THROTTLE_BITS:
            if bits & mask:
                out.append(label)
        return out
