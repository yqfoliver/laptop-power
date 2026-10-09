# -*- coding: utf-8 -*-
"""只读：把 Windows 对象命名空间里的 Section（共享内存）全列出来。

目的：确认本机有没有"别人已经算好、我们就地取用"的传感器共享内存
（HWiNFO SDK v2 用的就是 FileMapping Section，名字版本相关，猜不如列）。

只读操作，不开句柄 IO，不改任何东西。
"""
import ctypes
import sys
from ctypes import c_byte, c_ubyte, c_ulong, c_ushort, c_void_p, POINTER, Structure
from ctypes import c_long, byref, wintypes, c_char_p, c_wchar_p

ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

STATUS_MORE_ENTRIES = 0x00000105
STATUS_SUCCESS = 0

DIRECTORY_QUERY = 0x0001
OBJECT_DIRECTORY_INFORMATION_SIZE = 0x10000


class UNICODE_STRING(Structure):
    _fields_ = [("Length", c_ushort),
                ("MaximumLength", c_ushort),
                ("Buffer", c_wchar_p)]


class OBJECT_DIRECTORY_INFORMATION(Structure):
    _fields_ = [("Name", UNICODE_STRING),
                ("TypeName", UNICODE_STRING)]


class OBJECT_ATTRIBUTES(Structure):
    _fields_ = [("Length", c_ulong),
                ("RootDirectory", c_void_p),
                ("ObjectName", POINTER(UNICODE_STRING)),
                ("Attributes", c_ulong),
                ("SecurityDescriptor", c_void_p),
                ("SecurityQualityOfService", c_void_p)]


ntdll.NtOpenDirectoryObject.argtypes = [
    POINTER(c_void_p), c_ulong, POINTER(OBJECT_ATTRIBUTES)]
ntdll.NtOpenDirectoryObject.restype = c_long
ntdll.NtQueryDirectoryObject.argtypes = [
    c_void_p, c_void_p, c_ulong, c_ubyte, c_ubyte,
    POINTER(c_ulong), POINTER(c_ulong)]
ntdll.NtQueryDirectoryObject.restype = c_long
ntdll.NtClose.argtypes = [c_void_p]
ntdll.NtClose.restype = c_long

# 用 C 侧的 ULONG 字段需要正确的字节偏移，这里用原生 memset 缓冲区 + 手工解析太脆，
# 改为让 ctypes 直接以结构体数组方式解析。
_DINFO = OBJECT_DIRECTORY_INFORMATION


def _us(s):
    b = s.encode("utf-16-le") + b"\x00\x00"
    return UNICODE_STRING(len(s) * 2, len(s) * 2 + 2, s)


def enum_dir(path):
    """列出一个目录下的对象名 -> 类型名。"""
    us = UNICODE_STRING(len(path) * 2, len(path) * 2 + 2, path)
    oa = OBJECT_ATTRIBUTES()
    oa.Length = ctypes.sizeof(OBJECT_ATTRIBUTES)
    oa.RootDirectory = None
    oa.ObjectName = ctypes.pointer(us)
    oa.Attributes = 0
    h = c_void_p()
    st = ntdll.NtOpenDirectoryObject(byref(h), DIRECTORY_QUERY, byref(oa))
    if st != STATUS_SUCCESS:
        print("   [打开失败 0x%08X] %s" % (st & 0xFFFFFFFF, path))
        return []
    out = []
    try:
        buf = (c_byte * OBJECT_DIRECTORY_INFORMATION_SIZE)()
        idx = c_ulong(0)
        ret = c_ulong(0)
        restart = 1
        while True:
            # 参数顺序：ReturnSingleEntry=0，RestartScan 只有第一轮为 1。
            # 早期版本把 RestartScan 写成常量 1，结果每次都从头扫，
            # 永远只返回第一条，看起来像"目录是空的"。
            st = ntdll.NtQueryDirectoryObject(
                h, buf, OBJECT_DIRECTORY_INFORMATION_SIZE,
                0, restart, byref(idx), byref(ret))
            restart = 0
            if st != STATUS_SUCCESS:
                break
            info = ctypes.cast(buf, POINTER(_DINFO)).contents
            try:
                name = info.Name.Buffer
                tname = info.TypeName.Buffer
            except Exception:
                break
            if name:
                out.append((name, tname or "?"))
    finally:
        ntdll.NtClose(h)
    return out


KEY = ("hwinfo", "sensor", "aida", "rtss", "mabh", "gamepp", "hwmon",
       "monitor", "hardware", "presentmon", "gpp", "openhard", "libre")


def main():
    bases = ["\\BaseNamedObjects", "\\Sessions\\1\\BaseNamedObjects",
             "\\Sessions\\2\\BaseNamedObjects",
             "\\Sessions\\3\\BaseNamedObjects"]
    total = []
    for base in bases:
        items = enum_dir(base)
        if not items:
            continue
        print("=== %s （%d 个对象） ===" % (base, len(items)))
        sec = sorted(set(n.lower() for n, t in items if t == "Section"))
        print("   其中 Section: %d" % len(sec))
        hits = [n for n in sec if any(k in n for k in KEY)]
        total.extend(hits)
        if hits:
            print("   ★ 疑似传感器共享内存:")
            for n in hits:
                print("       " + n)
        print()
    if not total:
        print("没有发现任何名字里带 hwinfo/sensor/aida/rtss/monitor 的共享内存段。")
    else:
        print("汇总命中 %d 个。" % len(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
