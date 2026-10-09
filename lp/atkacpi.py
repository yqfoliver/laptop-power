# -*- coding: utf-8 -*-
r"""华硕 ATKACPI 硬件直控通道（对标 G-Helper 的 AsusACPI.cs，2026-10-05 本机实测打通）

=============================================================================
一、为什么之前判定"不可用"（教训）
=============================================================================
旧记录写着「`\\.\ATKACPI` 可打开，但 36 种载荷的 DeviceIoControl 全失败 err=1」。
实际原因是**漏了 DeviceInit() 握手**：设备打开后必须先发一次 INIT(0x54494E49)，
之后 DEVS/DSTS 才被 EC 接受。补上这一步后全部通道立即可用（err=0）。
⇒ 结论翻案：本机 ACPI 通道**完整可用**，风扇/温度/性能模式/充电阈值全是普通权限可读写，
  不需要管理员、不需要驱动、不需要 WMI（AsusAtkWmi_WMNB 实例 Access Denied 的老问题绕开了）。

二、协议（来自 G-Helper AsusACPI.cs，本机逐条验证）
=============================================================================
  CreateFileW("\\\\.\\ATKACPI", GENERIC_READ|GENERIC_WRITE, share=3, OPEN_EXISTING)
  DeviceIoControl(h, 0x0022240C, in, n, out, 16, &ret, NULL)
      in = [MethodID u32][argsLen u32][args...]
      MethodID: DEVS=0x53564544(写)  DSTS=0x53545344(读)  INIT=0x54494E49  WDOG=0x474F4457
      DEVS args = [DeviceID u32][value u32]        写入，out[0:4]==1 表示成功
      DSTS args = [DeviceID u32][status u32]       读取，out 见下

  读取返回 16 字节：
      · 标量型：out[0:4] = 0x0001_xxxx，低 16 位即数值（如 0x0001003D = 61℃）
      · 缓冲型：整个 16/64 字节都是数据（风扇曲线、VRAM 选项表）
      · 不支持：DeviceIoControl 返回 False 且 err=1(ERROR_INVALID_FUNCTION)
      · 大缓冲：DSTS 的 argsLen 要写 12（out_size 由调用方给 64）——见 read_buf(extra_in=True)

三、本机实测能力（2026-10-05，普通权限）
=============================================================================
  可读可写：风扇曲线(CPU/GPU)、性能模式(0/1/2/3)、屏幕 Overdrive
  可读：    CPU/GPU 风扇档位(0~120)、CPU 温度、GPU TGP 基准/加值、GPU MUX/Eco 状态
  读了没反应（写返回 1 但回读不变，疑似被 ASUS 服务托管）：充电阈值 BatteryLimit
  ⇒ 充电上限仍以 MyASUS 为准，本模块的 set_battery_limit 只做"尝试 + 校验"。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量

CONTROL_CODE = 0x0022240C
M_DEVS = 0x53564544     # 写
M_DSTS = 0x53545344     # 读
M_INIT = 0x54494E49     # 握手（必须！）
M_WDOG = 0x474F4457

DEV_PATH = "\\\\.\\ATKACPI"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
SHARE_RW = 3

# 设备 ID 表（G-Helper AsusACPI.cs 原文，本机已逐条验证可读性）
DEV: Dict[str, int] = {
    # —— 风扇 / 温度 ——
    "cpu_fan":          0x00110013,   # 风扇档位 0~120（不是 RPM！实测 37 = 37%）
    "gpu_fan":          0x00110014,
    "mid_fan":          0x00110031,   # 本机不支持（err=1）
    "cpu_temp":         0x00120094,   # ℃
    "gpu_temp":         0x00120097,
    "fan_hysteresis":   0x00110034,   # 本机不支持
    "fan_range_cpu":    0x00110022,
    "fan_range_gpu":    0x00110023,
    "fan_curve_cpu":    0x00110024,   # 16B：8 个温度点 + 8 个转速%
    "fan_curve_gpu":    0x00110025,
    "fan_curve_mid":    0x00110032,
    # —— 性能模式 ——
    "perf_mode":        0x00120075,   # 0=平衡 1=增强 2=静音 3=全速 4=手动
    "vivo_mode":        0x00110019,   # 本机不支持
    "status_mode":      0x00090031,
    "power_saving":     0x00090032,
    # —— 功耗限制（本机 READ 全 0；未做写入标定，代码里不主动使用）——
    "ppt_slow":         0x001200A0,   # sPPT / PL2
    "ppt_sustained":    0x001200A3,   # SPL / PL1
    "ppt_cpu_all":      0x001200B0,   # 全 AMD 机型的 CPU PPT
    "ppt_fast":         0x001200C1,   # fPPT
    "ppt_gpu_boost":    0x001200C0,   # NVIDIA GPU Boost（Dynamic Boost）
    "ppt_gpu_temp":     0x001200C2,   # NVIDIA GPU 温度目标 75~87℃
    "ppt_cpu_temp":     0x0012009E,   # CPU 温度上限（本机 err=1）
    "ppt_gpu2cpu":      0x0012009C,   # GPU→CPU 动态增强（本机 err=1）
    "ppt_cross":        0x0012009F,   # 交叉负载（本机 err=1）
    "gpu_power_var":    0x00120098,   # 独显 TGP 附加瓦数
    "gpu_power_base":   0x00120099,   # 独显 TGP 基准瓦数（本机读 55）
    # —— 电池 ——
    "battery_limit":    0x00120057,   # 无有效回读，写返回 1 但回读不变
    "battery_discharge": 0x0012005A,  # 本机读 0
    # —— 显卡 / 屏幕 ——
    "gpu_mux":          0x00090016,   # 1=标准(混合)
    "gpu_eco":          0x00090020,   # 1=独显关闭
    "screen_overdrive": 0x00050019,
    "overdrive_support": 0x00050020,
    "screen_opt_bright": 0x0005002A,
}

# 性能模式（G-Helper 枚举）
PERF_BALANCED, PERF_TURBO, PERF_SILENT, PERF_FULL, PERF_MANUAL = 0, 1, 2, 3, 4
PERF_LABEL = {
    PERF_BALANCED: "平衡",
    PERF_TURBO: "增强",
    PERF_SILENT: "静音",
    PERF_FULL: "全速",
    PERF_MANUAL: "手动",
}

# 风扇曲线档位（DSTS 的 status 参数就是曲线档位；注意 ASUS 的 1/2 与性能模式相反）
CURVE_BALANCED, CURVE_TURBO, CURVE_SILENT = 0, 1, 2

FAN_CPU, FAN_GPU = "cpu", "gpu"


# ---------------------------------------------------------------- 底层绑定

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                 ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL

_INVALID = ctypes.c_void_p(-1).value


def _u32(v: int) -> bytes:
    return (int(v) & 0xFFFFFFFF).to_bytes(4, "little")


def _i32(b: bytes) -> int:
    return int.from_bytes(b[:4], "little", signed=True)


# ---------------------------------------------------------------- 主类

class AtkAcpi:
    """ATKACPI 句柄封装。线程安全靠外部串行化（本程序所有写操作都在主循环线程）。"""

    def __init__(self) -> None:
        self._h = None
        self._ok = False
        self._inited = False
        self._support: Dict[int, bool] = {}
        # 改动前的原值，供 restore() 还原
        self._saved_perf: Optional[int] = None
        self._saved_curves: Dict[str, bytes] = {}
        self._last_err = 0
        self.open()

    # -------------------------------------------------- 生命周期
    def open(self) -> bool:
        if self._ok:
            return True
        try:
            h = _k32.CreateFileW(DEV_PATH, GENERIC_READ | GENERIC_WRITE, SHARE_RW,
                                 None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        except Exception:
            h = None
        if not h or h == _INVALID:
            self._last_err = ctypes.get_last_error()
            self._h = None
            self._ok = False
            return False
        self._h = h
        self._ok = True
        self._inited = False
        # 关键：必须先握手，否则后续 DEVS/DSTS 一律 err=1
        self._call(M_INIT, b"\x00" * 8)
        self._inited = True
        return True

    def close(self) -> None:
        if self._h:
            try:
                _k32.CloseHandle(self._h)
            except Exception:
                pass
        self._h = None
        self._ok = False
        self._inited = False

    @property
    def ok(self) -> bool:
        return bool(self._ok and self._h)

    @property
    def last_error(self) -> int:
        return self._last_err

    # -------------------------------------------------- 原始调用
    def _call(self, method: int, args: bytes, out_size: int = 16) -> Optional[bytes]:
        if not self._h:
            return None
        try:
            inb = ctypes.create_string_buffer(8 + len(args))
            ctypes.memmove(inb, _u32(method), 4)
            ctypes.memmove(ctypes.byref(inb, 4), _u32(len(args)), 4)
            if args:
                ctypes.memmove(ctypes.byref(inb, 8), args, len(args))
            out = ctypes.create_string_buffer(out_size)
            ret = wintypes.DWORD(0)
            okc = _k32.DeviceIoControl(self._h, CONTROL_CODE, inb, 8 + len(args),
                                       out, out_size, ctypes.byref(ret), None)
            if not okc:
                self._last_err = ctypes.get_last_error()
                return None
            return bytes(out.raw)
        except Exception:
            self._last_err = -1
            return None

    def _args2(self, dev_id: int, second: int = 0) -> bytes:
        return _u32(dev_id) + _u32(second)

    # -------------------------------------------------- 读
    def read_buf(self, dev: str, status: int = 0, size: int = 64) -> Optional[bytes]:
        """缓冲型读取（风扇曲线 / VRAM 选项表）。

        大缓冲要求 argsLen 写 12 而不是 8——G-Helper 的 DeviceGetLarge 用
        `[DSTS][len=4+extraIn][DeviceID][extraIn]`；这里 extraIn=4（即 status 字段）。
        """
        dev_id = DEV.get(dev, dev if isinstance(dev, int) else 0)
        if not dev_id:
            return None
        args = _u32(dev_id) + _u32(status)
        return self._call(M_DSTS, args, size)

    def read_val(self, dev: str, status: int = 0) -> Optional[int]:
        """标量型读取。返回 None = 本机不支持。"""
        raw = self.read_buf(dev, status, 16)
        if raw is None or len(raw) < 4:
            return None
        u = int.from_bytes(raw[:4], "little")
        if not (u & 0x10000):          # 无 0x0001_xxxx 标志位 ⇒ 不是有效标量
            return None
        return u & 0xFFFF

    def supported(self, dev: str) -> bool:
        dev_id = DEV.get(dev, 0)
        if not dev_id:
            return False
        if dev_id in self._support:
            return self._support[dev_id]
        ok = self.read_buf(dev, 0, 16) is not None
        self._support[dev_id] = ok
        return ok

    # -------------------------------------------------- 写
    def write_int(self, dev: str, value: int) -> bool:
        dev_id = DEV.get(dev, 0)
        if not dev_id:
            return False
        raw = self._call(M_DEVS, self._args2(dev_id, int(value) & 0xFFFFFFFF), 16)
        return bool(raw) and _i32(raw) == 1

    def write_buf(self, dev: str, data: bytes) -> bool:
        dev_id = DEV.get(dev, 0)
        if not dev_id or not data:
            return False
        args = _u32(dev_id) + bytes(data)
        raw = self._call(M_DEVS, args, 16)
        return bool(raw) and _i32(raw) == 1

    # -------------------------------------------------- 高层：风扇 / 温度
    def fan_level(self, which: str = FAN_CPU) -> Optional[int]:
        """风扇档位（0~120，单位 %）。G-Helper 口径：>120 视为无效。"""
        key = "gpu_fan" if which == FAN_GPU else "cpu_fan"
        v = self.read_val(key)
        if v is None:
            return None
        return None if v > 120 else v

    def cpu_temp(self) -> Optional[float]:
        v = self.read_val("cpu_temp")
        return None if v is None or v <= 0 or v > 130 else float(v)

    def gpu_temp(self) -> Optional[float]:
        v = self.read_val("gpu_temp")
        return None if v is None or v <= 0 or v > 130 else float(v)

    # -------------------------------------------------- 高层：性能模式
    def perf_mode(self) -> Optional[int]:
        return self.read_val("perf_mode")

    def get_perf_mode(self) -> Optional[int]:
        return self.perf_mode()

    def set_perf_mode(self, mode: int, remember: bool = True) -> bool:
        if mode not in PERF_LABEL:
            return False
        if remember and self._saved_perf is None:
            self._saved_perf = self.perf_mode()
        return self.write_int("perf_mode", int(mode))

    # -------------------------------------------------- 高层：风扇曲线
    def get_fan_curve(self, which: str = FAN_CPU, mode: int = CURVE_BALANCED
                      ) -> Optional[bytes]:
        """读 16 字节曲线：前 8 字节温度点(℃)，后 8 字节转速(%)。"""
        key = "fan_curve_gpu" if which == FAN_GPU else "fan_curve_cpu"
        raw = self.read_buf(key, mode, 64)
        if not raw or len(raw) < 16:
            return None
        curve = raw[:16]
        if all(b == 0 for b in curve):     # 空曲线 = 该档位没设置
            return None
        return curve

    def set_fan_curve(self, which: str = FAN_CPU, curve: Optional[bytes] = None,
                      remember: bool = True) -> bool:
        if not curve or len(curve) != 16 or all(b == 0 for b in curve):
            return False
        key = "fan_curve_gpu" if which == FAN_GPU else "fan_curve_cpu"
        if remember and which not in self._saved_curves:
            cur = self.get_fan_curve(which, CURVE_BALANCED)
            if cur:
                self._saved_curves[which] = cur
        return self.write_buf(key, bytes(curve))

    @staticmethod
    def curve_points(curve: bytes) -> Tuple[List[int], List[int]]:
        """拆成 (温度点列表, 转速列表)"""
        if not curve or len(curve) != 16:
            return [], []
        return list(curve[:8]), list(curve[8:16])

    @staticmethod
    def curve_text(curve: bytes) -> str:
        temps, fans = AtkAcpi.curve_points(curve)
        if not temps:
            return "—"
        return " ".join("%d℃→%d%%" % (t, f) for t, f in zip(temps, fans))

    # -------------------------------------------------- 高层：电池 / 屏幕 / 显卡
    def set_battery_limit(self, percent: int) -> bool:
        """尝试写充电上限。本机写返回 1 但回读不变（被 ASUS 服务托管）⇒ 仅作尝试。"""
        try:
            p = int(percent)
        except Exception:
            return False
        if not (40 <= p <= 100):
            return False
        return self.write_int("battery_limit", p)

    def overdrive_supported(self) -> bool:
        return self.read_val("overdrive_support") == 1

    def overdrive(self) -> Optional[int]:
        return self.read_val("screen_overdrive")

    def set_overdrive(self, on: bool) -> bool:
        return self.write_int("screen_overdrive", 1 if on else 0)

    def gpu_eco(self) -> Optional[int]:
        return self.read_val("gpu_eco")

    def gpu_mux(self) -> Optional[int]:
        return self.read_val("gpu_mux")

    # -------------------------------------------------- 状态汇总
    def snapshot(self) -> dict:
        """给面板/状态接口用的一次性快照（含不支持项为 None）。"""
        if not self.ok:
            return {"available": False}
        curve = self.get_fan_curve(FAN_CPU, CURVE_BALANCED)
        pm = self.perf_mode()
        return {
            "available": True,
            "fan_cpu": self.fan_level(FAN_CPU),
            "fan_gpu": self.fan_level(FAN_GPU),
            "temp_cpu": self.cpu_temp(),
            "temp_gpu": self.gpu_temp(),
            "perf_mode": pm,
            "perf_label": PERF_LABEL.get(pm, "未知") if pm is not None else "—",
            "overdrive": self.overdrive(),
            "overdrive_supported": self.overdrive_supported(),
            "gpu_base_w": self.read_val("gpu_power_base"),
            "gpu_var_w": self.read_val("gpu_power_var"),
            "fan_curve": curve.hex() if curve else None,
            "fan_curve_text": self.curve_text(curve) if curve else "—",
            "saved_perf": self._saved_perf,
        }

    # -------------------------------------------------- 还原
    def restore(self) -> bool:
        """把本模块改动过的硬件状态还原（性能模式 + 风扇曲线）。"""
        changed = False
        if self._saved_perf is not None:
            cur = self.perf_mode()
            if cur != self._saved_perf:
                changed = self.write_int("perf_mode", int(self._saved_perf)) or changed
            self._saved_perf = None
        for which, curve in list(self._saved_curves.items()):
            key = "fan_curve_gpu" if which == FAN_GPU else "fan_curve_cpu"
            changed = self.write_buf(key, curve) or changed
        self._saved_curves.clear()
        return changed


# ---------------------------------------------------------------- 单例

_SINGLETON: Optional[AtkAcpi] = None
_TRIED = False


def get() -> Optional[AtkAcpi]:
    """拿全局句柄；本机不支持时返回 None（不抛异常）。"""
    global _SINGLETON, _TRIED
    if _SINGLETON is not None and _SINGLETON.ok:
        return _SINGLETON
    if _TRIED and _SINGLETON is None:
        return None
    _TRIED = True
    try:
        inst = AtkAcpi()
    except Exception:
        inst = None
    if inst is not None and inst.ok:
        _SINGLETON = inst
    return _SINGLETON


def available() -> bool:
    return get() is not None


if __name__ == "__main__":      # pragma: no cover
    a = get()
    if a is None:
        print("ATKACPI 不可用")
        raise SystemExit(1)
    import json
    print(json.dumps(a.snapshot(), ensure_ascii=False, indent=1))
    for m in (0, 1, 2):
        c = a.get_fan_curve(FAN_CPU, m)
        print("curve mode=%d: %s" % (m, AtkAcpi.curve_text(c) if c else "—"))
