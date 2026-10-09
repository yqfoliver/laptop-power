# -*- coding: utf-8 -*-
"""原生桌面面板 —— 纯 ctypes + GDI 的 Win32 窗口（零依赖、真原生、深色卡片风）

设计目标（对齐 GHelper / 微软电脑管家的体验）：
  * 独立原生窗口，不依赖浏览器；托盘左键/热键打开
  * 同进程直读 Manager.status()，不走 HTTP，零延迟
  * 关闭窗口 = 隐藏（守护进程不退出），只有托盘菜单「退出」才退出
  * 每秒后台线程采样刷新，UI 线程只画图不阻塞
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import (CFUNCTYPE, POINTER, Structure, byref, c_int, c_ssize_t,
                    c_uint, c_void_p, wintypes)

# ---------------------------------------------------------------- 常量
WM_APP_REFRESH = 0x8000 + 0x123
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_MOUSELEAVE = 0x02A3
WM_TIMER = 0x0113
WM_ERASEBKGND = 0x0014
WM_PAINT = 0x000F
WM_CLOSE = 0x0010

SW_HIDE = 0
SW_RESTORE = 9
SW_SHOW = 5
SWP_NOZORDER = 0x0004
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
TME_LEAVE = 0x0002
DT_WORDBREAK = 0x0010
DT_SINGLELINE = 0x0020
DT_VCENTER = 0x0004
DT_CENTER = 0x0001
DT_RIGHT = 0x0002
DT_END_ELLIPSIS = 0x8000
DT_NOPREFIX = 0x0800
TRANSPARENT = 1
PS_SOLID = 0
FW_NORMAL = 400
FW_SEMIBOLD = 600
FW_BOLD = 700
CLEARTYPE_QUALITY = 5
DEFAULT_CHARSET = 1
LOGPIXELSX = 88
LOGPIXELSY = 90
SRCCOPY = 0x00CC0020
HWND_TOP = 0
GWL_STYLE = -16

LRESULT = c_ssize_t
WNDPROC = CFUNCTYPE(LRESULT, wintypes.HWND, c_uint, c_void_p, c_void_p)

_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)


def _rgb(r, g, b):
    return (r & 0xFF) | ((g & 0xFF) << 8) | ((b & 0xFF) << 16)


class RECT(Structure):
    _fields_ = [("left", c_int), ("top", c_int), ("right", c_int), ("bottom", c_int)]


class WNDCLASSW(Structure):
    _fields_ = [("style", c_uint), ("lpfnWndProc", c_void_p),
                ("cbClsExtra", c_int), ("cbWndExtra", c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HICON), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class TRACKMOUSEEVENT(Structure):
    _fields_ = [("cbSize", c_uint), ("dwFlags", c_uint),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", c_uint)]


class PAINTSTRUCT(Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                ("rcPaint", RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


# ---- user32
_u.RegisterClassW.restype = wintypes.ATOM
_u.CreateWindowExW.restype = wintypes.HWND
_u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                               c_int, c_int, c_int, c_int, wintypes.HWND, wintypes.HMENU,
                               wintypes.HINSTANCE, c_void_p]
_u.DefWindowProcW.restype = LRESULT
_u.DefWindowProcW.argtypes = [wintypes.HWND, c_uint, c_void_p, c_void_p]
_u.ShowWindow.argtypes = [wintypes.HWND, c_int]
_u.SetForegroundWindow.argtypes = [wintypes.HWND]
_u.IsIconic.argtypes = [wintypes.HWND]
_u.IsWindowVisible.argtypes = [wintypes.HWND]
_u.SetTimer.argtypes = [wintypes.HWND, c_void_p, c_uint, c_void_p]
_u.KillTimer.argtypes = [wintypes.HWND, c_void_p]
_u.InvalidateRect.argtypes = [wintypes.HWND, POINTER(RECT), wintypes.BOOL]
_u.PostMessageW.argtypes = [wintypes.HWND, c_uint, c_void_p, c_void_p]
_u.GetClientRect.argtypes = [wintypes.HWND, POINTER(RECT)]
_u.TrackMouseEvent.argtypes = [POINTER(TRACKMOUSEEVENT)]
_u.GetCursorPos.argtypes = [POINTER(wintypes.POINT)]
_u.ScreenToClient.argtypes = [wintypes.HWND, POINTER(wintypes.POINT)]
_u.BeginPaint.argtypes = [wintypes.HWND, POINTER(PAINTSTRUCT)]
_u.BeginPaint.restype = wintypes.HDC
_u.EndPaint.argtypes = [wintypes.HWND, POINTER(PAINTSTRUCT)]
_u.GetDC.restype = wintypes.HDC
_u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_u.GetWindowLongPtrW = getattr(_u, "GetWindowLongPtrW", _u.GetWindowLongW)
_u.SetWindowLongPtrW = getattr(_u, "SetWindowLongPtrW", _u.SetWindowLongW)
_u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, c_int, c_int, c_int, c_int, c_uint]
_u.DrawTextW.restype = c_int
_u.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, c_int, POINTER(RECT), c_uint]
_u.LoadCursorW.restype = wintypes.HICON
_u.LoadCursorW.argtypes = [wintypes.HINSTANCE, c_void_p]   # 第二参是 MAKEINTRESOURCE
_u.SetProcessDPIAware.restype = wintypes.BOOL
_u.SetWindowTextW.restype = wintypes.BOOL
_u.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
_u.SystemParametersInfoW.argtypes = [c_uint, c_uint, c_void_p, c_uint]
_u.AdjustWindowRect.restype = wintypes.BOOL
_u.AdjustWindowRect.argtypes = [POINTER(RECT), wintypes.DWORD, wintypes.BOOL]
_k.GetModuleHandleW.restype = wintypes.HINSTANCE

ROOT_DIR = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ---- gdi32
for fn, rest, args in (
    ("CreateSolidBrush", wintypes.HBRUSH, [c_uint]),
    ("CreatePen", wintypes.HPEN, [c_int, c_int, c_uint]),
    ("SelectObject", c_void_p, [wintypes.HDC, c_void_p]),
    ("DeleteObject", wintypes.BOOL, [c_void_p]),
    ("DeleteDC", wintypes.BOOL, [wintypes.HDC]),
    ("CreateCompatibleDC", wintypes.HDC, [wintypes.HDC]),
    ("CreateCompatibleBitmap", wintypes.HBITMAP, [wintypes.HDC, c_int, c_int]),
    ("BitBlt", wintypes.BOOL, [wintypes.HDC, c_int, c_int, c_int, c_int,
                               wintypes.HDC, c_int, c_int, c_uint]),
    ("Rectangle", wintypes.BOOL, [wintypes.HDC, c_int, c_int, c_int, c_int]),
    ("RoundRect", wintypes.BOOL, [wintypes.HDC, c_int, c_int, c_int, c_int, c_int, c_int]),
    ("SetTextColor", c_uint, [wintypes.HDC, c_uint]),
    ("SetBkMode", c_int, [wintypes.HDC, c_int]),
    ("CreateFontW", wintypes.HFONT,
     [c_int, c_int, c_int, c_int, c_int, c_uint, c_uint, c_uint,
      c_uint, c_uint, c_uint, c_uint, c_uint, wintypes.LPCWSTR]),
    ("GetDeviceCaps", c_int, [wintypes.HDC, c_int]),
):
    f = getattr(_g, fn)
    f.restype = rest
    if args:
        f.argtypes = args

# ---------------------------------------------------------------- 配色
C_BG = _rgb(24, 25, 30)
C_CARD = _rgb(35, 37, 43)
C_CARD_HI = _rgb(46, 48, 56)
C_TRACK = _rgb(52, 54, 62)
C_TXT = _rgb(232, 234, 240)
C_SUB = _rgb(152, 158, 170)
C_DIM = _rgb(110, 115, 126)
C_GREEN = _rgb(96, 205, 128)
C_BLUE = _rgb(84, 150, 245)
C_RED = _rgb(240, 96, 96)
C_ORANGE = _rgb(240, 170, 84)
C_TEAL = _rgb(84, 200, 190)
C_PURPLE = _rgb(158, 128, 245)

MODE_COLOR = {
    "gaming": C_RED, "saver": C_GREEN,
    "office": C_BLUE, "battery": C_TEAL, "balanced": C_PURPLE,
}
LOAD_STATE_CN = {"heavy": "重负载", "light": "空闲", "normal": "正常"}

BTN_H = 40

# 逻辑尺寸（1x 基准），实际窗口 = 这些值 × DPI 缩放，并夹到工作区内
TOTAL_W = 432
TOTAL_H = 818


def _lo_word(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def fmt_min(m) -> str:
    """分钟数 -> 「3 小时 12 分」"""
    try:
        m = int(m)
    except Exception:
        return "—"
    if m <= 0:
        return "—"
    if m < 60:
        return "%d 分钟" % m
    return "%d 小时 %02d 分" % (m // 60, m % 60)


class _Btn:
    __slots__ = ("bid", "rect", "hover")

    def __init__(self, bid, rect):
        self.bid = bid
        self.rect = rect
        self.hover = False


class NativePanel:
    """原生面板窗口。用法：panel.show() 打开/聚焦；窗口关闭只是隐藏。"""

    def __init__(self, mgr, web_url: str = ""):
        self.mgr = mgr
        self.web_url = web_url
        self.hwnd = 0
        self._proc = WNDPROC(self._wndproc)
        self._snap: dict = {}
        self._sampling = False
        self._samp_lock = threading.Lock()
        self._btns: list[_Btn] = []
        self._hover = None            # 当前 hover 的 bid
        self._down = None
        self._tracked = False
        self._fonts: dict = {}
        self._scale = 1.0
        self._brushes: dict = {}

    # ------------------------------------------------------ 对外
    def show(self) -> bool:
        if self.hwnd and _u.IsWindowVisible(self.hwnd):
            _u.SetForegroundWindow(self.hwnd)
            return True
        if self.hwnd:
            if _u.IsIconic(self.hwnd):
                _u.ShowWindow(self.hwnd, SW_RESTORE)
            else:
                _u.ShowWindow(self.hwnd, SW_SHOW)
            _u.SetForegroundWindow(self.hwnd)
            _u.PostMessageW(self.hwnd, WM_APP_REFRESH, 0, 0)
            return True
        return self._create()

    def close(self) -> None:
        if self.hwnd:
            _u.DestroyWindow(self.hwnd)
            self.hwnd = 0

    # ------------------------------------------------------ 创建
    def _create(self) -> bool:
        try:
            _u.SetProcessDPIAware()      # 注意：在 user32，不在 gdi32
        except Exception:
            pass
        cls = "LaptopPowerPanelWin"
        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = ctypes.cast(self._proc, c_void_p)
        wc.hInstance = _k.GetModuleHandleW(None)
        wc.hCursor = _u.LoadCursorW(None, 32512) if hasattr(_u, "LoadCursorW") else None
        try:
            from .icon import make_hicon
            wc.hIcon = make_hicon()          # 浣熊图标：标题栏 + 任务栏
        except Exception:
            wc.hIcon = None
        wc.lpszClassName = cls
        _u.RegisterClassW(byref(wc))

        hdc = _u.GetDC(None)
        dpi = _g.GetDeviceCaps(hdc, LOGPIXELSX) or 96
        _u.ReleaseDC(None, hdc)

        # 缩放 = DPI 比例，但绝不超出桌面工作区（本机 200% 缩放 + 1504px 工作区，
        # 若按 250% 放大会把底部按钮顶到屏幕外面去）
        work = RECT()
        avail_h, avail_w = 1080, 1920
        try:
            if _u.SystemParametersInfoW(0x0030, 0, byref(work), 0):   # SPI_GETWORKAREA
                avail_h = max(600, work.bottom - work.top)
                avail_w = max(800, work.right - work.left)
        except Exception:
            pass

        _wstyle = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX

        def win_size(scale):
            """给定的内容缩放 -> 整个窗口的物理尺寸（含标题栏/边框）"""
            def S2(v):
                return int(v * scale)
            r = RECT(0, 0, S2(TOTAL_W), S2(TOTAL_H))
            _u.AdjustWindowRect(byref(r), _wstyle, False)
            return (r.right - r.left, r.bottom - r.top)

        scale = max(1.0, dpi / 96.0)
        ww, hh = win_size(scale)
        for _ in range(4):
            if hh <= avail_h and ww <= avail_w:
                break
            scale = max(1.0, scale * min(avail_h / float(hh), avail_w / float(ww)))
            ww, hh = win_size(scale)
        self._scale = scale

        def S(v):
            return int(v * self._scale)

        w, h = ww, hh
        # 贴屏幕右下角：右缘贴工作区右边、底缘贴工作区底部。
        # SPI_GETWORKAREA 拿不到时退回 80,80 老位置。
        if work.right > work.left:
            px = max(work.left, work.right - w)
            py = max(work.top, work.bottom - h)
        else:
            px = py = 80
        self.hwnd = _u.CreateWindowExW(
            0, cls, "笔记本电源自适应",
            WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
            px, py, w, h, None, None, _k.GetModuleHandleW(None), None)
        if not self.hwnd:
            self.hwnd = 0
            return False
        _u.SetTimer(self.hwnd, 1, 1000, None)
        self._request_sample()
        _u.ShowWindow(self.hwnd, SW_SHOW)
        _u.SetForegroundWindow(self.hwnd)
        return True

    # ------------------------------------------------------ 采样
    def _request_sample(self) -> None:
        # check-then-set 必须原子（WM_TIMER 与采样线程收尾几乎同时发生时会双开）
        with self._samp_lock:
            if self._sampling or not self.hwnd:
                return
            self._sampling = True
        threading.Thread(target=self._sample_worker, daemon=True).start()

    def _sample_worker(self) -> None:
        try:
            self._snap = dict(self.mgr.status())
        except Exception:
            pass
        with self._samp_lock:
            self._sampling = False
        if self.hwnd:
            try:   # 标题栏带上当前档位，一眼可见
                label = (self._snap or {}).get("mode_label") or ""
                if label:
                    _u.SetWindowTextW(self.hwnd, "%s - 笔记本电源自适应" % label)
            except Exception:
                pass
            _u.PostMessageW(self.hwnd, WM_APP_REFRESH, 0, 0)

    # ------------------------------------------------------ 消息
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            # ctypes 回调会把 NULL 传成 None，后面 int(lparam) 会抛异常被吞掉，
            # 症状是「按下收到、抬起丢失」导致 _down 残留。统一归一化成整数。
            if lparam is None:
                lparam = 0
            if msg == WM_PAINT:
                self._paint(hwnd)
                return 0
            if msg == WM_TIMER:
                if _u.IsWindowVisible(hwnd):   # 隐藏时零采样零重绘（离电自省电）
                    self._request_sample()
                return 0
            if msg == WM_APP_REFRESH:
                _u.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_MOUSEMOVE:
                self._on_move(hwnd, lparam)
                return 0
            if msg == WM_MOUSELEAVE:
                self._tracked = False
                if self._hover is not None:
                    self._hover = None
                    _u.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_LBUTTONDOWN:
                bid = self._hit(_lo_word(lparam), _lo_word(int(lparam) >> 16))
                self._down = bid
                return 0
            if msg == WM_LBUTTONUP:
                bid = self._hit(_lo_word(lparam), _lo_word(int(lparam) >> 16))
                if bid is not None and bid == self._down:
                    self._click(bid)
                self._down = None
                return 0
            if msg == WM_CLOSE:
                _u.ShowWindow(hwnd, SW_HIDE)     # 关闭=隐藏，不退守护
                return 0
        except Exception:
            pass
        return _u.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------ 交互
    def _hit(self, x, y):
        for b in self._btns:
            r = b.rect
            if r.left <= x <= r.right and r.top <= y <= r.bottom:
                return b.bid
        return None

    def _on_move(self, hwnd, lparam):
        bid = self._hit(_lo_word(lparam), _lo_word(int(lparam) >> 16))
        if bid != self._hover:
            self._hover = bid
            _u.InvalidateRect(hwnd, None, False)
        if not self._tracked:
            tme = TRACKMOUSEEVENT()
            tme.cbSize = ctypes.sizeof(TRACKMOUSEEVENT)
            tme.dwFlags = TME_LEAVE
            tme.hwndTrack = hwnd
            if _u.TrackMouseEvent(byref(tme)):
                self._tracked = True

    def _click(self, bid: str) -> None:
        mgr = self.mgr
        if bid.startswith("p:"):
            key = bid[2:]

            def run():
                try:
                    mgr.set_manual(key)
                    mgr.cfg["auto_mode"] = False
                    from . import config
                    config.save(mgr.cfg)
                    mgr.apply(key, reason="面板手动选择")
                except Exception:
                    pass
            threading.Thread(target=run, daemon=True).start()
        elif bid == "auto":
            def run():
                try:
                    mgr.cfg["auto_mode"] = True
                    from . import config
                    config.save(mgr.cfg)
                    mgr.set_auto()
                except Exception:
                    pass
            threading.Thread(target=run, daemon=True).start()
        elif bid.startswith("t:"):
            key = bid[2:]

            def run():
                try:
                    mgr.set_thermal_mode(key)
                except Exception:
                    pass
            threading.Thread(target=run, daemon=True).start()
        elif bid == "recheck":
            def run():
                try:
                    if mgr.manual:
                        mgr.manual = None
                        mgr.cfg["manual_override"] = None
                    mgr.apply(mgr.detect(), reason="面板：重新判定")
                except Exception:
                    pass
            threading.Thread(target=run, daemon=True).start()
        elif bid == "web":
            def run():
                try:
                    from . import webui
                    webui.open_browser(self.web_url or "http://127.0.0.1:8753/")
                except Exception:
                    pass
            threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------ GDI 工具
    def _brush(self, color):
        b = self._brushes.get(color)
        if not b:
            b = _g.CreateSolidBrush(color)
            self._brushes[color] = b
        return b

    def _fill(self, hdc, color, l, t, r, btm, radius=0):
        old_brush = _g.SelectObject(hdc, self._brush(color))
        old_pen = _g.SelectObject(hdc, self._brush(color))   # 同色笔=无边框
        if radius > 0:
            _g.RoundRect(hdc, l, t, r, btm, radius, radius)
        else:
            _g.Rectangle(hdc, l, t, r, btm)
        _g.SelectObject(hdc, old_pen)
        _g.SelectObject(hdc, old_brush)

    def _bar(self, hdc, color, l, t, r, btm, pct):
        pct = max(0.0, min(1.0, pct or 0))
        self._fill(hdc, C_TRACK, l, t, r, btm, radius=(btm - t) // 2 or 1)
        if pct > 0.01:
            w = int((r - l) * pct)
            self._fill(hdc, color, l, t, l + w, btm, radius=(btm - t) // 2 or 1)

    def _font(self, hdc, size_px, weight=FW_NORMAL):
        key = (size_px, weight)
        f = self._fonts.get(key)
        if not f:
            f = _g.CreateFontW(-size_px, 0, 0, 0, weight, 0, 0, 0,
                               DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0,
                               "Microsoft YaHei UI")
            self._fonts[key] = f
        return _g.SelectObject(hdc, f)

    def _text(self, hdc, s, color, rect: RECT, size_px=12, weight=FW_NORMAL,
              flags=DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX):
        _g.SetBkMode(hdc, TRANSPARENT)
        _g.SetTextColor(hdc, color)
        old = self._font(hdc, size_px, weight)
        _u.DrawTextW(hdc, s, -1, byref(rect), flags)
        _g.SelectObject(hdc, old)

    # ------------------------------------------------------ 绘制
    def _paint(self, hwnd) -> None:
        ps = PAINTSTRUCT()
        hdc = _u.BeginPaint(hwnd, byref(ps))
        rc = RECT()
        _u.GetClientRect(hwnd, byref(rc))
        W, H = rc.right - rc.left, rc.bottom - rc.top

        mem = _g.CreateCompatibleDC(hdc)
        bmp = _g.CreateCompatibleBitmap(hdc, W, H)
        old_bmp = _g.SelectObject(mem, bmp)

        try:
            try:
                self._draw_ui(mem, W, H)
            except Exception:
                try:   # 绘制异常不静默：留日志防白屏排查无门
                    import traceback
                    with open(os.path.join(ROOT_DIR, "panel_ui_err.log"), "a",
                              encoding="utf-8") as f:
                        f.write(time.strftime("%F %T ") + traceback.format_exc())
                except Exception:
                    pass
                self._fill(mem, C_BG, 0, 0, W, H)
            _g.BitBlt(hdc, 0, 0, W, H, mem, 0, 0, SRCCOPY)
        finally:
            # GDI 对象清理必须放 finally：早前若兜底 _fill 也抛异常，
            # SelectObject/DeleteObject/DeleteDC 全被跳过 —— 每秒一次重绘
            # 会持续泄漏兼容 DC + 位图，最终耗尽 GDI 句柄配额导致全系统绘制异常。
            try:
                _g.SelectObject(mem, old_bmp)
                _g.DeleteObject(bmp)
                _g.DeleteDC(mem)
            except Exception:
                pass
        _u.EndPaint(hwnd, byref(ps))

    def _draw_ui(self, hdc, W, H) -> None:
        snap = self._snap or {}
        sc = self._scale

        def S(v):
            return int(v * sc)
        pad = S(16)
        self._fill(hdc, C_BG, 0, 0, W, H)

        # ------------------------------------------------ 顶栏
        self._text(hdc, "笔记本电源自适应", C_SUB, RECT(pad, S(8), W - pad, S(28)),
                   size_px=S(12))
        ac = snap.get("ac")
        bi = snap.get("battery_info") or {}
        bat = snap.get("battery")
        if bat is None:
            bat = bi.get("percent")
        pw = "⚡ 插电" if ac else "🔋 电池"
        if bat is not None:
            pw += " %d%%" % bat
        self._text(hdc, pw, C_SUB if ac else C_ORANGE,
                   RECT(W // 2, S(8), W - pad, S(28)), size_px=S(12),
                   flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)

        # ------------------------------------------------ 当前档位卡
        mode = snap.get("mode") or "balanced"
        accent = MODE_COLOR.get(mode, C_TEAL)
        y = S(38)
        card_h = S(88)
        self._fill(hdc, C_CARD, pad, y, W - pad, y + card_h, radius=S(10))
        label = snap.get("mode_label") or mode
        self._text(hdc, label, accent,
                   RECT(pad + S(16), y + S(8), W - pad - S(90), y + S(42)),
                   size_px=S(20), weight=FW_BOLD)
        auto = snap.get("auto", True)
        badge = "自动判定" if auto else "手动锁定"
        bc = C_GREEN if auto else C_ORANGE
        self._text(hdc, badge, bc,
                   RECT(W - pad - S(84), y + S(10), W - pad - S(10), y + S(32)),
                   size_px=S(11), flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)
        reason = snap.get("reason") or ""
        if reason:
            self._text(hdc, "原因：" + reason, C_SUB,
                       RECT(pad + S(16), y + S(44), W - pad - S(16), y + card_h - S(6)),
                       size_px=S(11), flags=DT_WORDBREAK | DT_NOPREFIX | DT_END_ELLIPSIS)

        # ------------------------------------------------ 档位按钮（6 个 = 3 列 × 2 行）
        profiles = [("gaming", "游戏"), ("office", "办公"),
                    ("battery", "续航"), ("saver", "极限省电"), ("balanced", "平衡")]
        btn_w = (W - pad * 2 - S(10) * 2) // 3
        by = y + card_h + S(10)
        self._btns = []
        for i, (key, txt) in enumerate(profiles):
            row, col = divmod(i, 3)
            l = pad + col * (btn_w + S(10))
            t = by + row * (S(BTN_H) + S(8))
            rect = RECT(l, t, l + btn_w, t + S(BTN_H))
            self._btns.append(_Btn("p:" + key, rect))
            active = (mode == key and not auto)
            hover = (self._hover == "p:" + key)
            color = MODE_COLOR.get(key, C_TEAL)
            bg = color if active else (C_CARD_HI if hover else C_CARD)
            self._fill(hdc, bg, rect.left, rect.top, rect.right, rect.bottom, radius=S(8))
            self._text(hdc, txt, C_BG if active else C_TXT,
                       RECT(rect.left + S(4), rect.top, rect.right - S(4), rect.bottom),
                       size_px=S(12), weight=FW_SEMIBOLD,
                       flags=DT_SINGLELINE | DT_VCENTER | DT_CENTER)

        # 恢复自动（全宽）
        t = by + S(BTN_H) * 2 + S(8) + S(8)
        rect = RECT(pad, t, W - pad, t + S(34))
        self._btns.append(_Btn("auto", rect))
        hover = self._hover == "auto"
        self._fill(hdc, C_CARD_HI if hover else C_CARD, rect.left, rect.top,
                   rect.right, rect.bottom, radius=S(8))
        auto_txt = "✓ 自动判定已开启" if auto else "恢复自动判定"
        self._text(hdc, auto_txt, C_GREEN if auto else C_TXT,
                   RECT(rect.left, rect.top, rect.right, rect.bottom),
                   size_px=S(12), weight=FW_SEMIBOLD,
                   flags=DT_SINGLELINE | DT_VCENTER | DT_CENTER)
        ay = t + S(34)

        # ------------------------------------------------ 电池 / 续航卡
        y = ay + S(10)
        card_h = S(130)
        self._draw_battery(hdc, W, pad, y, card_h, S, bi, ac,
                           snap.get("gpu_eco") or {})
        ly_end = y + card_h

        # ------------------------------------------------ 负载卡
        load = snap.get("load") or {}
        y = ly_end + S(10)
        card_h = S(80)
        self._fill(hdc, C_CARD, pad, y, W - pad, y + card_h, radius=S(10))
        self._text(hdc, "负载状态", C_TXT, RECT(pad + S(16), y + S(8), pad + S(100), y + S(28)),
                   size_px=S(12), weight=FW_SEMIBOLD)
        lstate = LOAD_STATE_CN.get(snap.get("load_state") or "", snap.get("load_state") or "—")
        heavy = snap.get("heavy")
        self._text(hdc, ("重负载占用中" if heavy else lstate),
                   C_ORANGE if heavy else (C_GREEN if lstate == "空闲" else C_SUB),
                   RECT(W - pad - S(140), y + S(8), W - pad - S(16), y + S(28)),
                   size_px=S(11), flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)
        busy = float(snap.get("busy") or 0)
        bar_y = y + S(32)
        self._bar(hdc, C_GREEN if not heavy else C_ORANGE, pad + S(16), bar_y,
                  W - pad - S(46), bar_y + S(8), busy / 100.0)
        self._text(hdc, "%d%%" % busy, C_SUB,
                   RECT(W - pad - S(44), bar_y - S(4), W - pad - S(12), bar_y + S(12)),
                   size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)

        def fmt(v):
            return "—" if v is None else "%d%%" % v
        sub = "CPU %s · 磁盘 %s · 网络 %s · 独显 %s" % (
            fmt(load.get("cpu")), fmt(load.get("disk")),
            fmt(load.get("net")), fmt(load.get("gpu")))
        self._text(hdc, sub, C_SUB, RECT(pad + S(16), y + S(50), W - pad - S(16), y + card_h - S(6)),
                   size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS)
        ay_end = y + card_h

        # ------------------------------------------------ 散热与风扇卡
        fan = snap.get("fan") or {}
        th = snap.get("thermal") or {}
        y = ay_end + S(10)
        card_h = S(98)
        self._fill(hdc, C_CARD, pad, y, W - pad, y + card_h, radius=S(10))
        self._text(hdc, "散热与风扇", C_TXT,
                   RECT(pad + S(16), y + S(8), pad + S(120), y + S(28)),
                   size_px=S(12), weight=FW_SEMIBOLD)
        pm_label = fan.get("perf_label") or ""
        self._text(hdc, ("华硕性能模式：%s" % pm_label) if pm_label else "华硕性能模式：—",
                   C_SUB, RECT(W // 2, y + S(8), W - pad - S(16), y + S(28)),
                   size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)

        def pct(v):
            return "—" if v is None else "%d%%" % v

        def deg(v):
            return "—" if v is None else "%.0f℃" % v

        line = "CPU 风扇 %s · GPU 风扇 %s · %s" % (
            pct(fan.get("fan_cpu")), pct(fan.get("fan_gpu")), deg(fan.get("temp_cpu")))
        self._text(hdc, line, C_SUB,
                   RECT(pad + S(16), y + S(30), W - pad - S(16), y + S(48)),
                   size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS)

        # 三档散热策略按钮
        tmodes = [("auto", "自动"), ("quiet", "静音"), ("perf", "性能")]
        bw = (W - pad * 2 - S(16) - S(8) * 2) // 3
        ty = y + S(56)
        cur_t = th.get("mode") or "auto"
        for i, (key, txt) in enumerate(tmodes):
            l = pad + S(16) + i * (bw + S(8))
            rect = RECT(l, ty, l + bw, ty + S(32))
            self._btns.append(_Btn("t:" + key, rect))
            active = (cur_t == key)
            hover = (self._hover == "t:" + key)
            col = C_TEAL if key == "quiet" else (C_RED if key == "perf" else C_BLUE)
            bg = col if active else (C_CARD_HI if hover else C_CARD_HI)
            self._fill(hdc, bg, rect.left, rect.top, rect.right, rect.bottom, radius=S(7))
            self._text(hdc, ("✓ " + txt) if active else txt,
                       C_BG if active else C_TXT,
                       RECT(rect.left, rect.top, rect.right, rect.bottom),
                       size_px=S(11), weight=FW_SEMIBOLD,
                       flags=DT_SINGLELINE | DT_VCENTER | DT_CENTER)
        ay_end2 = y + card_h

        # ------------------------------------------------ 功耗分配卡
        alloc = snap.get("alloc") or {}
        power = snap.get("power") or {}
        y = ay_end2 + S(10)
        card_h = S(152)
        self._fill(hdc, C_CARD, pad, y, W - pad, y + card_h, radius=S(10))
        self._text(hdc, "游戏功耗分配", C_TXT, RECT(pad + S(16), y + S(8), pad + S(120), y + S(28)),
                   size_px=S(12), weight=FW_SEMIBOLD)
        verdict = alloc.get("verdict_cn") or "—"
        self._text(hdc, verdict, C_BLUE if alloc.get("active") else C_SUB,
                   RECT(W - pad - S(170), y + S(8), W - pad - S(16), y + S(28)),
                   size_px=S(11), flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)

        cpu_w = power.get("cpu_w")
        gpu_w = power.get("gpu_w")
        env = alloc.get("envelope_w") or 110.0
        tgp = alloc.get("gpu_tgp_max") or 100.0
        cpu_temp = power.get("cpu_temp")
        gpu_temp = power.get("gpu_temp")
        gpu_util = power.get("gpu_util")

        def wtxt(v):
            return "—" if v is None else "%.1f W" % v

        line_y = y + S(36)
        self._line_kv_bar(hdc, pad, W, line_y, S,
                          "CPU", wtxt(cpu_w), (cpu_w or 0) / env, C_BLUE,
                          ("%.0f℃" % cpu_temp) if cpu_temp else "—")
        line_y += S(30)
        if ac:
            self._line_kv_bar(hdc, pad, W, line_y, S,
                              "独显", wtxt(gpu_w), (gpu_w or 0) / tgp, C_RED,
                              ("%.0f℃ · %d%%" % (gpu_temp, gpu_util or 0)) if gpu_temp else "—")
        else:
            # 离电：NVML 都不采样，画明确状态（Eco 生效时独显已在 ACPI 层断电）
            ge = (snap.get("gpu_eco") or {})
            glbl = "已断电" if ge.get("eco") == 1 else "休眠"
            self._line_kv_bar(hdc, pad, W, line_y, S,
                              "独显", glbl, 0.0, C_TRACK, "走 AMD 核显")
        line_y += S(30)
        # 整机 = 分配器口径（插电 soc+独显+开销；离电直接用电池放电真值）。
        # 注意 soc_w 已包含 CPU/核显，绝不能再把 cpu_w 加进来（重复计算）。
        total = alloc.get("system_w")
        if total is None:
            total = (power.get("soc_w") or 0) + (gpu_w or 0) + 12.0
        self._line_kv_bar(hdc, pad, W, line_y, S,
                          "整机", "%.1f W / %.0f W" % (total, env), total / env, C_PURPLE, "")
        line_y += S(30)
        areason = alloc.get("reason") or ""
        if not ac:
            areason = "离电中：分配器停用，整机功耗以电池遥测为准"
        env_short = alloc.get("env_short") or []
        if env_short and ac:
            areason = (areason + " · " + env_short[0]) if areason else env_short[0]
        if areason:
            self._text(hdc, areason, C_SUB,
                       RECT(pad + S(16), line_y, W - pad - S(16), y + card_h - S(6)),
                       size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS)

        # ------------------------------------------------ 底部按钮
        t = y + card_h + S(10)
        half = (W - pad * 2 - S(10)) // 2
        r1 = RECT(pad, t, pad + half, t + S(34))
        r2 = RECT(pad + half + S(10), t, W - pad, t + S(34))
        self._btns.append(_Btn("web", r1))
        self._btns.append(_Btn("recheck", r2))
        for bid, rect, txt in (("web", r1, "高级设置（网页）"), ("recheck", r2, "重新判定")):
            hover = self._hover == bid
            self._fill(hdc, C_CARD_HI if hover else C_CARD,
                       rect.left, rect.top, rect.right, rect.bottom, radius=S(8))
            self._text(hdc, txt, C_SUB,
                       RECT(rect.left, rect.top, rect.right, rect.bottom),
                       size_px=S(11), flags=DT_SINGLELINE | DT_VCENTER | DT_CENTER)

    # ------------------------------------------------------ 电池 / 续航卡
    def _draw_battery(self, hdc, W, pad, y, card_h, S, bi, ac, ge=None):
        self._fill(hdc, C_CARD, pad, y, W - pad, y + card_h, radius=S(10))
        self._text(hdc, "电池与续航", C_TXT,
                   RECT(pad + S(16), y + S(8), pad + S(120), y + S(28)),
                   size_px=S(12), weight=FW_SEMIBOLD)

        discharging = bool(bi.get("discharging"))
        rate = bi.get("rate_avg_w")
        if rate is None:
            rate = bi.get("rate_w")
        # 右上角状态
        if ac:
            if rate is not None and rate > 1.5:
                # 插着电却在放电 = 电源供不上（100W PD 打游戏），电池在补电 —— 红字告警
                chip, ccol = "电池补电 %.1f W" % rate, C_RED
            else:
                chip, ccol = ("充电中 %s%%" % bi["percent"]
                              if bi.get("charging") and bi.get("percent") is not None
                              else "已接电源"), C_GREEN
        elif discharging and rate:
            chip, ccol = "放电 %.1f W" % rate, C_ORANGE
        else:
            chip, ccol = bi.get("state_cn") or "电池", C_SUB
        self._text(hdc, chip, ccol,
                   RECT(W // 2, y + S(8), W - pad - S(16), y + S(28)),
                   size_px=S(11), flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)

        # 电量条
        pct = bi.get("percent")
        bar_y = y + S(32)
        left, right = pad + S(16), W - pad - S(60)
        if pct is None:
            self._bar(hdc, C_TRACK, left, bar_y, right, bar_y + S(9), 0)
        else:
            col = C_GREEN if pct > 50 else (C_ORANGE if pct > 20 else C_RED)
            self._bar(hdc, col, left, bar_y, right, bar_y + S(9), pct / 100.0)
        self._text(hdc, "—" if pct is None else "%d%%" % pct, C_TXT,
                   RECT(right + S(6), bar_y - S(5), W - pad - S(12), bar_y + S(14)),
                   size_px=S(12), weight=FW_SEMIBOLD,
                   flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)

        # 预计剩余 / 健康度
        eta = bi.get("eta_minutes")
        if discharging and eta:
            eta_txt = "还可用 %s" % fmt_min(eta)
            etc = C_GREEN if eta >= 120 else (C_ORANGE if eta >= 45 else C_RED)
        elif ac:
            eta_txt, etc = ("预计充满还需一会儿" if bi.get("charging") else "已充满，随时可拔"), C_SUB
        else:
            eta_txt, etc = "电量估算中…", C_SUB
        self._text(hdc, eta_txt, etc,
                   RECT(pad + S(16), y + S(50), W - pad - S(120), y + S(68)),
                   size_px=S(11), weight=FW_SEMIBOLD)
        hp = bi.get("health_pct")
        care = bi.get("care") or {}
        hp_txt = "健康度 " + ("—" if hp is None else "%.0f%%" % hp)
        if care.get("enabled") and care.get("score") is not None:
            hp_txt += " · 养护 %d 分" % care["score"]
        self._text(hdc, hp_txt, C_SUB,
                   RECT(W - pad - S(230), y + S(50), W - pad - S(16), y + S(68)),
                   size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)

        # 容量 / 刷新率
        cap, full = bi.get("capacity_mwh"), bi.get("full_mwh")
        if cap is not None and full:
            cap_txt = "%.1f / %.1f Wh" % (cap / 1000.0, full / 1000.0)
        else:
            cap_txt = "容量 —"
        self._text(hdc, cap_txt, C_SUB,
                   RECT(pad + S(16), y + S(70), W - pad - S(110), y + S(86)),
                   size_px=S(10))
        hz = bi.get("display_hz")
        hz_txt = "—" if hz is None else "屏幕 %d Hz" % hz
        if bi.get("display_changed"):
            hz_col = C_GREEN
        else:
            hz_col = C_SUB
        self._text(hdc, hz_txt, hz_col,
                   RECT(W - pad - S(130), y + S(70), W - pad - S(16), y + S(86)),
                   size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)

        # 离电这台的图形硬件（用户口径：不插电时走 AMD 核显）
        if ac:
            gtxt, gcol = "插电 · 功耗分配已启用", C_SUB
            # 华硕充电上限（装了 MyASUS 才读得到；100% = 满容量，等于没保护）
            cl = care.get("charge_limit") or {}
            cv = cl.get("value")
            if cv is not None:
                if cv >= 100:
                    gtxt += " · 充电上限 100%（无保护）"
                    gcol = C_ORANGE
                else:
                    gtxt += " · 充电上限 %d%%（%s）" % (cv, cl.get("mode_cn") or "自定义")
                    gcol = C_GREEN
        elif bi.get("display_changed"):
            gtxt = "离电图形：AMD 核显 · 屏幕已降到 %d Hz" % (hz or 60)
            gcol = C_GREEN
        else:
            ge = ge or {}
            tail = "独显已 ACPI 断电" if ge.get("eco") == 1 else "独显休眠"
            gtxt, gcol = "离电图形：AMD 核显（%s）" % tail, C_SUB
        # 亮度优化状态（封顶/空闲调暗）+ 充电降温状态
        if not ac and bi.get("brightness_applied") is not None:
            if bi.get("brightness_dimmed"):
                gtxt += " · 亮度 %d%%（空闲调暗）" % bi["brightness_applied"]
                gcol = C_ORANGE
            else:
                gtxt += " · 亮度≤%d%%" % bi["brightness_cap"]
        if ac and care.get("relief_on"):
            gtxt = "充电中机身偏热：CPU 上限暂降至 %d%% 降温" % (care.get("relief_cap") or 70)
            gcol = C_ORANGE
        self._text(hdc, gtxt, gcol,
                   RECT(pad + S(16), y + S(88), W - pad - S(16), y + S(106)),
                   size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS)

        # 耗电排行（离电时每 30s 采样 CPU 占用前列进程）
        tp = bi.get("top_procs") or []
        if tp and not ac:
            tip_txt = "耗电前列：" + " · ".join(
                "%s %.0f%%" % (nm[:-4] if nm.lower().endswith(".exe") else nm, p)
                for p, nm, _pid in tp)
            tip_col = C_ORANGE
        elif not ac:
            tip_txt = "耗电排行采样中…"
            tip_col = C_SUB
        else:
            # 插电时这一行空闲：用来放电池养护建议（文献：满充搁置/高温充电/深放）
            adv = (care.get("advice") or [])
            tip_txt = ("电池养护：" + adv[0]) if care.get("enabled") and adv else ""
            tip_col = C_ORANGE if tip_txt else C_SUB
        if tip_txt:
            self._text(hdc, tip_txt, tip_col,
                       RECT(pad + S(16), y + S(108), W - pad - S(16), y + card_h - S(6)),
                       size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS)

    def _line_kv_bar(self, hdc, pad, W, y, S, name, val_txt, pct, color, right_txt):
        x = pad + S(16)
        self._text(hdc, name, C_SUB, RECT(x, y, x + S(34), y + S(18)),
                   size_px=S(10))
        self._text(hdc, val_txt, C_TXT, RECT(x + S(36), y, x + S(170), y + S(18)),
                   size_px=S(10), weight=FW_SEMIBOLD)
        bar_l = x + S(178)
        bar_r = W - pad - S(16) - (S(96) if right_txt else 0)
        self._bar(hdc, color, bar_l, y + S(4), bar_r, y + S(13), pct)
        if right_txt:
            self._text(hdc, right_txt, C_SUB, RECT(bar_r + S(6), y, W - pad - S(16), y + S(18)),
                       size_px=S(10), flags=DT_SINGLELINE | DT_VCENTER | DT_RIGHT)


if __name__ == "__main__":  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lp import config
    from lp.manager import Manager

    m = Manager(config.load())
    p = NativePanel(m, "http://127.0.0.1:8753/")
    if not p._create():
        print("创建窗口失败")
        raise SystemExit(1)
    print("窗口已显示 5 秒（pid=%s）…" % os.getpid())

    class MSG(Structure):
        _fields_ = [("hWnd", c_void_p), ("message", c_uint), ("wParam", c_void_p),
                    ("lParam", c_void_p), ("time", c_uint), ("pt", wintypes.LONG * 2)]
    msg = MSG()
    end = time.time() + 5
    while time.time() < end:
        while _u.PeekMessageW(byref(msg), None, 0, 0, 1):
            _u.TranslateMessage(byref(msg))
            _u.DispatchMessageW(byref(msg))
        time.sleep(0.03)
    print("快照字段：", sorted((p._snap or {}).keys())[:12], "…")
    p.close()
