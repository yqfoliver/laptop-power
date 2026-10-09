# -*- coding: utf-8 -*-
"""只读探测：本机可用的第三方硬件传感器数据源。

回答一个问题：除了我们已经在用的那几条通道（ATKACPI / PDH / NVML / ACPI 电池），
这台机器上还有没有别的、能免费拿到的传感器数据？

重点查三样：
  1. HwMonitor64.exe 到底是什么 —— 如果是 OpenHardwareMonitor 系，
     它会往 WMI 里注册 provider，那就是现成的免费数据源。
  2. WMI 里有没有硬件传感器命名空间（root\\OpenHardwareMonitor / LibreHardwareMonitor
     / WMI 里的 MSAcpi_ThermalZoneTemperature / Win32_TemperatureProbe）。
  3. 有没有其它共享内存类的数据源（AIDA64 / HWiNFO）。

只读，不写任何东西，不改任何配置。
"""
import ctypes
import os
import subprocess
import sys
from ctypes import byref, c_int, c_long, c_ulong, c_wchar_p, pointer, POINTER

CREATE_NO_WINDOW = 0x08000000


def _run(cmd, timeout=25):
    """安静地跑一条命令并返回 (ok, 输出)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        s = p.stdout.decode("mbcs", "replace")
        return (p.returncode == 0 and bool(s.strip())), s
    except Exception as e:
        return False, repr(e)


def exe_path(name):
    """用 WMIC 拿进程的可执行文件路径（比 PowerShell 轻，且不依赖 PS）。"""
    ok, s = _run(["wmic", "process", "where", "name='%s'" % name,
                  "get", "ExecutablePath", "/value"])
    if ok:
        for ln in s.splitlines():
            if ln.startswith("ExecutablePath="):
                v = ln.split("=", 1)[1].strip()
                if v:
                    return v
    return None


def probe_processes():
    print("=" * 66)
    print("1) 相关进程与它们的真实路径")
    print("=" * 66)
    for name in ("GamePP.exe", "GPPPlatform.exe", "GameSDK.exe",
                 "HwMonitor64.exe"):
        p = exe_path(name)
        print("  %-16s %s" % (name, p or "(取不到路径 / 未运行)"))
        if p and os.path.isdir(os.path.dirname(p)):
            d = os.path.dirname(p)
            try:
                dlls = [f for f in os.listdir(d)
                        if f.lower().endswith((".dll", ".sys"))]
            except Exception:
                dlls = []
            interesting = [f for f in dlls
                           if any(k in f.lower() for k in
                                  ("hardware", "monitor", "ring0", "sensor",
                                   "inpout", "winring", "wmi", "openhard"))]
            if interesting:
                print("      └─ 关键依赖: %s" % ", ".join(sorted(interesting)[:8]))
    print()


def probe_wmic_namespaces():
    """用 WMIC 探测硬件监控命名空间是否存在。"""
    print("=" * 66)
    print("2) WMI 传感器命名空间 / 类探测")
    print("=" * 66)
    targets = [
        ("root\\OpenHardwareMonitor", "Sensor"),
        ("root\\OpenHardwareMonitor", "Hardware"),
        ("root\\LibreHardwareMonitor", "Sensor"),
        ("root\\LibreHardwareMonitor", "Hardware"),
        ("root\\WMI", "MSAcpi_ThermalZoneTemperature"),
        ("root\\CIMV2", "Win32_TemperatureProbe"),
        ("root\\WMI", "MSAcpi_ThermalZoneTemperature"),
    ]
    for ns, cls in targets:
        ok, s = _run(["wmic", "/namespace:\\\\%s" % ns,
                      "path", cls, "get", "Identifier,Value", "/value"],
                     timeout=20)
        tail = ""
        if ok:
            ids = [l for l in s.splitlines() if l.startswith("Identifier=")]
            tail = "%d 个对象" % len(ids)
            if ids:
                tail += "  例: " + " | ".join(
                    x.split("=", 1)[1] for x in ids[:3])
        else:
            first = (s.strip().splitlines() or [""])[0][:60] if s.strip() else ""
            tail = "不可用  %s" % first
        print("  %-38s %-28s %s" % (ns, cls, tail))
    print()


def probe_shared_memory():
    """共享内存类数据源（AIDA64 / HWiNFO）是否存在。"""
    print("=" * 66)
    print("3) 共享内存数据源")
    print("=" * 66)
    fm = None
    try:
        fm = ctypes.WinDLL("kernel32")
        fm.OpenFileMappingW.argtypes = [c_ulong, c_int, c_wchar_p]
        fm.OpenFileMappingW.restype = ctypes.c_void_p
        fm.CloseHandle.argtypes = [ctypes.c_void_p]
    except Exception as e:
        print("  打开 kernel32 失败:", e)
        return

    for label in ("AIDA64_Sensor_Item", "Global\\AIDA64_Sensor_Item",
                  "HWiNFO_SENSORS_MAP", "Global\\HWiNFO_SENSORS_MAP",
                  "HWiNFO_SENSORS_SM2", "Global\\HWiNFO_SENSORS_SM2",
                  "RTSSSharedMemoryV2", "Global\\RTSSSharedMemoryV2"):
        h = fm.OpenFileMappingW(0xF001F, 0, label)
        print("  %-32s %s" % (label, "存在 ✔" if h else "不存在"))
        if h:
            fm.CloseHandle(ctypes.c_void_p(h))
    print()


def probe_pdh_temperature():
    """PDH 里有没有我们还没用的温度计数器。"""
    print("=" * 66)
    print("4) PDH 计数器里可用的温度 / 时钟项")
    print("=" * 66)
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        from lp import power as _power  # noqa
        pdh = ctypes.WinDLL("pdh")
        query = ctypes.c_void_p()
        pdh.PdhOpenQueryW(None, 0, byref(query))
        pdh.PdhEnumObjectsW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, c_wchar_p,
            POINTER(ctypes.c_ulong), c_ulong, c_int]
        # 用 wildcard 列出名字里带 Thermal / Temperature 的对象
        for want in (b"Thermal", b"Temperature"):
            pdh.PdhCloseQuery(query)
            print("  (枚举 %s)" % want.decode())
    except Exception as e:
        print("  PDH 枚举不可用:", e)
    print()


def main():
    probe_processes()
    probe_wmic_namespaces()
    probe_shared_memory()
    print("探测完成。以上全部只读。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
