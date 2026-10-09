# -*- coding: utf-8 -*-
"""纯 Python 生成托盘 ICO（不依赖 Pillow）"""
import ctypes
import os
import struct
import sys

SIZE = 32
buf = [[(0, 0, 0, 0) for _ in range(SIZE)] for _ in range(SIZE)]


def px(x, y, color):
    if 0 <= x < SIZE and 0 <= y < SIZE:
        buf[y][x] = (color[2], color[1], color[0], 255)          # BGRA


def rect(x0, y0, x1, y1, color, t=1):
    for k in range(t):
        for x in range(x0 + k, x1 - k + 1):
            px(x, y0 + k, color)
            px(x, y1 - k, color)
        for y in range(y0 + k, y1 - k + 1):
            px(x0 + k, y, color)
            px(x1 - k, y, color)


def fill(x0, y0, x1, y1, color):
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            px(x, y, color)


GREEN = (0x2E, 0x7D, 0x32)
LIGHT = (0x4C, 0xAF, 0x50)
DARK = (0x1B, 0x5E, 0x20)
GREY = (0x9E, 0x9E, 0x9E)
WHITE = (0xFF, 0xFF, 0xFF)

rect(4, 8, 27, 24, DARK, 2)
fill(28, 13, 30, 18, GREY)
fill(6, 14, 25, 22, GREEN)
for x in range(7, 26):
    px(x, 15, LIGHT)

for (x, y) in [(17, 11), (18, 11), (18, 15), (21, 15), (16, 21),
               (17, 17), (14, 17), (18, 11), (17, 11), (17, 15)]:
    px(x, y, WHITE)


def _pixel_bytes() -> bytes:
    out = bytearray()
    for y in range(SIZE - 1, -1, -1):          # DIB 自下而上
        for x in range(SIZE):
            b, g, r, a = buf[y][x]
            out += bytes((b, g, r, a))
    row = ((SIZE + 31) // 32) * 4              # AND 掩码，按 4 字节对齐
    out += bytes(row * SIZE)
    return bytes(out)


def make_ico_bytes(size: int = SIZE) -> bytes:
    payload = _pixel_bytes()
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(payload), 22)
    dib = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                      len(payload), 0, 0, 0, 0)
    return header + entry + dib + payload


# ---------------------------------------------------------- 浣熊图标（多尺寸）
_ICO_PATH = (os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "lp", "raccoon.ico")
             if getattr(sys, "frozen", False) else
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "raccoon.ico"))
_HICON = [0]
_u = None


def make_hicon() -> int:
    """浣熊 HICON（LoadImage 从 raccoon.ico 取最佳尺寸）；失败回退手绘电池"""
    if _HICON[0]:
        return _HICON[0]
    global _u
    if _u is None:
        _u = ctypes.WinDLL("user32", use_last_error=True)
        _u.LoadImageW.restype = ctypes.c_void_p
        _u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                  ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    if os.path.exists(_ICO_PATH):
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
        h = _u.LoadImageW(None, _ICO_PATH, IMAGE_ICON, 0, 0,
                          LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if h:
            _HICON[0] = int(h)
            return _HICON[0]
    # 回退：手绘电池
    buf = ctypes.create_string_buffer(make_ico_bytes(), 64)
    u2 = ctypes.WinDLL("user32")
    u2.CreateIconFromResource.restype = ctypes.c_void_p
    u2.CreateIconFromResource.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                          ctypes.c_uint, ctypes.c_int, ctypes.c_int]
    h = u2.CreateIconFromResource(buf, 64, 1, 0x00030000, 0, 0) or 0
    _HICON[0] = int(h)
    return _HICON[0]
