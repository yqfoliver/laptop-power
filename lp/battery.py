# -*- coding: utf-8 -*-
"""电池遥测：ACPI 电池类设备接口（纯 ctypes，零子进程、零窗口）

为什么不用别的路子（2026-10-04 在本机逐条实测过）：
  · PDH 的 \\Battery Status(*)\\* 全部返回 PDH_CSTATUS_NO_OBJECT —— 本机没有这组计数器
  · WMI（root\\wmi BatteryStatus / Win32_Battery）要拉 PowerShell 子进程：
    约 1 秒、要闪一个控制台（正是历史上「幽灵终端窗」的源头）
  · ACPI 电池设备接口（GUID_DEVCLASS_BATTERY）是内核直接暴露的设备接口：
      SetupDiGetClassDevs -> 设备路径 -> CreateFile -> DeviceIoControl
    一次读取 ≈ 0.2 ms，普通用户权限就能读（只有写才要管理员）。

能拿到什么
----------
  BATTERY_INFORMATION.DesignedCapacity / FullChargedCapacity  -> 电池健康度
  BATTERY_STATUS.Capacity(mWh) / Voltage(mV)                  -> 剩余容量、电压
  BATTERY_STATUS.Rate(mW)                                     -> 实时充/放电功率（放电为正）
  => 再配上满充容量就能算：剩余电量 %、按当前功率还能撑多久

本机实测（TUF A14 FA401WV，2026-10-04）：
  设计容量 73000 mWh，满充容量 60536 mWh -> 健康度 82.9%
  插电满充时 PowerState=0x1（AC 在线）、Rate=0 mW
"""
from __future__ import annotations

import ctypes
import time
from ctypes import (POINTER, Structure, byref, c_long, c_ubyte, c_void_p,
                    sizeof, wintypes)

# ------------------------------------------------------------ ACPI 常量
POWER_ON_LINE = 0x00000001        # 已接交流电
DISCHARGING = 0x00000002          # 正在放电
CHARGING = 0x00000004             # 正在充电
CRITICAL = 0x00000008             # 电量告急

IOCTL_BATTERY_QUERY_TAG = 0x294040
IOCTL_BATTERY_QUERY_INFORMATION = 0x294044
IOCTL_BATTERY_QUERY_STATUS = 0x29404C

GUID_DEVCLASS_BATTERY = "{72631e54-78a4-11d0-bcf7-00aa00b7b32a}"
BATTERY_INFORMATION_LEVEL = 0     # BatteryInformation

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ole = ctypes.WinDLL("ole32", use_last_error=True)


class GUID(Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", c_ubyte * 8)]

    def __str__(self):
        d = self.Data4
        return "{%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}" % (
            self.Data1, self.Data2, self.Data3,
            d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7])


class SP_DEVICE_INTERFACE_DATA(Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", c_void_p)]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(Structure):
    _pack_ = 1
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1024)]


class BATTERY_INFORMATION(Structure):
    _fields_ = [("Capabilities", wintypes.DWORD), ("Technology", wintypes.BYTE),
                ("Reserved", wintypes.BYTE * 3), ("Chemistry", wintypes.BYTE * 4),
                ("DesignedCapacity", wintypes.DWORD),
                ("FullChargedCapacity", wintypes.DWORD),
                ("DefaultAlert1", wintypes.DWORD), ("DefaultAlert2", wintypes.DWORD),
                ("CriticalBias", wintypes.DWORD), ("CycleCount", wintypes.DWORD)]


class BATTERY_WAIT_STATUS(Structure):
    _fields_ = [("BatteryTag", wintypes.DWORD), ("Timeout", wintypes.DWORD),
                ("PowerState", wintypes.DWORD), ("LowCapacity", wintypes.DWORD),
                ("HighCapacity", wintypes.DWORD)]


class BATTERY_STATUS(Structure):
    _fields_ = [("PowerState", wintypes.DWORD), ("Capacity", wintypes.DWORD),
                ("Voltage", wintypes.DWORD), ("Rate", c_long)]


class BATTERY_QUERY_INFORMATION(Structure):
    _fields_ = [("BatteryTag", wintypes.DWORD), ("InformationLevel", wintypes.DWORD),
                ("AtRate", c_long)]


class _SYSTEM_POWER_STATUS(Structure):
    _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD)]


_setupapi.SetupDiGetClassDevsW.restype = c_void_p
_setupapi.SetupDiGetClassDevsW.argtypes = [POINTER(GUID), wintypes.LPCWSTR,
                                           wintypes.HWND, wintypes.DWORD]
_setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    c_void_p, c_void_p, POINTER(GUID), wintypes.DWORD,
    POINTER(SP_DEVICE_INTERFACE_DATA)]
_setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    c_void_p, POINTER(SP_DEVICE_INTERFACE_DATA), c_void_p, wintypes.DWORD,
    POINTER(wintypes.DWORD), c_void_p]
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [c_void_p]

_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             c_void_p, wintypes.DWORD, wintypes.DWORD, c_void_p]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, c_void_p,
                                 wintypes.DWORD, c_void_p, wintypes.DWORD,
                                 POINTER(wintypes.DWORD), c_void_p]
