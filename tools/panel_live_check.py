# -*- coding: utf-8 -*-
"""
端到端验证：真·exe 里点「打开」，看弹出来的到底是什么窗口、有没有内容。

流程：启动 exe → 等面板 → 广播"打开"命令 → 找可见窗口 → PrintWindow 截图
→ 统计非白像素比例（判断白屏）→ 打印窗口类名 → 干净退出。
"""
from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
import time
import zlib
from ctypes import POINTER, Structure, byref, c_void_p, wintypes

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
sys.path.insert(0, HERE)

EXE = os.path.join(HERE, "笔记本电源自适应.exe")
DETACHED, NEW_GROUP, NO_WINDOW = 0x00000008, 0x00000200, 0x08000000
WM_COMMAND = 0x0111
MENU_OPEN, MENU_QUIT = 1001, 1006

_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)


class RECT(Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class BITMAPINFOHEADER(Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_u.GetWindowDC.restype = wintypes.HDC
_u.GetWindowDC.argtypes = [wintypes.HWND]
_u.GetWindowRect.argtypes = [wintypes.HWND, POINTER(RECT)]
_u.PrintWindow.restype = wintypes.BOOL
_u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint]
_u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u.IsWindowVisible.argtypes = [wintypes.HWND]
_u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_g.CreateCompatibleDC.restype = wintypes.HDC
_g.CreateCompatibleDC.argtypes = [wintypes.HDC]
_g.CreateCompatibleBitmap.restype = c_void_p
_g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_g.DeleteObject.argtypes = [c_void_p]
_g.DeleteDC.argtypes = [wintypes.HDC]
_g.GetDIBits.restype = ctypes.c_int
_g.GetDIBits.argtypes = [wintypes.HDC, c_void_p, ctypes.c_uint, ctypes.c_uint,
                         c_void_p, c_void_p, ctypes.c_uint]
_g.SelectObject.restype = c_void_p
_g.SelectObject.argtypes = [wintypes.HDC, c_void_p]


def _png(path, w, h, bgra):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[(h - 1 - y) * w * 4:(h - y) * w * 4]
        for x in range(w):
            b, g, r, a = row[x * 4:x * 4 + 4]
            raw += bytes((r, g, b))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    out += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(out)


def grab(hwnd, path=None):
    """抓窗口像素，返回 (w, h, 非白像素比例, buf)"""
    rc = RECT()
    _u.GetWindowRect(hwnd, byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    hdc = _u.GetWindowDC(hwnd)
    mem = _g.CreateCompatibleDC(hdc)
    bmp = _g.CreateCompatibleBitmap(hdc, w, h)
    _g.SelectObject(mem, bmp)
    _u.PrintWindow(hwnd, mem, 2)          # PW_RENDERFULLCONTENT
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    _g.GetDIBits(mem, bmp, 0, h, buf, byref(bi), 0)
    _g.DeleteObject(bmp)
    _g.DeleteDC(mem)
    _u.ReleaseDC(hwnd, hdc)
    data = buf.raw
    nonwhite = 0
    total = 0
    for i in range(0, len(data) - 4, 4 * 37):        # 稀疏采样，够判断就行
        b, g, r = data[i], data[i + 1], data[i + 2]
        total += 1
        if not (r > 245 and g > 245 and b > 245):
            nonwhite += 1
    ratio = (nonwhite / total) if total else 0.0
    if path:
        _png(path, w, h, data)
    return w, h, ratio


def windows_of(pids):
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    out = []

    def cb(hwnd, lp):
        pid = wintypes.DWORD()
        _u.GetWindowThreadProcessId(hwnd, byref(pid))
        if pid.value in pids:
            n = _u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            _u.GetWindowTextW(hwnd, buf, n + 1)
            cbuf = ctypes.create_unicode_buffer(256)
            _u.GetClassNameW(hwnd, cbuf, 256)
            out.append((hwnd, pid.value, buf.value, cbuf.value,
                        bool(_u.IsWindowVisible(hwnd))))
        return True

    _u.EnumWindows(CB(cb), 0)
    return out


def pids_of_exe():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq 笔记本电源自适应.exe",
                              "/FO", "CSV", "/NH"], capture_output=True,
                             timeout=15).stdout.decode("mbcs", "replace")
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].startswith("笔记本电源自适应"):
            try:
                pids.append(int(parts[1]))
            except Exception:
                pass
    return pids


def main():
    for pid in pids_of_exe():
        h = _k.OpenProcess(0x0001, False, pid)
        if h:
            _k.TerminateProcess(h, 0)
            _k.CloseHandle(h)
    time.sleep(1)

    p = subprocess.Popen([EXE], creationflags=DETACHED | NEW_GROUP | NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("启动 pid=%d" % p.pid)
    import json
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ready = False
    for i in range(40):
        time.sleep(1)
        try:
            op.open("http://127.0.0.1:8753/api/status", timeout=3).read()
            print("面板就绪（%ds）" % (i + 1))
            ready = True
            break
        except Exception:
            pass
    if not ready:
        print("❌ 面板没起来")
        return 1
    time.sleep(2)
    pids = pids_of_exe()
    print("进程 pid 列表：%s" % pids)

    wins = windows_of(pids)
    print("窗口：%s" % [(hx, pid, title[:20], cls, vis) for hx, pid, title, cls, vis in wins])
    for hx, _pid, _t, _c, _v in wins:
        _u.PostMessageW(hx, WM_COMMAND, MENU_OPEN, 0)     # 广播"打开"
    print("已发送「打开」命令")

    target = None
    for i in range(20):
        time.sleep(0.5)
        for hx, _pid, _t, _c, vis in windows_of(pids):
            if vis and _u.IsWindowVisible(hx):
                r = RECT()
                _u.GetWindowRect(hx, byref(r))
                if r.right - r.left > 300:
                    target = (hx, _t, _c)
                    break
        if target:
            break
    if not target:
        print("❌ 没有弹出可见窗口")
        return 1
    hx, title, cls = target
    print("弹出窗口：hwnd=%s 标题=%r 类名=%s" % (hx, title, cls))
    time.sleep(1.5)
    w, h, ratio = grab(hx, os.path.join(HERE, "_panel_live.png"))
    print("截图 %dx%d 非白像素 %.1f%% → %s"
          % (w, h, ratio * 100, "有内容 ✔" if ratio > 0.02 else "疑似白屏 ❌"))

    # 退出
    for hx2, _pid, _t, _c, _v in windows_of(pids):
        _u.PostMessageW(hx2, WM_COMMAND, MENU_QUIT, 0)
    time.sleep(2.5)
    print("剩余进程：%s" % pids_of_exe())
    log = os.path.join(HERE, "exit.log")
    if os.path.exists(log):
        print("--- exit.log 末尾 ---")
        with open(log, encoding="utf-8") as f:
            print("".join(f.readlines()[-6:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
