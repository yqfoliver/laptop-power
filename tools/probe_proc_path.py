# -*- coding: utf-8 -*-
"""只读：查进程的可执行路径 + 同目录依赖（纯 ctypes，不引入任何第三方包）。

用它回答一个具体问题：本机跑着的 `HwMonitor64.exe` 到底是哪家出品，
如果是 OpenHardwareMonitor 系的，它会在 WMI 里注册 provider，
那就是一个现成的、可免费复用的传感器数据源。
"""
import ctypes
import os
import sys
from ctypes import byref, c_int, c_ulong, c_void_p, c_wchar, sizeof
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_ulonglong),  # ULONG_PTR
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", c_wchar * MAX_PATH),
    ]


def all_processes():
    """返回 [(pid, 进程名)] —— сь 32/64 位都要能跑。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return []
    pe = PROCESSENTRY32W()
    pe.dwSize = sizeof(PROCESSENTRY32W)
    out = []
    try:
        if kernel32.Process32FirstW(snap, byref(pe)):
            while True:
                out.append((int(pe.th32ProcessID), pe.szExeFile))
                if not kernel32.Process32NextW(snap, byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return out


def path_of(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not h:
        return None
    try:
        buf = (c_wchar * 4096)()
        size = wintypes.DWORD(4096)
        ok = kernel32.QueryFullProcessImageNameW(
            c_void_p(h), 0, buf, byref(size))
        return buf.value if ok else None
    finally:
        kernel32.CloseHandle(c_void_p(h))


WANT = ("GamePP", "GPPPlatform", "GameSDK", "HwMonitor64", "Game", "PP")
KEY_DLL = ("hardware", "monitor", "ring0", "sensor", "inpout", "wmi",
           "openhard", "cpuid", "sdk", "lib")


def main():
    procs = all_processes()
    print("进程总数", len(procs))
    print()
    hits = [(pid, n) for pid, n in procs
            if any(k.lower() in n.lower() for k in
                   ("game", "hwmonitor", "gpp", "monitor", "hardware"))]
    for pid, name in sorted(set(hits)):
        p = path_of(pid)
        print("%-20s pid=%-7d" % (name, pid))
        print("   路径: %s" % (p or "(权限不足)"))
        if p and os.path.isfile(p):
            try:
                print("   大小: %d 字节   时间: %s"
                      % (os.path.getsize(p),
                         __import__("time").strftime(
                             "%Y-%m-%d %H:%M",
                             __import__("time").localtime(
                                 os.path.getmtime(p)))))
            except Exception:
                pass
        if p:
            d = os.path.dirname(p)
            try:
                files = os.listdir(d)
            except Exception:
                files = []
            hit = sorted(f for f in files
                         if any(k in f.lower() for k in KEY_DLL))
            if hit:
                print("   目录: %s" % d)
                print("   关键依赖(%d): %s" % (len(hit), ", ".join(hit[:10])))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
