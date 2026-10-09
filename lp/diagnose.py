# -*- coding: utf-8 -*-
"""自检用的小工具"""
from __future__ import annotations

import ctypes
import os

_sa_fields = [("nLength", ctypes.c_ulong), ("lpSecurityDescriptor", ctypes.c_void_p),
              ("bInheritHandle", ctypes.c_int)]
from ctypes import wintypes, Structure, c_ulong


class SECURITY_ATTRIBUTES(Structure):
    _fields_ = _sa_fields


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
                            wintypes.DWORD, wintypes.HANDLE]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def atk_available() -> bool:
    """GHelper 用的 \\\\.\\ATKACPI 通道是否可用（本机实测大多不可用，别当失败）"""
    try:
        h = k32.CreateFileW(r"\\.\ATKACPI", 0xC0000000, 0x3, None, 3, 0, None)
        if h and h != -1:
            k32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def running_from_exe() -> bool:
    return bool(getattr(os, "path", None)) and bool(__import__("sys").frozen if hasattr(__import__("sys"), "frozen") else False)
