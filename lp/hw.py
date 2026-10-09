# -*- coding: utf-8 -*-
"""硬件 / 电源 / 进程 / 前台窗口 探测（纯 ctypes + WMI，无第三方库）"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import winreg
from ctypes import (Structure, POINTER, byref, c_int, c_void_p, wintypes)
from typing import Optional

# 本程序自己的窗口标题（面板/托盘共用，webview2panel.TITLE 与托盘同名字面量）。
# 不 import webview2panel（避免环形依赖），字面量保持一致即可。
_SELF_WINDOW_TITLES = {"笔记本电源自适应"}

# ------------------------------------------------------------- Win32 封装
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD)]


k32.GetSystemPowerStatus.argtypes = [POINTER(SYSTEM_POWER_STATUS)]
u32.GetForegroundWindow.restype = wintypes.HANDLE
u32.GetWindowThreadProcessId.argtypes = [wintypes.HANDLE, POINTER(wintypes.DWORD)]
u32.GetWindowTextW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, c_int]
u32.GetWindowRect.argtypes = [wintypes.HANDLE, POINTER(RECT)]
u32.MonitorFromWindow.argtypes = [wintypes.HANDLE, wintypes.DWORD]
u32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, POINTER(MONITORINFO)]

# 进程快照
k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

ULONG_PTR = ctypes.c_ulonglong      # 64/32 位都按指针宽度；下面用 sizeof 校准


class PROCESSENTRY32W(Structure):
    """
    PROCESSENTRY32W 的真实布局。
    注意 th32DefaultHeapID 是 ULONG_PTR（64 位下 8 字节且需 8 字节对齐），
    写成 DWORD 会让整个结构体尺寸偏小，导致 Process32FirstW 报 ERROR_BAD_LENGTH。
    """
    _fields_ = [("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("_pad", ctypes.c_char * (ctypes.sizeof(ULONG_PTR) - ctypes.sizeof(wintypes.DWORD))),
                ("th32DefaultHeapID", ULONG_PTR),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


k32.Process32FirstW.restype = wintypes.BOOL
k32.Process32FirstW.argtypes = [wintypes.HANDLE, POINTER(PROCESSENTRY32W)]
k32.Process32NextW.restype = wintypes.BOOL
k32.Process32NextW.argtypes = [wintypes.HANDLE, POINTER(PROCESSENTRY32W)]
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
psapi = ctypes.WinDLL("psapi", use_last_error=True)
# QueryFullProcessImageNameW 只需 PROCESS_QUERY_LIMITED_INFORMATION，
# 不像 GetModuleBaseNameW 那样必须拿到 VM_READ，受限句柄也能出结果。
k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                           wintypes.LPWSTR, POINTER(wintypes.DWORD)]
if hasattr(psapi, "GetModuleBaseNameW"):
    psapi.GetModuleBaseNameW.restype = wintypes.DWORD
    psapi.GetModuleBaseNameW.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                         wintypes.LPWSTR, wintypes.DWORD]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


def _process_name(pid: int) -> str:
    """拿到进程的可执行文件名，多级兜底"""
    for access in (PROCESS_QUERY_LIMITED_INFORMATION,
                   PROCESS_QUERY_INFORMATION | PROCESS_VM_READ):
        h = k32.OpenProcess(access, False, int(pid))
        if not h:
            continue
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(h, 0, buf, byref(size)):
                full = buf.value
                if full:
                    return full.rsplit("\\", 1)[-1].lower()
            nb = ctypes.create_unicode_buffer(260)
            if psapi.GetModuleBaseNameW(h, 0, nb, 260):
                return str(nb.value).lower()
        finally:
            k32.CloseHandle(h)
    return ""


# ------------------------------------------------------------- 电源状态
def power_status() -> dict:
    """返回 AC 是否在线、电池百分比、充放电状态"""
    st = SYSTEM_POWER_STATUS()
    info = {"ac": True, "battery_percent": None, "charging": False, "has_battery": True}
    try:
        k32.GetSystemPowerStatus(byref(st))
    except Exception:
        return info

    code = st.ACLineStatus
    info["ac"] = (code == 1)
    info["ac_unknown"] = (code == 255)

    pct = st.BatteryLifePercent
    if code == 0 and pct == 255:
        # 无电池 / 情况未知，回退到 WMI 判断
        info["has_battery"] = _wmi_has_battery()
        info["ac"] = not info["has_battery"]
    else:
        info["has_battery"] = True
        info["battery_percent"] = None if pct == 255 else int(pct)
        info["charging"] = bool(st.BatteryFlag & 0x08) if st.BatteryFlag != 0x80 else False
    return info


def _wmi_has_battery() -> bool:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance -Namespace root/wmi -Class BatteryStatus -ErrorAction SilentlyContinue | "
             "Measure-Object -Property PowerOnline -Minimum).Minimum -eq $false"],
            capture_output=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
        return b"True" in (r.stdout or b"")
    except Exception:
        return True


# ------------------------------------------------------------- 进程
# 排行榜排除名单：系统服务/自身/瞬时进程，避免噪声刷屏
_TOP_SKIP = {
    "system idle process", "system", "registry", "memcompression",
    "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "smss.exe", "svchost.exe", "audiodg.exe", "wudfhost.exe", "wmiprvse.exe",
    "msmpeng.exe", "securityhealthservice.exe", "searchindexer.exe",
    "conhost.exe", "cmd.exe", "python.exe", "pythonw.exe",
    "windowsterminal.exe", "powershell.exe", "dwm.exe",
}


def top_cpu_procs(window: float = 4.0, top_n: int = 3) -> list:
    """采样 window 秒，返回 CPU 占用最高的进程 [(pct, name, pid), ...]。

    用于离电时的「耗电排行」展示；任何失败返回 []。
    """
    def _snap():
        out = {}
        h = k32.CreateToolhelp32Snapshot(0x2, 0)
        if not h:
            return out
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(h, byref(pe))
            while ok:
                pid = pe.th32ProcessID
                hh = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION_IO, False, pid)
                if hh:
                    ft_cr, ft_ex, ft_k, ft_u = (FILETIME(), FILETIME(),
                                                FILETIME(), FILETIME())
                    if k32.GetProcessTimes(hh, byref(ft_cr), byref(ft_ex),
                                           byref(ft_k), byref(ft_u)):
                        out[pid] = (pe.szExeFile, ft_k.value() + ft_u.value())
                    k32.CloseHandle(hh)
                ok = k32.Process32NextW(h, byref(pe))
        finally:
            k32.CloseHandle(h)
        return out

    try:
        if not hasattr(k32, "GetProcessTimes") or not k32.GetProcessTimes.restype:
            k32.GetProcessTimes.restype = wintypes.BOOL
            k32.GetProcessTimes.argtypes = [wintypes.HANDLE, POINTER(FILETIME),
                                            POINTER(FILETIME), POINTER(FILETIME),
                                            POINTER(FILETIME)]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        idle0, kern0, user0 = FILETIME(), FILETIME(), FILETIME()
        if not k32.GetSystemTimes(byref(idle0), byref(kern0), byref(user0)):
            return []
        p0 = _snap()
        time.sleep(window)
        idle1, kern1, user1 = FILETIME(), FILETIME(), FILETIME()
        if not k32.GetSystemTimes(byref(idle1), byref(kern1), byref(user1)):
            return []
        p1 = _snap()
        dt = max(1, (kern1.value() + user1.value()) - (kern0.value() + user0.value()))
        rows = []
        for pid, (name, t) in p1.items():
            if pid in p0:
                d = t - p0[pid][1]
                if d <= 0:
                    continue
                pct = 100.0 * d / dt
                if pct >= 1.0 and str(name).lower() not in _TOP_SKIP:
                    rows.append((round(pct, 1), str(name), pid))
        rows.sort(reverse=True)
        return rows[:top_n]
    except Exception:
        return []


def list_processes() -> dict:
    """返回 {exe名小写: pid}，同名取最小 pid"""
    procs: dict = {}
    h = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not h:
        return procs
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(h, byref(pe))
        while ok:
            name = pe.szExeFile
            pid = pe.th32ProcessID
            key = str(name).lower()
            if key not in procs or pid < procs[key]:
                procs[key] = pid
            ok = k32.Process32NextW(h, byref(pe))
    finally:
        k32.CloseHandle(h)
    return procs


def foreground() -> dict:
    """前台窗口所在进程信息。

    **本程序自己的窗口（原生面板/托盘）当前台时返回空**：否则面板一打开，
    场景判定/自学习/原因展示都会把 pythonw.exe 当成「用户正在用的程序」。
    双保险：除了 pid 比对，还按「进程名 == 自己 exe」和「窗口标题 == 面板标题」
    兜底 —— WebView2 渲染进程等特殊窗口的 pid 不一定还在本进程里
    （2026-10-08 实测截图出现过自己把自己当前台）。
    """
    res = {"process": "", "title": "", "fullscreen": False}
    try:
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return res
        pid = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, byref(pid))
        if pid.value and pid.value == k32.GetCurrentProcessId():
            res["self"] = True
            return res
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, buf, 512)
        res["title"] = buf.value or ""

        if pid.value:
            res["process"] = _process_name(pid.value)
            # 兜底 1：前台进程就是本程序的 exe（onefile 双进程等形态）
            try:
                if sys.executable and \
                        res["process"].lower() == os.path.basename(sys.executable).lower():
                    res.clear()
                    res.update({"process": "", "title": "", "fullscreen": False,
                                "self": True})
                    return res
            except Exception:
                pass
        # 兜底 2：窗口标题与面板/托盘窗口标题一致
        if res.get("title") and res["title"] in _SELF_WINDOW_TITLES:
            res.clear()
            res.update({"process": "", "title": "", "fullscreen": False,
                        "self": True})
        # 全屏判定：矩形铺满显示器 且 没有标题栏/边框样式（否则最大化的普通应用会被误判）
        GWL_STYLE = -16
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        rc = RECT()
        if u32.GetWindowRect(hwnd, byref(rc)):
            style = u32.GetWindowLongW(hwnd, GWL_STYLE)
            has_chrome = bool(style & (WS_CAPTION | WS_THICKFRAME))
            if not has_chrome:
                mono = u32.MonitorFromWindow(hwnd, 0)
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if u32.GetMonitorInfoW(mono, byref(mi)):
                    res["fullscreen"] = (rc.left == mi.rcMonitor.left and rc.top == mi.rcMonitor.top
                                         and (rc.right - rc.left) == (mi.rcMonitor.right - mi.rcMonitor.left)
                                         and (rc.bottom - rc.top) == (mi.rcMonitor.bottom - mi.rcMonitor.top))
    except Exception:
        pass
    return res


# ------------------------------------------------------------- 负载采样
# 参考 Windows Power Throttling / Adaptive Energy Saver 的做法：
# 持续采样 CPU / 磁盘 / 网络 / GPU，用来区分「重负载」与「轻负载」，
# 供 lp/algo.py 的 EWMA 追踪器使用。全部走 ctypes，零依赖、无子进程。
class FILETIME(Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    def value(self) -> int:
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


k32.GetSystemTimes.argtypes = [POINTER(FILETIME), POINTER(FILETIME), POINTER(FILETIME)]
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
k32.DeviceIoControl.restype = wintypes.BOOL
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                POINTER(wintypes.DWORD), wintypes.LPVOID]


class IO_COUNTERS(Structure):
    """GetProcessIoCounters 的返回结构，用于把整机 I/O 增量归到某个进程。"""
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


k32.GetProcessIoCounters.restype = wintypes.BOOL
k32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, POINTER(IO_COUNTERS)]
PROCESS_QUERY_LIMITED_INFORMATION_IO = 0x1000

# 全局磁盘吞吐计数（IOCTL_DISK_PERFORMANCE，XP 起可用，非 Vista+ 的 IOCTL_DISK_*_STATISTICS_EX）
_IOCTL_DISK_PERFORMANCE = 0x00070020


class DISK_PERFORMANCE(Structure):
    _fields_ = [("BytesRead", ctypes.c_longlong),
                ("BytesWritten", ctypes.c_longlong),
                ("ReadTime", ctypes.c_longlong),
                ("WriteTime", ctypes.c_longlong),
                ("IdleTime", ctypes.c_longlong),
                ("ReadCount", wintypes.DWORD),
                ("WriteCount", wintypes.DWORD),
                ("QueueDepth", wintypes.DWORD),
                ("SplitCount", wintypes.DWORD),
                ("QueryTime", ctypes.c_longlong),
                ("StorageDeviceNumber", wintypes.DWORD),
                ("StorageManagerName", wintypes.WCHAR * 8)]


# ------------------------------------------------------------ 自身省电
class _POWER_THROTTLING_STATE(Structure):
    _fields_ = [("ControlMask", wintypes.DWORD), ("StateMask", wintypes.DWORD)]


_ProcessPowerThrottling = 4          # PROCESS_INFORMATION_CLASS
_PTHROTTLING_EXECUTION_SPEED = 0x1   # EcoQoS
_PRIORITY_IDLE, _PRIORITY_NORMAL = 0x40, 0x20


def self_power_saver(on: bool) -> bool:
    """离电时把本进程切到 EcoQoS（E核/低频优先）+ 低优先级；插电还原。

    只影响本程序自身，不动系统任何设置。返回是否成功。
    """
    try:
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetProcessInformation.restype = wintypes.BOOL
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              c_void_p, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        cur = k32.GetCurrentProcess()
        st = _POWER_THROTTLING_STATE(
            _PTHROTTLING_EXECUTION_SPEED,                    # ControlMask
            _PTHROTTLING_EXECUTION_SPEED if on else 0)       # StateMask
        ok1 = k32.SetProcessInformation(cur, _ProcessPowerThrottling,
                                        byref(st), ctypes.sizeof(st))
        ok2 = k32.SetPriorityClass(cur, _PRIORITY_IDLE if on else _PRIORITY_NORMAL)
        return bool(ok1 or ok2)
    except Exception:
        return False


class _LoadSampler:
    """有状态的负载采样器：每次调用返回相对于上次调用的「增量」指标。"""

    def __init__(self):
        self._cpu = None          # (idle, kernel, user) 累计值
        self._disk = None         # (read_bytes, write_bytes)
        self._net = None          # 网卡收发字节累计
        self._last_t = 0.0
        self._last_proc_io = None  # {pid: (read, write)}

    # ---------------------------------------------------------- CPU
    def cpu_percent(self) -> Optional[float]:
        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        if not k32.GetSystemTimes(byref(idle), byref(kern), byref(user)):
            return None
        cur = (idle.value(), kern.value(), user.value())
        prev, self._cpu = self._cpu, cur
        if prev is None:
            return None
        # 注意：GetSystemTimes 的 kernel 时间**包含** idle 时间！
        # 真实忙碌 = kernel + user - idle；分母 = kernel + user。
        # 旧写法 (kernel+user)/(kernel+user+idle) 把 idle 算了两遍，占用率严重虚高。
        d_kern = cur[1] - prev[1]
        d_user = cur[2] - prev[2]
        d_idle = cur[0] - prev[0]
        total = d_kern + d_user
        if total <= 0:
            return None
        pct = 100.0 * max(0, total - d_idle) / total
        return max(0.0, min(100.0, pct))

    # ---------------------------------------------------------- 磁盘
    def _disk_raw(self):
        try:
            h = k32.CreateFileW("\\\\.\\PhysicalDrive0", 0, 0x1 | 0x2, None, 3, 0, None)
            INVALID = wintypes.HANDLE(-1).value
            if not h or (INVALID is not None and h == INVALID):
                return None
            try:
                buf = DISK_PERFORMANCE()
                ret = wintypes.DWORD(0)
                ok = k32.DeviceIoControl(h, _IOCTL_DISK_PERFORMANCE, None, 0,
                                         byref(buf), ctypes.sizeof(buf),
                                         byref(ret), None)
                if not ok:
                    return None
                # QueryTime/ReadTime/WriteTime 都是 100ns 累计值
                return (buf.BytesRead, buf.BytesWritten, buf.QueryTime,
                        buf.ReadTime, buf.WriteTime)
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None

    def disk_percent(self) -> Optional[float]:
        """磁盘忙碌百分比。

        正确算法：DISK_PERFORMANCE 的 ReadTime/WriteTime 是「设备忙」的
        100ns 累计值，QueryTime 是「开机至今」的 100ns 累计值 ——
        两者相除即忙碌率。早前用「字节数 / 时间戳」是量纲错误（结果恒近 0），
        导致 algo 的重负载判定永远不因磁盘触发。
        """
        cur = self._disk_raw()
        prev, self._disk = self._disk, cur
        if prev and cur:
            d_q = cur[2] - prev[2]                       # 经过的 100ns 时间
            d_busy = (cur[3] - prev[3]) + (cur[4] - prev[4])   # 设备忙的 100ns
            if d_q > 0:
                if d_busy <= 0:
                    return 0.0
                pct = 100.0 * d_busy / d_q
                return max(0.0, min(100.0, pct))
            return 0.0
        # 回退：按时间估算（拿不到硬件计数时用进程 I/O 速率代替）
        return None

    def fallback_disk_busy(self, procs: dict, elapsed: float,
                           foreground_pid: Optional[int] = None) -> Optional[float]:
        """无硬件计数时的兜底：统计所有进程的 I/O 增量速率，估算忙碌度。
        elapsed 为距上次调用的秒数。"""
        if elapsed <= 0:
            return None
        total_delta = 0
        cur = {}
        for exe, pid in list(procs.items())[:400]:
            try:
                h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION_IO, False, int(pid))
                if not h:
                    continue
                io = IO_COUNTERS()
                if k32.GetProcessIoCounters(h, byref(io)):
                    cur[pid] = (io.ReadTransferCount, io.WriteTransferCount)
                k32.CloseHandle(h)
            except Exception:
                continue
        prev = self._last_proc_io
        self._last_proc_io = cur
        if prev:
            for pid, (r, w) in cur.items():
                if pid in prev:
                    pr, pw = prev[pid]
                    total_delta += max(0, r - pr) + max(0, w - pw)
        if not prev:
            return None
        # 经验换算：持续 >8MB/s 视为磁盘基本忙满
        mbps = total_delta / elapsed / (1024.0 * 1024.0)
        return max(0.0, min(100.0, mbps / 8.0 * 100.0))

    # ---------------------------------------------------------- 网络
    def net_percent(self, elapsed: float) -> Optional[float]:
        """网络吞吐折算的忙碌百分比（GetIfTable，纯 ctypes、无子进程）。

        口径与磁盘兜底一致：持续 8 MB/s 折算为 100%，即 HEAVY_NET=60%
        约对应 4.8 MB/s 的持续下载（大文件/更新）。
        首次调用无基线返回 None；计数器回绕按 0 处理。
        """
        cur = net_bytes_total()
        prev, self._net = self._net, cur
        if cur is None or prev is None:
            return None
        d = cur - prev
        if d <= 0:
            return 0.0                  # 32 位计数器回绕或无流量
        elapsed = max(float(elapsed or 0.0), 0.001)
        mbps = d / elapsed / (1024.0 * 1024.0)
        return max(0.0, min(100.0, mbps / 8.0 * 100.0))


_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
_iphlpapi.GetIfTable.restype = wintypes.DWORD
_iphlpapi.GetIfTable.argtypes = [ctypes.c_void_p, POINTER(wintypes.ULONG), wintypes.BOOL]

MAXLEN_PHYSADDR = 8
MAXLEN_IFDESCR = 256
# 排除回环(24)/隧道(53)/无线广域虚拟口(131)：它们的计数不代表真实网络活动
_SKIP_IFTYPE = {24, 53, 131}


class MIB_IFROW(Structure):
    """iphlpapi GetIfTable 的一行（ifdef.h 原始布局，全 DWORD/WCHAR/BYTE，自然对齐）"""
    _fields_ = [
        ("wszName", wintypes.WCHAR * 256),
        ("dwIndex", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwMtu", wintypes.DWORD),
        ("dwSpeed", wintypes.DWORD),
        ("dwPhysAddrLen", wintypes.DWORD),
        ("bPhysAddr", ctypes.c_ubyte * MAXLEN_PHYSADDR),
        ("dwAdminStatus", wintypes.DWORD),
        ("dwOperStatus", wintypes.DWORD),
        ("dwLastChange", wintypes.DWORD),
        ("dwInOctets", wintypes.DWORD),
        ("dwInUcastPkts", wintypes.DWORD),
        ("dwInNUcastPkts", wintypes.DWORD),
        ("dwInDiscards", wintypes.DWORD),
        ("dwInErrors", wintypes.DWORD),
        ("dwInUnknownProtos", wintypes.DWORD),
        ("dwOutOctets", wintypes.DWORD),
        ("dwOutUcastPkts", wintypes.DWORD),
        ("dwOutNUcastPkts", wintypes.DWORD),
        ("dwOutDiscards", wintypes.DWORD),
        ("dwOutErrors", wintypes.DWORD),
        ("dwOutQLen", wintypes.DWORD),
        ("dwDescrLen", wintypes.DWORD),
        ("bDescr", ctypes.c_ubyte * MAXLEN_IFDESCR),
    ]


_IFROW_SIZE = ctypes.sizeof(MIB_IFROW)


def net_bytes_total() -> Optional[int]:
    """所有物理网卡的收发字节总数（排除回环/隧道）；失败返回 None。"""
    try:
        size = wintypes.ULONG(0)
        # 首查必然返回 122 (ERROR_INSUFFICIENT_BUFFER)，同时在 size 里带回所需缓冲区大小
        _iphlpapi.GetIfTable(None, byref(size), False)
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(int(size.value) + 16)
        if _iphlpapi.GetIfTable(buf, byref(size), False) != 0:
            return None
        n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        total = 0
        for i in range(min(int(n), 256)):
            row = MIB_IFROW.from_address(ctypes.addressof(buf) + 4 + i * _IFROW_SIZE)
            if row.dwType in _SKIP_IFTYPE:
                continue
            total += int(row.dwInOctets) + int(row.dwOutOctets)
        return total
    except Exception:
        return None


_sampler = _LoadSampler()


def load_sample(procs: Optional[dict] = None, gpu_percent: Optional[float] = None) -> dict:
    """采集一次整机负载。返回可能含 None 的字段，交给 algo.LoadTracker 平滑。"""
    now = time.time()
    elapsed = now - _sampler._last_t if _sampler._last_t else 0.0
    _sampler._last_t = now

    cpu = _sampler.cpu_percent()
    disk = _sampler.disk_percent()
    if disk is None and procs:
        disk = _sampler.fallback_disk_busy(procs, elapsed)
    net = _sampler.net_percent(elapsed)
    return {"cpu": cpu, "disk": disk, "net": net, "gpu": gpu_percent}


# ------------------------------------------------------------- 游戏调度环境
# 文献依据（2026-10 检索）：
#  · 硬件加速 GPU 调度（HAGS）：把 GPU 命令队列调度交回 GPU 硬件，
#    降低 CPU 侧驱动开销 2~5% —— 游戏 CPU 瓶颈时这部分开销正是
#    Dynamic Boost 能从 CPU 手里抠给 GPU 的瓦数。
#  · Windows 游戏模式：抑制后台更新/通知抢占，减少帧时间抖动。
#  · GameDVR 后台录制（Game Bar）：开启时持续做硬件编码，占 GPU/编码器
#    与一部分 CPU，属于「白给的功耗」，关掉可提升帧数并降低整机功耗。
# 这些项都需要重启/注销才完全生效，且属于用户系统设置，
# 因此程序只检测、只建议，不擅自改注册表。
_GAME_REG = (
    ("hags", winreg.HKEY_LOCAL_MACHINE,
     r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode",
     "硬件加速 GPU 调度(HAGS)"),
    ("game_mode", winreg.HKEY_CURRENT_USER,
     r"Software\Microsoft\GameBar", "AutoGameModeEnabled",
     "Windows 游戏模式"),
    # 2026-10-05 修正：后台录制的真正开关是 AppCaptureEnabled。
    # 旧代码读 System\GameConfigStore\GameDVR_Enabled —— 该键在 Win11 上默认就是 1，
    # 只表示「Game DVR 功能可用」，并不代表后台录制在跑，读它会稳定误报「已开启」。
    ("game_dvr", winreg.HKEY_CURRENT_USER,
     r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled",
     "GameDVR 后台录制"),
)

# 同一项目可能有多个历史键名：任一为真即视为开启
_GAME_ALT = {
    "game_dvr": ((winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
                  "HistoricalCaptureEnabled"),),
}


def _reg_read(hive, path, name):
    """读一个 DWORD。键/值不存在、或存的不是数字（第三方软件写字符串）时
    一律返回 None —— 调用方 int(raw) 才不会炸。"""
    try:
        with winreg.OpenKey(hive, path) as k:
            v = winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except Exception:
        return None


def gaming_env() -> dict:
    """只读检测游戏相关系统设置，返回 {'items': [...], 'hints': [...]}。

    读取失败/键不存在一律视为「默认」——Windows 11 上键缺失常代表
    使用系统默认值，不强行判断为关闭，避免给出错误建议。
    """
    out = {"items": [], "hints": [], "hints_short": []}
    for key, hive, path, name, label in _GAME_REG:
        raw = _reg_read(hive, path, name)
        if raw is None:
            for hive2, path2, name2 in _GAME_ALT.get(key, ()):
                raw = _reg_read(hive2, path2, name2)
                if raw is not None:
                    break
        if key == "game_dvr":
            # 只有「显式打开」才算开启；键缺失 = 默认就是关的
            state = "已开启" if raw else "未开启"
            note = ("持续后台录制，白占 GPU 编码器与 CPU" if raw
                    else ("未设置，系统默认关闭" if raw is None else ""))
        elif raw is None:
            state, note = "系统默认", (
                "未手动改过；Win11 24H2 起在支持的显卡上默认开启"
                if key == "hags" else "未手动改过；Win11 默认开启")
        elif key == "hags":
            state = "已开启" if int(raw) == 2 else "已关闭"
            note = "" if int(raw) == 2 else "关闭时 CPU 侧图形开销更高"
        else:
            state = "已开启" if int(raw) else "已关闭"
            note = "" if int(raw) else "关闭时后台任务易抢帧"
        out["items"].append({"key": key, "label": label, "state": state,
                             "value": raw, "note": note})

    for it in out["items"]:
        if it["key"] == "hags" and it["state"] == "已关闭":
            out["hints"].append("开启「硬件加速 GPU 调度」可降低 CPU 图形开销 "
                                "2~5%（设置 → 系统 → 屏幕 → 显卡 / 默认图形设置），"
                                "省下的 CPU 预算会被 Dynamic Boost 转给独显")
            out["hints_short"].append("建议开启硬件加速GPU调度")
        if it["key"] == "game_mode" and it["state"] == "已关闭":
            out["hints"].append("开启「游戏模式」可减少后台任务抢占（设置 → 游戏 → 游戏模式）")
            out["hints_short"].append("建议开启游戏模式")
        if it["key"] == "game_dvr" and it["state"] == "已开启":
            out["hints"].append("关闭「后台录制(GameDVR)」可省下 GPU 编码器与部分 CPU 开销"
                                "（设置 → 游戏 → 捕获）")
            out["hints_short"].append("建议关闭后台录制(GameDVR)")
    return out


# ------------------------------------------------- 华硕「电池健康充电」阈值（只读）
# 2026-10-05 标定：装了 MyASUS（ASUS System Control Interface v3）后，华硕会把
# 充电上限镜像到 HKLM 注册表，普通权限可读、开销 ≈ 0。写入侧三条通道全部不通：
#   · \\.\ATKACPI 裸 IOCTL 0x0022240C → DeviceIoControl err=1（功能不支持）
#   · root\WMI 的 AsusAtkWmi_WMNB.DEVS(0x00120057, ...) → 该类 0 个实例，调用是空操作
#   · 直接改 HKLM 注册表 → 非管理员，拒绝访问
# ⇒ 程序只「读 + 建议 + 展示」，设定动作交给 MyASUS（其服务以 SYSTEM 权限写 EC）。
_ASUS_CHARGE_KEYS = (
    # 新机型实测命中，值 = 100
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\ASUS\ASUS System Control Interface\AsusOptimization\ASUS Keyboard Hotkeys",
     "ChargingRate"),
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\ASUS\ASUS System Control Interface\AsusOptimization", "ChargingRate"),
    # 老机型用的另一套键名
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ASUS\ASUS_Optimize_Setting",
     "Battery_HealthCharging"),
)

# 华硕的充电上限语义：60 / 80 / 100 三个档（部分老机型只有 60 与 100）
_CHARGE_MODE = {
    100: ("full", "满容量模式"),
    80: ("balanced", "平衡模式"),
    60: ("lifespan", "长效使用模式"),
}


def has_myasus() -> bool:
    """是否装了 MyASUS（ASUS PC Assistant 这个 Appx 包）——用于给建议时决定措辞"""
    for hive, path in (
        (winreg.HKEY_CURRENT_USER,
         r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
         r"\AppModel\Repository\Packages"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Appx\AppxAllUserStore\Applications"),
    ):
        try:
            k = winreg.OpenKey(hive, path)
        except Exception:
            continue
        try:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    if "asuspcassistant" in winreg.EnumKey(k, i).lower():
                        return True
                except Exception:
                    continue
        finally:
            k.Close()
    return False


# ------------------------------------------------- 风扇 / 散热状态
# 2026-10-05 二次标定（参考 G-Helper 的 ATKACPI 协议）：
#   · **ATKACPI 通道可用**（此前失败是漏了 DeviceInit 握手）：普通权限直读
#     CPU/GPU 风扇档位、CPU/GPU 温度、ASUS 性能模式、独显 TGP —— 0.02ms/次、零子进程。
#   · 注册表镜像（AsusOptimization / AsusDiagnosis）作为补充：风扇数量、电池循环。
#   · **写入侧被 ASUS 服务托管**：风扇曲线/性能模式写 DEVS 返回成功但回读立刻还原
#     ⇒ 只读，档位仍须用户在 MyASUS / Armoury Crate 里改。
_FAN_KEY = (winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\ASUS\ASUS System Control Interface\AsusOptimization"
            r"\ASUS Keyboard Hotkeys")
_DIAG_KEY = (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\ASUS\ASUS System Control Interface\AsusDiagnosis")

FAN_LABEL = {0: "平衡", 1: "增强", 2: "静音", 3: "全速", 4: "手动"}


def fan_status() -> dict:
    """读风扇/散热状态。优先 ATKACPI 直读，注册表镜像只作补充。

    返回 {available, source, fan_cpu, fan_gpu, temp_cpu, temp_gpu, perf_mode,
          perf_label, overdrive, overdrive_supported, gpu_base_w, gpu_var_w,
          fan_count, cycles, label, hint, raw}
    注意：风扇值单位是**档位百分比(0~120)**，不是 RPM（本机无 RPM 读数通道，
    AsusHWMonitorWMI 需要管理员才拿得到实例）。
    """
    out = {"available": False, "source": "", "fan_cpu": None, "fan_gpu": None,
           "temp_cpu": None, "temp_gpu": None, "perf_mode": None, "perf_label": "",
           "overdrive": None, "overdrive_supported": None,
           "gpu_base_w": None, "gpu_var_w": None,
           "fan_count": None, "cycles": None, "quiet_fan_supported": None,
           "overboost": None, "thermal_sensor_limit": None,
           "label": "风扇信息不可用", "hint": "", "raw": {}, "acpi": False}
    # 1) ATKACPI 直读（首选）
    try:
        from . import atkacpi
        a = atkacpi.get()
        if a is not None:
            out["acpi"] = True
            out["source"] = "ACPI"
            out["fan_cpu"] = a.fan_level("cpu")
            out["fan_gpu"] = a.fan_level("gpu")
            out["temp_cpu"] = a.cpu_temp()
            out["temp_gpu"] = a.gpu_temp()
            pm = a.perf_mode()
            out["perf_mode"] = pm
            out["perf_label"] = FAN_LABEL.get(pm, "") if pm is not None else ""
            out["overdrive"] = a.overdrive()
            out["overdrive_supported"] = a.overdrive_supported()
            out["gpu_base_w"] = a.read_val("gpu_power_base")
            out["gpu_var_w"] = a.read_val("gpu_power_var")
            out["available"] = True
    except Exception:
        pass

    # 2) 注册表镜像（补充：风扇数量 / 循环次数 / 我的华硕风扇档开关）
    try:
        hive, path = _FAN_KEY
        for k in ("QuietFan", "QuietFanDC", "QuietFanSupported", "FanOverboost",
                  "ThermalSensor", "ThermalSensorLimit", "DesktopFan",
                  "DynamicPerformanceThreshold"):
            v = _reg_read(hive, path, k)
            if v is not None:
                out["raw"][k] = v
        hive2, path2 = _DIAG_KEY
        for k in ("fanCounts", "CycleCount", "BATSOH_Percent"):
            v = _reg_read(hive2, path2, k)
            if v is not None:
                out["raw"][k] = v
    except Exception:
        pass
    if out["raw"]:
        out["available"] = True
        out["source"] = out["source"] or "注册表"
    for key, reg in (("quiet_fan_supported", "QuietFanSupported"),
                     ("overboost", "FanOverboost"),
                     ("thermal_sensor_limit", "ThermalSensorLimit"),
                     ("fan_count", "fanCounts"), ("cycles", "CycleCount")):
        try:
            v = out["raw"].get(reg)
            out[key] = int(v) if v is not None else None
        except Exception:
            out[key] = None

    # 3) 一行短文本 + 建议
    parts = []
    if out["fan_cpu"] is not None:
        parts.append("CPU 风扇 %d%%" % out["fan_cpu"])
    if out["fan_gpu"] is not None:
        parts.append("GPU 风扇 %d%%" % out["fan_gpu"])
    if out["temp_cpu"] is not None:
        parts.append("CPU %.0f℃" % out["temp_cpu"])
    if out["perf_label"]:
        parts.append("华硕性能模式：%s" % out["perf_label"])
    if not parts:
        n = out["fan_count"] or 0
        if n:
            parts.append("%d 风扇" % n)
        if out["overboost"]:
            parts.append("超增压已开")
    out["label"] = " · ".join(parts) or "风扇信息可用"

    hints = []
    if out["acpi"]:
        hints.append("风扇转速为档位百分比（本机无 RPM 读数通道），直读自 ATKACPI")
    if out["quiet_fan_supported"] == 0:
        hints.append("MyASUS 未提供风扇档位开关（QuietFanSupported=0）")
    if out["overboost"]:
        hints.append("FanOverboost 已开启：风扇更激进，噪音更高")
    out["hint"] = "；".join(hints)
    return out


def asus_charge_limit() -> dict:
    """读华硕充电上限（注册表镜像值）。只读、零特权、无子进程。

    返回 {supported, value, mode, mode_cn, label, myasus}
      value : 100 / 80 / 60；None = 读不到（非华硕机或从未设置过）
      label : 给面板用的一行短文本
    """
    out = {"supported": False, "value": None, "mode": None,
           "mode_cn": None, "label": "不可用", "myasus": False}
    try:
        out["myasus"] = has_myasus()
    except Exception:
        pass
    for hive, path, name in _ASUS_CHARGE_KEYS:
        raw = _reg_read(hive, path, name)
        if raw is None:
            continue
        try:
            v = int(raw)
        except Exception:
            continue
        if not (1 <= v <= 100):
            continue
        mode, cn = _CHARGE_MODE.get(v, ("custom", "自定义 %d%%" % v))
        out.update({"supported": True, "value": v, "mode": mode, "mode_cn": cn,
                    "label": ("充电上限 %d%%（%s）" % (v, cn)) if v < 100
                             else "充电上限 100%（满容量，无保护）"})
        return out
    return out


# ------------------------------------------------------------- 硬件信息
def system_info() -> dict:
    info = {"cpu": "", "gpus": [], "model": "", "manufacturer": ""}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"],
                           capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        info["cpu"] = (r.stdout or b"").decode("utf-8", "ignore").strip()
    except Exception:
        pass
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "(Get-CimInstance Win32_VideoController).Name -join ' | '"],
                           capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        info["gpus"] = [g.strip() for g in (r.stdout or b"").decode("utf-8", "ignore").split("|") if g.strip()]
    except Exception:
        pass
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "$cs=Get-CimInstance Win32_ComputerSystem; \"$($cs.Manufacturer) $($cs.Model)\""],
                           capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        info["model"] = (r.stdout or b"").decode("utf-8", "ignore").strip()
    except Exception:
        pass
    return info
