# -*- coding: utf-8 -*-
"""系统托盘（ctypes 直调 shell32/user32，不依赖 pystray / Pillow）

用一个独立的隐藏窗口承载托盘图标与消息，
不去劫持 tkinter 主窗口的窗体过程，最稳。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes, byref, c_int, c_uint, c_void_p, POINTER, CFUNCTYPE, Structure
from ctypes import c_ubyte as _ubyte, c_long as _LONG

from .icon import make_hicon


def _trace(tag: str) -> None:
    """托盘链路诊断轨迹（默认关闭）。设 PYTRAY_DEBUG=<路径> 后记录关键事件，
    用于排查「右键没反应」这类只在真实进程出现的问题。"""
    p = os.environ.get("PYTRAY_DEBUG")
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), tag))
    except Exception:
        pass

WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_COMMAND = 0x0111
WM_HOTKEY = 0x0312
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0008
TPM_RETURNCMD = 0x0100        # 返回用户选中的菜单 ID（而非只回 TRUE）
TPM_NONOTIFY = 0x0010         # 不向 owner 窗口发通知消息（配合 RETURNCMD 用）
MF_STRING = 0x00000000
MF_CHECKED = 0x00000008
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_USER = 0x00000004

_u = ctypes.WinDLL("user32", use_last_error=True)
_s = ctypes.WinDLL("shell32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)   # 模块句柄在 kernel32 上

LRESULT = ctypes.c_ssize_t      # wintypes 里没有 LRESULT；64 位下为有符号字长


class NOTIFYICONDATA(Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.DWORD),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _ubyte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", c_void_p),
                ("cbClsExtra", c_int), ("cbWndExtra", c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HICON), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class _RECT(Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


WNDPROC = CFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, c_void_p, c_void_p)

# ---- 菜单项 ID（对外暴露给 App 处理）
MENU_OPEN = 1001
MENU_AUTO = 1002
MENU_RECHECK = 1003
MENU_BALANCED = 1004
MENU_ABOUT = 1005
MENU_QUIT = 1006
MENU_LEARN = 1007
MENU_AUTOSTART = 1008
MENU_SETTINGS = 1009

_u.CreateIconFromResource.restype = wintypes.HICON
_u.CreateIconFromResource.argtypes = [c_void_p, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, c_int, c_int]
_u.SetForegroundWindow.argtypes = [wintypes.HWND]
_u.SetForegroundWindow.restype = wintypes.BOOL
_u.GetCursorPos.argtypes = [POINTER(wintypes.POINT)]
_u.GetCursorPos.restype = wintypes.BOOL
_u.CreatePopupMenu.restype = wintypes.HMENU
_u.CreatePopupMenu.argtypes = []
_u.AppendMenuW.restype = wintypes.BOOL
_u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, c_void_p,
                           wintypes.LPCWSTR]
# 注意：TrackPopupMenu 是 **7 参数**（hMenu, uFlags, x, y, nReserved, hWnd, prcRect）。
# 早年按 6 参数调用（漏掉 nReserved），hWnd 被塞进 nReserved、真正的 hWnd 传成 NULL，
# 于是菜单一次都没弹出来、只留 err=1400 —— 症状就是「右键没反应」。
# 统一改用 5 参数的 TrackPopupMenuEx（无 nReserved，语义更清晰，实测可弹）。
_u.TrackPopupMenuEx.restype = ctypes.c_int
_u.TrackPopupMenuEx.argtypes = [wintypes.HMENU, wintypes.UINT, c_int, c_int,
                                wintypes.HWND, c_void_p]
_u.DestroyMenu.argtypes = [wintypes.HMENU]
_k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_k.GetModuleHandleW.restype = wintypes.HINSTANCE
_u.ShowWindow.argtypes = [wintypes.HWND, c_int]
_u.ShowWindow.restype = wintypes.BOOL
_u.DefWindowProcW.restype = LRESULT
_u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, c_void_p, c_void_p]
_u.GetWindowLongPtrW = getattr(_u, "GetWindowLongPtrW") if hasattr(_u, "GetWindowLongPtrW") else _u.GetWindowLongA
_u.GetWindowLongPtrW.restype = c_void_p
_u.GetWindowLongPtrW.argtypes = [wintypes.HWND, c_int]
_u.SetWindowLongPtrW = getattr(_u, "SetWindowLongPtrW") if hasattr(_u, "SetWindowLongPtrW") else _u.SetWindowLongA
_u.SetWindowLongPtrW.restype = c_void_p
_u.SetWindowLongPtrW.argtypes = [wintypes.HWND, c_int, c_void_p]
_u.RegisterClassW.restype = wintypes.ATOM
_u.CreateWindowExW.restype = wintypes.HWND
_u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                               c_int, c_int, c_int, c_int, wintypes.HWND, wintypes.HMENU,
                               wintypes.HINSTANCE, c_void_p]
_s.Shell_NotifyIconW.restype = wintypes.BOOL
_s.Shell_NotifyIconW.argtypes = [wintypes.UINT, POINTER(NOTIFYICONDATA)]


def load_icon_from_bytes(data: bytes) -> int:
    """
    从 ICO 原始字节创建 HICON。
    CreateIconFromResource(pbBits, cbBits, fIcon, dwVer, cxDesired, cyDesired) 共 6 参，
    最后两个用 0 = 按资源里自带的尺寸。
    """
    buf = ctypes.create_string_buffer(data, len(data))
    return int(_u.CreateIconFromResource(buf, len(data), 1, 0x00030000, 0, 0) or 0)


# 窗口类只注册一次，窗口过程必须是「模块级」的：早期把每个实例的 _wndproc
# 写进 WNDCLASS，RegisterClassW 第二次会因类已存在而失败（异常被吞），
# 于是第二个实例的窗口仍在用第一个实例的过程 → 消息全跑错对象。
# 正确做法：全局过程按 hwnd 查回实例（Win32 里就是 SetWindowLong(GWLP_USERDATA) 的套路）。
_INSTANCES = {}
_GLOBAL_PROC = None


def _global_wndproc(hwnd, msg, wparam, lparam):
    try:
        inst = _INSTANCES.get(int(hwnd))
        if inst is not None:
            return inst._wndproc(hwnd, msg, wparam, lparam)
    except Exception as e:
        _trace("global wndproc EXC %r" % e)
    return _u.DefWindowProcW(hwnd, msg, wparam, lparam)


class Tray:
    def __init__(self, on_command=None):
        self.on_command = on_command
        self.hwnd = 0
        self.nid = NOTIFYICONDATA()
        self.menu_provider = None      # callable -> [(id, text, checked)]
        self._proc = WNDPROC(self._wndproc)
        # 托盘图标句柄只创建一次并复用：早前每次 notify/set_tooltip 都新建
        # HICON 且从不销毁，切档通知频繁时会线性泄漏 GDI 句柄。
        self._hicon = 0
        self._taskbar_created = 0      # explorer 重启后重建图标用的消息 ID
        # —— 可测试性 ——
        self._test_mode = False        # True：_popup 只计数不真弹菜单
        self._popup_n = 0              # 右键菜单被请求的次数（测试断言用）

    def _icon(self) -> int:
        if not self._hicon:
            try:
                self._hicon = make_hicon()
            except Exception:
                self._hicon = 0
        return self._hicon

    # ---------------------------------------------------------- 消息
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if self._taskbar_created and msg == self._taskbar_created:
                # explorer 重启（崩溃/重装/注销）会清掉所有托盘图标，重新添加
                self._fill(self.nid, self.nid.szTip or "笔记本电源自适应")
                _s.Shell_NotifyIconW(0, byref(self.nid))       # NIM_ADD
                return 0
            if msg == WM_LBUTTONUP:
                # 关键：托盘回调消息（uCallbackMessage）注册的就是 0x0202，
                # Shell 对左键/右键点击**都发它**，真正的鼠标动作在 lparam 低 16 位。
                # 之前直接等 WM_RBUTTONUP 消息（永远不会来），右键菜单因此从未弹出。
                mouse = int(lparam or 0) & 0xFFFF
                _trace("callback msg=0x%X mouse=0x%X wparam=0x%X"
                       % (msg, mouse, int(wparam or 0)))
                if mouse == WM_RBUTTONUP or mouse == WM_CONTEXTMENU:
                    self._popup()
                elif mouse in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, 0):
                    self._dispatch(MENU_OPEN)
                return 0
            if msg == WM_HOTKEY:
                self._dispatch(MENU_OPEN)
                return 0
            if msg == WM_COMMAND:
                # wparam 低 16 位 = 菜单 ID，高 16 位 = 通知码。
                # 别用 ctypes.cast（回调里 wparam 是普通 int，cast 会抛异常被吞，
                # 症状就是"菜单弹出来了但点什么都没反应"）。
                mid = int(wparam or 0) & 0xFFFF
                _trace("command mid=%d" % mid)
                self._dispatch(int(mid))
                return 0
        except Exception as e:
            _trace("wndproc EXC %r" % e)
        return _u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _dispatch(self, mid: int) -> None:
        if self.on_command:
            try:
                self.on_command(mid)
            except Exception:
                pass

    def _popup(self) -> None:
        self._popup_n += 1
        if self._test_mode:
            return                    # 测试：只验证右键触发了菜单路径
        _trace("popup enter")
        items = []
        if callable(self.menu_provider):
            try:
                items = self.menu_provider() or []
            except Exception as e:
                _trace("menu_provider EXC %r" % e)
                items = [(MENU_OPEN, "打开", False), (MENU_QUIT, "退出", False)]
        fg = _u.SetForegroundWindow(self.hwnd)
        _trace("popup fg=%d err=%d" % (fg, ctypes.get_last_error()))
        menu = _u.CreatePopupMenu()
        _trace("popup menu=%s err=%d" % (menu, ctypes.get_last_error()))
        if not menu:
            return
        for mid, text, checked in items:
            _u.AppendMenuW(menu, MF_STRING | (MF_CHECKED if checked else 0), mid, text)
        # 菜单必须弹在鼠标位置（TPM_LEFTALIGN 传 0,0 会弹到屏幕左上角）
        pt = wintypes.POINT()
        _u.GetCursorPos(byref(pt))
        # TPM_RETURNCMD：直接拿到用户选中的菜单 ID，不再依赖 WM_COMMAND 回传
        # （WM_COMMAND 走隐藏窗口也能到，但 RETURNCMD 少一跳，更稳）
        flags = (TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_BOTTOMALIGN
                 | TPM_RETURNCMD | TPM_NONOTIFY)
        t0 = time.time()
        cmd = _u.TrackPopupMenuEx(menu, flags, pt.x, pt.y, self.hwnd, None)
        _trace("popup track cmd=%d ms=%.0f err=%d"
               % (cmd, (time.time() - t0) * 1000, ctypes.get_last_error()))
        if cmd:
            self._dispatch(int(cmd))
        # MSDN 要求的经典 workaround：不投这条，第二次右键菜单可能立刻消失
        try:
            _u.PostMessageW(self.hwnd, 0x0000, 0, 0)   # WM_NULL
        except Exception:
            pass
        _u.DestroyMenu(menu)

    # ---------------------------------------------------------- 生命周期
    def start(self, tip: str = "笔记本电源自适应") -> bool:
        cls = "LaptopPowerTrayWin"
        # explorer 重启后 Shell 会广播这条消息，托盘图标需要自我重建
        try:
            _u.RegisterWindowMessageW.restype = wintypes.UINT
            _u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
            self._taskbar_created = int(_u.RegisterWindowMessageW("TaskbarCreated"))
        except Exception:
            self._taskbar_created = 0
        wc = WNDCLASSW()
        global _GLOBAL_PROC
        if _GLOBAL_PROC is None:
            _GLOBAL_PROC = WNDPROC(_global_wndproc)
        wc.lpfnWndProc = ctypes.cast(_GLOBAL_PROC, c_void_p)
        wc.hInstance = _k.GetModuleHandleW(None)
        wc.lpszClassName = cls
        hinst = _k.GetModuleHandleW(None)
        try:
            _u.RegisterClassW(byref(wc))   # 已注册过也没关系，下面照样能建窗
        except Exception:
            pass
        self.hwnd = _u.CreateWindowExW(0, cls, "", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            return False
        _INSTANCES[int(self.hwnd)] = self   # 供全局过程按 hwnd 反查实例
        _u.ShowWindow(self.hwnd, 0)
        self._fill(self.nid, tip)
        ok = bool(_s.Shell_NotifyIconW(0, byref(self.nid)))   # NIM_ADD
        return ok

    def _fill(self, nid: NOTIFYICONDATA, tip: str, alt_hwnd: int = None) -> None:
        """注意结构体字段名是 hWnd（大小写敏感），写成 hwnd 会被静默忽略"""
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = int(alt_hwnd if alt_hwnd else (self.hwnd or 0))
        nid.uID = 0x7A11
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_LBUTTONUP
        nid.hIcon = self._icon()
        nid.szTip = (tip or "笔记本电源自适应")[:127]
        nid.szInfo = ""
        nid.szInfoTitle = ""
        nid.dwInfoFlags = 0

    def set_tooltip(self, text: str) -> bool:
        nid = NOTIFYICONDATA()
        self._fill(nid, text)
        return bool(_s.Shell_NotifyIconW(1, byref(nid)))

    def notify(self, title: str, text: str) -> bool:
        nid = NOTIFYICONDATA()
        self._fill(nid, "笔记本电源自适应")
        nid.hWnd = self.hwnd
        nid.uID = 0x7A11
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO
        nid.szTip = "笔记本电源自适应"
        nid.szInfo = (text or "")[:255]
        nid.szInfoTitle = (title or "")[:63]
        nid.dwInfoFlags = NIIF_INFO | NIIF_USER
        nid.uTimeoutOrVersion = 3500
        return bool(_s.Shell_NotifyIconW(1, byref(nid)))

    def hide(self) -> None:
        try:
            nid = NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
            nid.hWnd = self.hwnd
            nid.uID = 0x7A11
            nid.uFlags = 0
            _s.Shell_NotifyIconW(2, byref(nid))
        except Exception:
            pass

    def destroy(self) -> None:
        self.hide()
        try:
            _INSTANCES.pop(int(self.hwnd), None)
        except Exception:
            pass
        if self._hicon:
            try:
                _u.DestroyIcon(self._hicon)
            except Exception:
                pass
            self._hicon = 0