_k32.GetSystemPowerStatus.argtypes = [POINTER(_SYSTEM_POWER_STATUS)]


def battery_paths() -> list:
    """枚举本机所有 ACPI 电池设备路径（通常只有一个）"""
    out = []
    g = GUID()
    try:
        _ole.CLSIDFromString(GUID_DEVCLASS_BATTERY, byref(g))
    except Exception:
        return out
    h = _setupapi.SetupDiGetClassDevsW(byref(g), None, None,
                                       DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not h or h == ctypes.c_void_p(-1).value:
        return out
    try:
        did = SP_DEVICE_INTERFACE_DATA()
        did.cbSize = sizeof(did)
        k = 0
        while _setupapi.SetupDiEnumDeviceInterfaces(h, None, byref(g), k, byref(did)):
            need = wintypes.DWORD(0)
            _setupapi.SetupDiGetDeviceInterfaceDetailW(h, byref(did), None, 0,
                                                       byref(need), None)
            if not need.value:
                break
            buf = ctypes.create_string_buffer(need.value)
            # cbSize 在 x64 上是 8（DWORD + 对齐），先写进去再取详情
            ctypes.memmove(buf, byref(wintypes.DWORD(sizeof(wintypes.DWORD) * 2)), 4)
            if _setupapi.SetupDiGetDeviceInterfaceDetailW(
                    h, byref(did), buf, need.value, byref(need), None):
                out.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
            k += 1
    except Exception:
        pass
    finally:
        try:
            _setupapi.SetupDiDestroyDeviceInfoList(h)
        except Exception:
            pass
    return out


class BatteryMonitor:
    """一次打开、反复读取。任何异常都不抛出，只把字段留成 None。"""

    INFO_TTL = 300.0          # 「设计/满充容量」这类静态信息 5 分钟刷一次就够
    RATE_ALPHA = 0.3          # 放电功率 EWMA：压掉瞬时尖峰，剩余时间才不跳

    def __init__(self):
        self.present = False
        self.open_error = ""
        self.designed_mwh = None
        self.full_mwh = None
        self.cycles = None
        self.capacity_mwh = None
        self.voltage_v = None
        self.rate_w = None            # 放电为正、充电为负
        self.state = 0
        self.last = {}
        self._path = None
        self._handle = None
        self._tag = 0
        self._info_at = 0.0
        self._rate_ewma = None
        self._fail = 0
        self._next_try = 0.0

    # ---------------------------------------------------------- 打开/关闭
    def _open(self) -> bool:
        self.close()
        paths = battery_paths()
        if not paths:
            self.open_error = "未找到电池设备"
            return False
        self._path = paths[0]
        h = _k32.CreateFileW(self._path, GENERIC_READ | GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                             OPEN_EXISTING, 0, None)
        if not h or h == ctypes.c_void_p(-1).value:
            # 只读打开兜底（少数机型不允许写）
            h = _k32.CreateFileW(self._path, GENERIC_READ,
                                 FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                 OPEN_EXISTING, 0, None)
        if not h or h == ctypes.c_void_p(-1).value:
            self.open_error = "打开电池设备失败 err=%s" % ctypes.get_last_error()
            return False
        self._handle = h
        self.present = True
        self.open_error = ""
        return True

    def close(self):
        if self._handle:
            try:
                _k32.CloseHandle(self._handle)
            except Exception:
                pass
        self._handle = None

    # ---------------------------------------------------------- 低层
    def _ioctl(self, code, inbuf, insize, outbuf, outsize) -> bool:
        if not self._handle:
            return False
        ret = wintypes.DWORD(0)
        try:
            return bool(_k32.DeviceIoControl(
                self._handle, code, inbuf, insize, outbuf, outsize,
                byref(ret), None))
        except Exception:
            return False

    def _query_tag(self) -> bool:
        tag = wintypes.DWORD(0)
        if self._ioctl(IOCTL_BATTERY_QUERY_TAG, None, 0, byref(tag), 4):
            self._tag = tag.value
            return True
        return False

    def _read_info(self) -> bool:
        if not self._tag:
            return False
        qi = BATTERY_QUERY_INFORMATION(self._tag, BATTERY_INFORMATION_LEVEL, 0)
        bi = BATTERY_INFORMATION()
        if not self._ioctl(IOCTL_BATTERY_QUERY_INFORMATION, byref(qi), sizeof(qi),
                           byref(bi), sizeof(bi)):
            return False
        self.designed_mwh = int(bi.DesignedCapacity) or None
        self.full_mwh = int(bi.FullChargedCapacity) or None
        self.cycles = int(bi.CycleCount) or None
        self._info_at = time.time()
        return True

    def _read_status(self) -> bool:
        if not self._tag:
            return False
        ws = BATTERY_WAIT_STATUS(self._tag, 0, 0, 0, 0)
        bs = BATTERY_STATUS()
        if not self._ioctl(IOCTL_BATTERY_QUERY_STATUS, byref(ws), sizeof(ws),
                           byref(bs), sizeof(bs)):
            return False
        self.state = int(bs.PowerState)
        self.capacity_mwh = int(bs.Capacity) or None
        self.voltage_v = round(bs.Voltage / 1000.0, 3) if bs.Voltage else None
        rate = bs.Rate / 1000.0
        # 本机（ASUS FA401WV）固件放电时 Rate 报负值；统一成「放电为正、充电为负」
        if (self.state & DISCHARGING) and rate < 0:
            rate = -rate
        elif (self.state & CHARGING) and rate > 0:
            rate = -rate
        self.rate_w = round(rate, 3)
        return True

    # ---------------------------------------------------------- 对外
    def health_pct(self):
        if self.full_mwh and self.designed_mwh:
            return round(100.0 * self.full_mwh / self.designed_mwh, 1)
        return None

    def percent(self):
        if self.capacity_mwh and self.full_mwh:
            return max(0, min(100, int(round(100.0 * self.capacity_mwh / self.full_mwh))))
        return None

    def eta_minutes(self):
        """按当前放电功率估算还能撑多久（分钟）。只在使用电池时有效。"""
        if not self.capacity_mwh or self._rate_ewma is None or self._rate_ewma <= 0.5:
            return None
        hours = (self.capacity_mwh / 1000.0) / self._rate_ewma
        return int(round(hours * 60))

    def sample(self, force: bool = False) -> dict:
        """读一次电池。返回扁平 dict，任何取不到的字段都是 None。"""
        now = time.time()
        if (not self._handle) and now >= self._next_try:
            self._open()
            if self._handle:
                self._query_tag()
                self._read_info()

        ok = False
        if self._handle:
            if not self._tag:
                self._query_tag()
            if self.designed_mwh is None or (now - self._info_at) > self.INFO_TTL:
                self._read_info()
            ok = self._read_status()
            if not ok:
                # 电池被换过 / 刚插拔过：tag 变了，重来一遍
                self._fail += 1
                if self._query_tag() and self._read_status():
                    ok = True
                    self._fail = 0
                elif self._fail >= 3:
                    self._fail = 0
                    self._next_try = now + 30.0        # 退避 30 秒再重开
                    self._open()
                    if self._handle:
                        self._query_tag()
                        self._read_info()

        fb = _SYSTEM_POWER_STATUS()
        ac = True
        pct_fb = None
        life_s = None
        try:
            if _k32.GetSystemPowerStatus(byref(fb)):
                ac = (fb.ACLineStatus == 1)
                if fb.BatteryLifePercent != 255:
                    pct_fb = int(fb.BatteryLifePercent)
                if fb.BatteryLifeTime not in (0xFFFFFFFF, 0):
                    life_s = int(fb.BatteryLifeTime)
        except Exception:
            pass

        discharging = bool(self.state & DISCHARGING)
        charging = bool(self.state & CHARGING)
        if not ok:
            discharging = not ac
            charging = ac and pct_fb is not None and pct_fb < 100

        # 放电功率做 EWMA；AC 下清零，避免插上电后剩余时间还挂在旧值
        if discharging and self.rate_w and self.rate_w > 0:
            self._rate_ewma = (self.RATE_ALPHA * self.rate_w
                               + (1 - self.RATE_ALPHA) * self._rate_ewma
                               if self._rate_ewma else self.rate_w)
        elif not discharging:
            self._rate_ewma = None

        pct = self.percent() if ok else pct_fb
        eta = self.eta_minutes()
        if eta is None and life_s:
            eta = int(round(life_s / 60.0))

        out = {
            "ok": bool(ok),
            "present": bool(self.present or pct_fb is not None),
            "ac": ac,
            "percent": pct,
            "capacity_mwh": self.capacity_mwh,
            "full_mwh": self.full_mwh,
            "designed_mwh": self.designed_mwh,
            "health_pct": self.health_pct(),
            "cycles": self.cycles,
            "voltage_v": self.voltage_v,
            "rate_w": (round(self.rate_w, 2) if (ok and self.rate_w is not None) else None),
            "rate_avg_w": (round(self._rate_ewma, 2) if self._rate_ewma else None),
            "discharging": bool(discharging),
            "charging": bool(charging),
            "critical": bool(self.state & CRITICAL) if ok else False,
            "eta_minutes": eta,
            "state_cn": ("放电中" if discharging else
                         "充电中" if charging else
                         "已接电源" if ac else "未知"),
            "err": self.open_error,
        }
        self.last = out
        return out


def _selftest():  # pragma: no cover
    print("电池设备：", battery_paths())
    m = BatteryMonitor()
    for i in range(3):
        s = m.sample()
        print("#%d ok=%s %s %s%% rate=%sW avg=%sW eta=%smin health=%s%% "
              "cap=%smWh full=%smWh V=%s"
              % (i, s["ok"], s["state_cn"], s["percent"], s["rate_w"],
                 s["rate_avg_w"], s["eta_minutes"], s["health_pct"],
                 s["capacity_mwh"], s["full_mwh"], s["voltage_v"]))
        time.sleep(1.0)
    m.close()


if __name__ == "__main__":  # pragma: no cover
    _selftest()
