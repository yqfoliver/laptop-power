# -*- coding: utf-8 -*-
"""只读：穷举打开已知的第三方传感器共享内存，并区分"不存在"与"拒绝访问"。

这一点很关键 —— 早期探测只看 OpenFileMapping 返回 NULL 就判定"不存在"，
但拒绝访问(ERROR_ACCESS_DENIED=5)和真不存在(ERROR_FILE_NOT_FOUND=2)
含义完全不同：前者说明对象确实存在（比如由服务/提权进程创建），只是权限不够。

只读，不写任何东西。
"""
import ctypes
import sys

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

FILE_MAP_READ = 0x0004
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5

kernel32.OpenFileMappingW.argtypes = [ctypes.c_ulong, ctypes.c_int,
                                      ctypes.c_wchar_p]
kernel32.OpenFileMappingW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetFileSize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
kernel32.GetFileSize.restype = ctypes.c_ulong

# 已知会对外提供传感器共享内存的产品 / SDK 名
CANDIDATES = [
    # HWiNFO SDK v2（游戏加加目录里就有 HWiNFO64.dll，这条最有可能）
    "HWiNFO_SENSORS_MAP2", "Global\\HWiNFO_SENSORS_MAP2",
    "HWiNFO_SENSORS_SM2", "Global\\HWiNFO_SENSORS_SM2",
    # HWiNFO SDK v1（老式）
    "HWiNFO_SENSORS_MAP", "Global\\HWiNFO_SENSORS_MAP",
    # AIDA64
    "AIDA64_Sensor_Item", "Global\\AIDA64_Sensor_Item",
    # MSI Afterburner / RTSS
    "RTSSSharedMemoryV2", "Global\\RTSSSharedMemoryV2",
    "MAHMSharedMemory", "Global\\MAHMSharedMemory",
    # TechPowerUp GPU-Z
    "GPUZShMem", "Global\\GPUZShMem",
    # 游戏加加自己的（不确定，试探）
    "GPPSharedMem", "GamePPSharedMemory", "Global\\GPPSharedMemory",
    "GamePPSensorMem", "Global\\GamePPSensorMem",
]


def try_open(name):
    """返回 (是否成功, 说明)。"""
    h = kernel32.OpenFileMappingW(FILE_MAP_READ, 0, name)
    if h:
        try:
            hi = ctypes.c_ulong(0)
            sz = kernel32.GetFileSize(ctypes.c_void_p(h), ctypes.byref(hi))
            return True, "存在 ✔  (%d 字节)" % sz
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(h))
    err = ctypes.get_last_error()
    if err == ERROR_ACCESS_DENIED:
        return False, "★ 存在但拒绝访问（对象存在，权限不够）"
    if err == ERROR_FILE_NOT_FOUND:
        return False, "不存在"
    return False, "错误码 %d" % err


def main():
    print("=" * 74)
    print("第三方传感器共享内存探测（区分 不存在 / 拒绝访问）")
    print("=" * 74)
    found = []
    for n in CANDIDATES:
        ok, msg = try_open(n)
        mark = "  " if not ok else "→ "
        print("%s%-26s %s" % (mark, n, msg))
        if ok or "拒绝访问" in msg:
            found.append((n, msg))
    print()
    if found:
        print("命中 %d 条：" % len(found))
        for n, m in found:
            print("   %-28s %s" % (n, m))
    else:
        print("一条都没命中 —— 本机上没有可用的第三方传感器共享内存。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
