# -*- coding: utf-8 -*-
"""WebView2 面板诊断探针（独立脚本，不进 exe）。

背景：之前在用户桌面上开网页面板 = 白屏 + 进程消失。为了定位在不炸主程序的前提
下做体外诊断，本探针做三件事：

  1. **专用 STA 线程**：所有 WebView2 调用都在同一条线程上，且这条线程拥有
     一个【永久】消息泵（不是 show() 里泵 2 秒就走人）。这是 WebView2 的硬要求：
     回调与子窗口都要靠这条线程的消息循环活着。
  2. **逐步日志 + 心跳**：每一步写文件并 flush，进程若在回调里崩（访问违例），
     最后一条日志会停在崩的那一步 —— 否则这种崩溃是"无声消失"，无从查起。
  3. **屏幕抓像素**：用屏 DC 的 BitBlt 抓窗口区域（不是 PrintWindow）。
     PrintWindow 抓不到跨进程/合成层渲染的内容，会误判成白屏。

用法：
    python tools/wv2_probe.py [url]
默认目标：本机面板 http://127.0.0.1:8753/，服务没起则回退 web/index.html。
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
import zlib
from ctypes import POINTER, byref, c_int, c_void_p, wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)
_o = ctypes.WinDLL("ole32", use_last_error=True)

LOG_LINES: list = []


def log(s):
    line = "[%7.2fs] %s" % (time.time() - T0, s)
    LOG_LINES.append(line)
    print(line, flush=True)
    try:
        with open(os.path.join(ROOT, "tools", "wv2_probe.log"), "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


T0 = time.time()


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", c_int),
                ("biHeight", c_int), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", c_int),
                ("biYPelsPerMeter", c_int), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def _write_png(path, w, h, bgra):
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


_u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, c_void_p, c_void_p]
_u.ShowWindow.argtypes = [wintypes.HWND, c_int]
_u.ShowWindow.restype = wintypes.BOOL
_u.GetWindowRect.argtypes = [wintypes.HWND, POINTER(RECT)]
_u.GetDC.restype = wintypes.HDC
_u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_g.CreateCompatibleDC.restype = wintypes.HDC
_g.CreateCompatibleDC.argtypes = [wintypes.HDC]
_g.CreateCompatibleBitmap.restype = c_void_p
_g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, c_int, c_int]
_g.DeleteObject.argtypes = [c_void_p]
_g.DeleteDC.argtypes = [wintypes.HDC]
_g.SelectObject.restype = c_void_p
_g.SelectObject.argtypes = [wintypes.HDC, c_void_p]
_g.BitBlt.restype = wintypes.BOOL
_g.BitBlt.argtypes = [wintypes.HDC, c_int, c_int, c_int, c_int,
                      wintypes.HDC, c_int, c_int, wintypes.DWORD]
# GetDIBits 必须显式绑 argtypes：不绑的话参数按 c_int 传，句柄 >2^31 时会
# "int too long to convert"（时好时坏，因为句柄地址每次不同）。
_g.GetDIBits.restype = c_int
_g.GetDIBits.argtypes = [wintypes.HDC, c_void_p, ctypes.c_uint, ctypes.c_uint,
                         c_void_p, c_void_p, ctypes.c_uint]


def capture_screen(hwnd, path):
    """从屏 DC 抓窗口矩形——反映真正的合成结果"""
    rc = RECT()
    _u.GetWindowRect(hwnd, byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return 0, 0, 0.0
    hdc = _u.GetDC(0)
    mem = _g.CreateCompatibleDC(hdc)
    bmp = _g.CreateCompatibleBitmap(hdc, w, h)
    _g.SelectObject(mem, bmp)
    _g.BitBlt(mem, 0, 0, w, h, hdc, rc.left, rc.top, 0x00CC0020)   # SRCCOPY
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    _g.GetDIBits(mem, bmp, 0, h, buf, byref(bi), 0)
    _g.DeleteObject(bmp)
    _g.DeleteDC(mem)
    _u.ReleaseDC(0, hdc)
    data = buf.raw
    nonwhite = total = 0
    for i in range(0, len(data) - 4, 4 * 13):
        b, gg, r = data[i], data[i + 1], data[i + 2]
        total += 1
        if not (r > 245 and gg > 245 and b > 245):
            nonwhite += 1
    ratio = (nonwhite / total) if total else 0.0
    if path:
        _write_png(path, w, h, data)
    return w, h, ratio


def _server_alive() -> bool:
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        op.open("http://127.0.0.1:8753/api/status", timeout=2).read()
        return True
    except Exception:
        return False


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else None
    if not url:
        url = "http://127.0.0.1:8753/" if _server_alive() else \
            "file:///" + os.path.join(ROOT, "web", "index.html").replace("\\", "/")
    log("目标 URL = %s" % url)

    try:
        logfile = os.path.join(ROOT, "tools", "wv2_probe.log")
        open(logfile, "w", encoding="utf-8").close()
    except Exception:
        pass

    from lp.webview2panel import WebView2Panel
    data_dir = os.path.join(os.environ.get("LOCALAPPDATA", "."),
                            "LaptopPowerAuto", "webview2_probe")
    panel = WebView2Panel(url, data_dir)
    stop = threading.Event()
    result = {"ready": False}

    def worker():
        try:
            log("线程 %d 启动，CoInitializeEx ..." % _k.GetCurrentThreadId())
            hr = _o.CoInitializeEx(None, 0x2)
            log("  CoInitializeEx hr=0x%X" % (hr & 0xFFFFFFFF))
            log("  EnsureWindow ...")
            ok = panel._ensure_window()
            log("  EnsureWindow = %s (note=%s)" % (ok, panel._note))
            if not ok:
                return
            log("  ensure() 开始建立环境 ...")
            if not panel.ensure():
                log("  ensure() 失败: %s" % panel._note)
                return
            # 持续泵消息（这是关键：不是泵 2 秒就撒手）
            deadline = time.time() + 40
            while time.time() < deadline and not stop.is_set():
                panel._pump_wait(0.4)
                if panel.ready:
                    break
                if panel.failed:
                    break
            result["ready"] = bool(panel.ready)
            log("  ready=%s failed=%s note=%s" %
                (panel.ready, panel.failed, panel._note))
            if panel.ready:
                log("  ready -> ShowWindow")
                _u.ShowWindow(panel.hwnd, 5)
                log("  ShowWindow OK")
                _u.SetForegroundWindow(panel.hwnd)
                log("  SetForegroundWindow OK")
                _u.PostMessageW(panel.hwnd, 0x0005, 0, 0)   # WM_SIZE：触发 put_Bounds
                log("  posted WM_SIZE")
            # 就绪后继续泵 —— 渲染/输入都要靠这条循环
            _beat = [time.time()]
            while not stop.is_set():
                panel._pump_wait(0.3)
                if time.time() - _beat[0] > 1.5:
                    _beat[0] = time.time()
                    log("  pump 存活  t=%.1fs" % (time.time() - T0))
        except Exception as e:
            log("  worker 异常 %r" % e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    # 主线程等待，顺便每 1 秒打一次心跳；若进程在回调里崩溃，心跳会直接断开
    for i in range(50):
        time.sleep(1)
        if panel.ready or panel.failed:
            break
    time.sleep(4)          # 给页面加载与首帧渲染留时间

    if panel.ready:
        shot = os.path.join(ROOT, "tools", "wv2_probe.png")
        w, h, ratio = capture_screen(panel.hwnd, shot)
        log("抓屏 %dx%d 非白占比=%.3f  -> %s" %
            (w, h, ratio, "有内容 OK" if ratio > 0.02 else "疑似白屏 FAIL"))
        log("截图: %s" % shot)
    else:
        log("未就绪: ready=%s failed=%s note=%s" %
            (panel.ready, panel.failed, panel._note))

    time.sleep(1)
    stop.set()
    time.sleep(0.6)
    log("探针结束")


if __name__ == "__main__":
    main()
