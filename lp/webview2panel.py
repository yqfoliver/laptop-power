# -*- coding: utf-8 -*-
"""WebView2 原生面板窗口 —— 借鉴微软电脑管家的形态。

管家是「WPF + CoreWebView2」：一个独立程序窗口，里面是网页，没有地址栏、
不依赖外部浏览器。我们用 ctypes 手写 COM 复刻同样的形态：
自建 Win32 窗口 + WebView2 控制器 + 加载本地 127.0.0.1:8753 面板。

零第三方依赖：只用到系统自带的 WebView2 Runtime 和随包分发的
WebView2Loader.dll（微软官方 loader，NuGet: Microsoft.Web.WebView2）。
初始化失败时静默降级（由调用方回退 GDI 面板 / 浏览器），绝不弹错。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes, POINTER, byref, c_void_p, c_int, c_long, c_ulong, c_ulonglong

_u = ctypes.WinDLL("user32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)
_o = ctypes.WinDLL("ole32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)

_g.GetDeviceCaps.restype = c_int
_g.GetDeviceCaps.argtypes = [c_void_p, c_int]
_u.GetDC.restype = c_void_p
_u.GetDC.argtypes = [c_void_p]
_u.ReleaseDC.restype = c_int
_u.ReleaseDC.argtypes = [c_void_p, c_void_p]

LOGPIXELSX = 88
# 与网页(web/index.html)配套的逻辑尺寸 @1x：紧凑两列布局，内容实测 401 CSS px 高，
# 留余量到 424（约 200% DPI 下物理 848 + 标题栏）。宽 560 保证两列不挤。
# 改网页布局后用 tools 里 headless 探针量一次新高度，再同步这两个数。
PANEL_W = 560
# 2026-10-08：424 是旧版校准值，之后页面新增了「PD 余量 / 充电上限」等
# 状态行，实测内容 429px 起步（告警时更高）→ 底部溢出 5px，滚动条偶现。
# 提到 444 并配合 body overflow:hidden 双保险。
# 2026-10-10：新增「核显优先」卡片（84px），实测 wrap 底边 496 / 文档
# scrollHeight 506 ⇒ 再提到 512，同样用探针量过（tools/panel_height_check.py）。
# 2026-10-11：电池卡新增「充电上限」行 + 「本次充满 / 回养护」按钮（按场景养护）。
# 用**最长文案**量的（意图激活 + 校准到期 + mobile 场景长建议）：wrap 底边 591、
# 文档 601 → 定 604。注意 mock 要填进 battery_info.care（面板取的是
# state.battery_info），填顶层测出来会偏乐观 49px。
# 教训复述：网页每加一行都要重量一次，body overflow:hidden 会**裁掉**溢出
# 内容而不是出滚动条，静默丢信息比滚动条更糟。
PANEL_H = 604

HRESULT = c_long
S_OK = 0
COINIT_APARTMENTTHREADED = 0x2
E_NOINTERFACE = -2147467262      # 0x80004002
E_FAIL = -2147467259             # 0x80004005
# IID_IUnknown = {00000000-0000-0000-C000-000000000046} 的内存字节序
_IID_IUNKNOWN = b"\x00" * 8 + b"\xc0" + b"\x00" * 6 + b"\x46"
WM_SIZE = 0x0005
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_WINDOWPOSCHANGED = 0x0047
SW_SHOW = 5
SW_HIDE = 0
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
CW_USEDEFAULT = 0x80000000
WM_COPYDATA = 0x004A
WM_APP_EXIT = 0x8001
WAIT_OBJECT_0 = 0


class COPYDATASTRUCT(ctypes.Structure):
    # dwData 是 ULONG_PTR（指针宽度），用 c_void_p 保证 64 位下尺寸正确
    _fields_ = [("dwData", c_void_p), ("cbData", wintypes.DWORD),
                ("lpData", c_void_p)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


_u.AdjustWindowRect.restype = wintypes.BOOL
_u.AdjustWindowRect.argtypes = [POINTER(RECT), wintypes.DWORD, wintypes.BOOL]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", c_void_p),
                ("cbClsExtra", c_int), ("cbWndExtra", c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HICON), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             c_void_p, c_void_p)

_u.CreateWindowExW.restype = wintypes.HWND
_u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                               wintypes.DWORD, c_int, c_int, c_int, c_int,
                               wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE,
                               c_void_p]
_u.DefWindowProcW.restype = ctypes.c_ssize_t
_u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, c_void_p, c_void_p]
_u.GetClientRect.argtypes = [wintypes.HWND, POINTER(RECT)]
_u.MoveWindow.argtypes = [wintypes.HWND, c_int, c_int, c_int, c_int, wintypes.BOOL]
_u.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
_u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, c_void_p, wintypes.UINT]

# ---------------------------------------------------------------- COM 基础设施
_KEEP: list = []          # 防止 COM 对象/数组被 GC
_HWND_MAP: dict = {}      # hwnd -> WebView2Panel 实例（全局窗口过程反查用）


def _com_object(extra):
    """造一个 COM 对象：IUnknown 三件套 + 追加的方法（按 vtable 顺序）。
    返回对象地址（可直接作为接口指针传给 WebView2）。"""

    class Holder(ctypes.Structure):
        _fields_ = [("lpVtbl", c_void_p), ("refcount", c_int)]

    holder = Holder()
    holder.refcount = 1

    @ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, POINTER(c_void_p))
    def qi(this, riid, ppv):
        # 关键：绝不能对任何 IID 都返回 S_OK。
        # WebView2 内部会 QI 查询 IAgileObject / IMarshal 等接口，如果这里一律
        # 应答并把 handler 本身当接口指针交出去，对方会按【另一个接口的 vtable
        # 布局】来 CALL —— 轻则访问违例、整个进程静默消失（无窗口无报错，
        # 正是之前"点了打开程序就退出"的症状）。不认识的 IID 必须 E_NOINTERFACE。
        try:
            if riid and ctypes.string_at(riid, 16) == _IID_IUNKNOWN:
                ppv[0] = this
                holder.refcount += 1
                return S_OK
            ppv[0] = None
            return E_NOINTERFACE
        except Exception:
            return E_FAIL

    @ctypes.WINFUNCTYPE(c_ulong, c_void_p)
    def addref(this):
        holder.refcount += 1
        return holder.refcount

    @ctypes.WINFUNCTYPE(c_ulong, c_void_p)
    def release(this):
        holder.refcount -= 1
        return max(holder.refcount, 0)

    funcs = [qi, addref, release] + list(extra)
    arr = (c_void_p * len(funcs))(*[ctypes.cast(f, c_void_p) for f in funcs])
    holder.lpVtbl = ctypes.addressof(arr)
    _KEEP.append((holder, arr, funcs))       # 必须常驻，否则回调变成野指针
    return ctypes.addressof(holder)


def _vcall(ptr, idx, restype, argtypes):
    """按 vtable 下标调用 COM 方法"""
    if not ptr:
        return None
    try:
        lpvtbl = ctypes.cast(ptr, POINTER(c_void_p)).contents.value
        addr = ctypes.cast(lpvtbl, POINTER(c_void_p * 64)).contents[idx]
        if not addr:
            return None
        proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
        return ctypes.cast(addr, proto)
    except Exception:
        return None


def _set_dpi() -> bool:
    """把进程切到 PerMonitorV2。失败返回 False（ WebView2 会按 100% 渲染
    再被系统拉伸 —— 高 DPI 屏上就是「分辨率低/发糊」，所以不能静默吞掉）。"""
    try:
        fn = _u.SetProcessDpiAwarenessContext
        fn.restype = wintypes.BOOL
        fn.argtypes = [c_void_p]
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4（句柄是无符号指针宽度，
        # 必须用 c_void_p 包 0xFFFF...FC；传 c_ssize_t(-4) 会 TypeError）
        ok = fn(c_void_p(0xFFFFFFFFFFFFFFFC))
        if not ok:
            return False
        ctx = _u.GetThreadDpiAwarenessContext()
        _u.GetAwarenessFromDpiAwarenessContext.restype = c_int
        _u.GetAwarenessFromDpiAwarenessContext.argtypes = [c_void_p]
        return _u.GetAwarenessFromDpiAwarenessContext(ctx) >= 2
    except Exception:
        return False


def _global_wndproc(hwnd, msg, wparam, lparam):
    try:
        p = _HWND_MAP.get(int(hwnd))
        if p is not None:
            return p._on_msg(hwnd, msg, wparam, lparam)
    except Exception:
        pass
    return _u.DefWindowProcW(hwnd, msg, wparam, lparam)


_GLOBAL_WNDPROC = None


class WebView2Panel:
    """一个装 WebView2 的普通窗口，用来承载 web/index.html 面板。"""

    TITLE = "笔记本电源自适应"

    def __init__(self, url: str, data_dir: str = None):
        self.url = url
        self.hwnd = 0
        self.controller = 0
        self.webview = 0
        self.env = 0
        self.ready = False
        self.failed = False
        self._note = ""
        self._coinit = False
        self._nav_url = None
        self._loader = None
        self._pumping = False
        self._exiting = False
        self._data_dir = data_dir or os.path.join(
            os.environ.get("LOCALAPPDATA", "."), "LaptopPowerAuto", "webview2")
        self._scale = 1.0        # 窗口实际采用的 DPI 缩放（可能被工作区压小）
        self._clamped = False    # 内容高度是否已被工作区钳制（钳了让页面出滚动条）

    # ------------------------------------------------------------ 环境准备
    def _loader_path(self) -> str:
        base = getattr(sys, "_MEIPASS", None)
        cand = []
        if base:
            cand.append(os.path.join(base, "lp", "WebView2Loader.dll"))
            cand.append(os.path.join(base, "WebView2Loader.dll"))
        cand.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "WebView2Loader.dll"))
        for p in cand:
            if os.path.isfile(p):
                return p
        return ""

    @staticmethod
    def available() -> bool:
        try:
            from .webview2panel import WebView2Panel as _P  # noqa: F401
        except Exception:
            pass
        return True

    def _load_loader(self) -> bool:
        p = self._loader_path()
        if not p:
            self._note = "缺少 WebView2Loader.dll"
            return False
        try:
            self._loader = ctypes.WinDLL(p)
            fn = self._loader.CreateCoreWebView2EnvironmentWithOptions
            fn.restype = HRESULT
            fn.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, c_void_p, c_void_p]
            self._create_env = fn
            return True
        except Exception as e:
            self._note = "loader 加载失败 %r" % e
            return False

    # ------------------------------------------------------------ 窗口
    def _ensure_window(self) -> bool:
        if self.hwnd:
            return True
        global _GLOBAL_WNDPROC
        if not _set_dpi():
            # 高 DPI 屏上这意味着 WebView2 会按 100% 渲染再被系统拉伸（发糊）
            self._note = (self._note + " " if self._note else "") + \
                "DPI 感知设置失败，画面可能发糊"
        if _GLOBAL_WNDPROC is None:
            _GLOBAL_WNDPROC = WNDPROC(_global_wndproc)
        # 类名必须和 GDI 面板(native_panel)区分开：RegisterClassW 对已存在的类
        # 会失败并被我们吞掉，于是第二个窗口会挂到第一个窗口的 WNDPROC 上——
        # 结果就是「窗口出来了但什么都不画 / 消息跑错对象」。
        cls = "LaptopPowerPanelWV2"
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(_GLOBAL_WNDPROC, c_void_p)
        wc.hInstance = _k.GetModuleHandleW(None)
        wc.lpszClassName = cls
        wc.hbrBackground = ctypes.c_void_p(6)     # COLOR_WINDOW + 1
        try:
            _u.RegisterClassW(byref(wc))
        except Exception:
            pass
        # 尺寸与 GDI 面板同一套算法：逻辑 432x818 × DPI 缩放，超出工作区就
        # 整体缩小。旧版写死 980x800 物理 —— 在 200% DPI 屏上 CSS 视口只有
        # 490x400，网页又高又挤还出滚动条（用户反馈「太大、分辨率太低」）。
        # 样式也对齐 GDI：固定尺寸不可拉伸，更像电脑管家的小面板。
        wstyle = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX
        hdc = _u.GetDC(None)
        dpi = _g.GetDeviceCaps(hdc, LOGPIXELSX) or 96
        _u.ReleaseDC(None, hdc)

        work = RECT()
        avail_h, avail_w = 1080, 1920
        try:
            if _u.SystemParametersInfoW(0x0030, 0, byref(work), 0):
                avail_h = max(600, work.bottom - work.top)
                avail_w = max(800, work.right - work.left)
        except Exception:
            pass

        def win_size(s):
            r = RECT(0, 0, int(PANEL_W * s), int(PANEL_H * s))
            _u.AdjustWindowRect(byref(r), wstyle, False)
            return r.right - r.left, r.bottom - r.top

        scale = max(1.0, dpi / 96.0)
        ww, hh = win_size(scale)
        for _ in range(4):
            if hh <= avail_h and ww <= avail_w:
                break
            scale = max(1.0, scale * min(avail_h / float(hh), avail_w / float(ww)))
            ww, hh = win_size(scale)
        self._scale = scale      # 高度自适应要用同一套缩放换算 CSS px → 物理 px

        # 贴屏幕右下角：右缘贴工作区右边、底缘贴工作区底部。
        # SPI_GETWORKAREA (0x0030) 拿不到时退回 CW_USEDEFAULT。
        px = py = CW_USEDEFAULT
        if work.right > work.left:
            px = max(work.left, work.right - ww)
            py = max(work.top, work.bottom - hh)
        hwnd = _u.CreateWindowExW(0, cls, self.TITLE, wstyle,
                                  px, py, ww, hh,
                                  None, None, _k.GetModuleHandleW(None), None)
        if not hwnd:
            self._note = "窗口创建失败 err=%d" % ctypes.get_last_error()
            return False
        self.hwnd = int(hwnd)
        _HWND_MAP[self.hwnd] = self
        _u.SetWindowTextW(hwnd, self.TITLE)
        return True

    def _on_msg(self, hwnd, msg, wparam, lparam):
        if msg == WM_COPYDATA:
            # 主进程 -> 面板子进程的命令通道（面板跑在独立进程里，
            # 这样 WebView2 万一崩也只死面板，不会带走电源管理本体）。
            # 命令格式：utf-8 文本 "show\t<url>" / "hide" / "quit"
            if lparam:
                try:
                    cds = ctypes.cast(lparam, POINTER(COPYDATASTRUCT)).contents
                    if cds.cbData and cds.lpData:
                        text = ctypes.string_at(cds.lpData, cds.cbData).decode(
                            "utf-8", "replace").rstrip("\x00")
                        self._on_cmd(text)
                except Exception:
                    pass
            return 1
        if msg == WM_APP_EXIT:
            self._exiting = True
            return 0
        if msg in (WM_SIZE, WM_WINDOWPOSCHANGED):
            self._resize()
            return 0
        if msg == WM_CLOSE:
            _u.ShowWindow(hwnd, SW_HIDE)     # 关闭=隐藏，程序继续在托盘跑
            return 0
        if msg == WM_DESTROY:
            _HWND_MAP.pop(int(hwnd), None)
            return 0
        return _u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_cmd(self, text: str) -> None:
        """处理外部命令（WM_COPYDATA 文本）"""
        parts = text.split("\t", 1)
        cmd = parts[0].strip()
        if cmd == "show":
            url = (parts[1].strip() if len(parts) > 1 else "")
            if url and self.url != url:
                self.url = url
                _u.ShowWindow(self.hwnd, SW_HIDE)     # 先藏，避免闪旧内容
                self._navigate()
                _u.ShowWindow(self.hwnd, SW_SHOW)
            else:
                self._navigate()
                _u.ShowWindow(self.hwnd, SW_SHOW)
            try:
                _u.SetForegroundWindow(self.hwnd)
            except Exception:
                pass
        elif cmd == "hide":
            _u.ShowWindow(self.hwnd, SW_HIDE)
        elif cmd == "quit":
            self._exiting = True

    def _resize(self) -> None:
        if not self.controller:
            return
        r = RECT()
        _u.GetClientRect(self.hwnd, byref(r))
        fn = _vcall(self.controller, 6, HRESULT, [RECT])      # put_Bounds
        if fn:
            try:
                fn(self.controller, r)
            except Exception:
                pass

    # ------------------------------------------------------------ 初始化链路
    def ensure(self) -> bool:
        """准备窗口与 WebView2 环境（异步：靠主线程消息泵推进回调）"""
        if self.ready or self.failed:
            return self.ready
        if not self._ensure_window():
            self.failed = True
            return False
        if not self._load_loader():
            self.failed = True
            return False
        try:
            _o.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            self._coinit = True
        except Exception:
            pass
        try:
            os.makedirs(self._data_dir, exist_ok=True)
        except Exception:
            pass

        @ctypes.WINFUNCTYPE(HRESULT, c_void_p, HRESULT, c_void_p)
        def on_env(this, result, env):
            self._on_env_created(result, env)
            return S_OK

        handler = _com_object([on_env])
        hr = self._create_env(None, self._data_dir, None, handler)
        if hr != S_OK:
            self._note = "CreateEnvironment hr=0x%X" % (hr & 0xFFFFFFFF)
            self.failed = True
        return not self.failed

    @staticmethod
    def _addref(ptr) -> None:
        """对一个 COM 接口指针 AddRef（vtable[1]）。

        必须做：回调里拿到的 env/controller 指针，只在本回调期间被运行时保活；
        回调一返回就可能释放，之后再调用就是野指针 → 随机访问违例
        （症状：时好时坏，看起来像 DisplayWindow/后续调用随机崩）。"""
        fn = _vcall(ptr, 1, c_ulong, [])
        if fn:
            try:
                fn(ptr)
            except Exception:
                pass

    def _on_env_created(self, result, env):
        if result != S_OK or not env:
            self._note = "环境创建失败 hr=0x%X" % (result & 0xFFFFFFFF)
            self.failed = True
            return
        self.env = env
        self._addref(env)          # 保活，否则回调返回后指针随时失效

        @ctypes.WINFUNCTYPE(HRESULT, c_void_p, HRESULT, c_void_p)
        def on_ctrl(this, res, ctrl):
            self._on_controller_created(res, ctrl)
            return S_OK

        handler = _com_object([on_ctrl])
        fn = _vcall(env, 3, HRESULT, [wintypes.HWND, c_void_p])   # CreateController
        if not fn:
            self.failed = True
            return
        hr = fn(env, self.hwnd, handler)
        if hr != S_OK:
            self._note = "CreateController hr=0x%X" % (hr & 0xFFFFFFFF)
            self.failed = True

    def _on_controller_created(self, result, ctrl):
        if result != S_OK or not ctrl:
            self._note = "控制器创建失败 hr=0x%X" % (result & 0xFFFFFFFF)
            self.failed = True
            return
        self.controller = ctrl
        self._addref(ctrl)         # 同上：controller 也要自己持有引用
        try:
            f = _vcall(ctrl, 4, HRESULT, [wintypes.BOOL])        # put_IsVisible
            if f:
                f(ctrl, 1)
            self._resize()
            g = _vcall(ctrl, 25, HRESULT, [POINTER(c_void_p)])   # get_CoreWebView2
            wv = c_void_p()
            if g and g(ctrl, byref(wv)) == S_OK:
                self.webview = wv.value
            self.ready = True
            self._install_height_watch()
            self._navigate()
        except Exception as e:
            self._note = "控制器初始化异常 %r" % e
            self.failed = True

    def _navigate(self) -> None:
        if not self.webview or self._nav_url == self.url:
            return
        fn = _vcall(self.webview, 5, HRESULT, [wintypes.LPCWSTR])   # Navigate
        if fn:
            try:
                hr = fn(self.webview, self.url)
                if hr == S_OK:
                    self._nav_url = self.url
                else:
                    self._note = "Navigate hr=0x%X" % (hr & 0xFFFFFFFF)
            except Exception as e:
                self._note = "Navigate 异常 %r" % e

    def navigate(self, url: str) -> None:
        """切换页面（托盘「设置」要跳 #adv 时用）"""
        self.url = url
        if not self.webview:
            return
        fn = _vcall(self.webview, 5, HRESULT, [wintypes.LPCWSTR])
        if fn:
            try:
                fn(self.webview, url)
            except Exception:
                pass

    # ------------------------------------------------------ 高度自适应
    # vtable（0 起点含 IUnknown 三件套）：27=AddScriptToExecuteOnDocumentCreated
    # 32=PostWebMessageAsJson 34=add_WebMessageReceived；
    # EventArgs 上 4=TryGetWebMessageAsString。Navigate=5 已在别处验证过，
    # 这几个下标与之同一张 vtable，顺序来自官方 ICoreWebView2 头文件。
    _HEIGHT_SCRIPT = (
        "(function(){var s=function(){try{var h=0,b=document.body;"
        "if(b&&b.getBoundingClientRect().bottom>0)"
        "h=Math.ceil(b.getBoundingClientRect().bottom);"
        "if(!(h>0))h=Math.ceil(document.documentElement.scrollHeight);"
        "window.chrome.webview.postMessage("
        "JSON.stringify({__panel_h:h}));}catch(e){}};"
        "try{new ResizeObserver(function(){setTimeout(s,0);})"
        ".observe(document.body);}catch(e){"
        "window.setInterval(s,1500);}"
        "window.addEventListener('load',function(){setTimeout(s,50);});"
        "setTimeout(s,50);})();")

    def _install_height_watch(self) -> None:
        """注入高度上报脚本 + 注册 WebMessageReceived。

        网页(index.html)里也有一份上报逻辑；这里再注入一份是故意的双保险：
        页面版带去重，注入版覆盖「其它页面/页面改动丢失」的情况，
        宿主端有滞回，重复报无害。"""
        if not self.webview:
            return

        @ctypes.WINFUNCTYPE(HRESULT, c_void_p, HRESULT, c_void_p)
        def on_script_done(this, err, _id):
            return S_OK

        add = _vcall(self.webview, 27, HRESULT, [wintypes.LPCWSTR, c_void_p])
        if add:
            try:
                add(self.webview, self._HEIGHT_SCRIPT,
                    ctypes.cast(_com_object([on_script_done]), c_void_p))
            except Exception:
                pass

        @ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)
        def on_msg(this, _sender, args):
            try:
                get = _vcall(args, 4, HRESULT, [POINTER(c_void_p)])
                s = c_void_p()
                if get and get(args, byref(s)) == S_OK and s.value:
                    raw = ctypes.wstring_at(s.value)
                    _o.CoTaskMemFree(s)
                    self._on_web_message(raw)
            except Exception:
                pass
            return S_OK

        reg = _vcall(self.webview, 34, HRESULT, [c_void_p, c_void_p])
        if reg:
            try:
                token = c_ulonglong(0)
                reg(self.webview,
                    ctypes.cast(_com_object([on_msg]), c_void_p),
                    ctypes.byref(token))
            except Exception:
                pass

    def _on_web_message(self, text: str) -> None:
        # 实测 WebView2 会把字符串消息再 JSON 编码一层（'"…"'），
        # 页面自己又 stringify 了一层 —— 循环解到出 dict 为止。
        d = text
        for _ in range(3):
            if not isinstance(d, str):
                break
            try:
                d = json.loads(d)
            except Exception:
                return
        if isinstance(d, dict):
            h = d.get("__panel_h")
            if isinstance(h, (int, float)) and 100 < h < 4000:
                self._fit_height(int(h))

    def _fit_height(self, css_h: int) -> None:
        """按网页内容高度调整窗口，保持右下角锚点不动。

        - 滞回 6 CSS px：内容行高抖动时不来回拉窗；
        - 超出工作区就钳到工作区，并通知页面改出滚动条（而不是裁掉）。"""
        if not self.hwnd:
            return
        work = RECT()
        have_work = bool(_u.SystemParametersInfoW(0x0030, 0, byref(work), 0))
        wr, cr = RECT(), RECT()
        _u.GetWindowRect(self.hwnd, byref(wr))
        _u.GetClientRect(self.hwnd, byref(cr))
        vframe = (wr.bottom - wr.top) - (cr.bottom - cr.top)   # 标题栏+边框
        scale = max(1.0, self._scale)
        max_client = 1 << 30
        if have_work:
            max_client = max(200, (work.bottom - work.top) - vframe)
        want = min(int(round(css_h * scale)), max_client)
        cur = cr.bottom - cr.top
        if abs(want - cur) < int(6 * scale):
            return
        clamped = int(round(css_h * scale)) > max_client
        if clamped != self._clamped:
            self._clamped = clamped
            post = _vcall(self.webview, 32, HRESULT, [wintypes.LPCWSTR])
            if post:
                try:
                    post(self.webview,
                         '{"__panel_clamped":true}' if clamped
                         else '{"__panel_clamped":false}')
                except Exception:
                    pass
        new_wh = want + vframe
        x = wr.left
        y = wr.bottom - new_wh                      # 底缘贴住原位置
        if have_work:
            y = max(work.top, y)
        _u.MoveWindow(self.hwnd, x, y, wr.right - wr.left, new_wh, True)

    # ------------------------------------------------------------ 对外
    def _pump_once(self, seconds: float) -> None:
        """无条件泵消息若干秒 —— 常驻消息循环用。

        注意与 _pump_wait 的区别：后者是「还没 ready 才泵」，一旦就绪立刻返回，
        拿它当常驻循环会导致窗口【就绪之后就彻底不处理消息】：WM_COPYDATA 收
        不到、点关闭按钮没反应、看起来像卡死。
        """
        if self._pumping:
            time.sleep(seconds)
            return
        self._pumping = True

        class MSG(ctypes.Structure):
            _fields_ = [("hWnd", c_void_p), ("message", wintypes.UINT),
                        ("wParam", c_void_p), ("lParam", c_void_p),
                        ("time", wintypes.DWORD), ("pt", wintypes.LONG * 2)]

        try:
            _u.PeekMessageW.argtypes = [POINTER(MSG), c_void_p, wintypes.UINT,
                                        wintypes.UINT, wintypes.UINT]
            _u.PeekMessageW.restype = wintypes.BOOL
            _u.TranslateMessage.argtypes = [POINTER(MSG)]
            _u.DispatchMessageW.argtypes = [POINTER(MSG)]
            msg = MSG()
            end = time.time() + max(0.0, seconds)
            while time.time() < end and not self._exiting:
                if _u.PeekMessageW(byref(msg), None, 0, 0, 1):   # PM_REMOVE
                    _u.TranslateMessage(byref(msg))
                    _u.DispatchMessageW(byref(msg))
                else:
                    time.sleep(0.01)
        except Exception:
            time.sleep(min(seconds, 0.5))
        finally:
            self._pumping = False

    def _pump_wait(self, timeout: float) -> None:
        """在当前线程泵消息，等 WebView2 的异步回调落地。

        WebView2 的 CompletedHandler 是靠消息循环推进的：不泵消息就永远 ready
        不了，窗口先显示出来、内容还没到 —— 这就是「窗口出来了但一片白」。"""
        if self._pumping:
            return
        self._pumping = True

        class MSG(ctypes.Structure):
            _fields_ = [("hWnd", c_void_p), ("message", wintypes.UINT),
                        ("wParam", c_void_p), ("lParam", c_void_p),
                        ("time", wintypes.DWORD), ("pt", wintypes.LONG * 2)]

        try:
            _u.PeekMessageW.argtypes = [POINTER(MSG), c_void_p, wintypes.UINT,
                                        wintypes.UINT, wintypes.UINT]
            _u.PeekMessageW.restype = wintypes.BOOL
            _u.TranslateMessage.argtypes = [POINTER(MSG)]
            _u.DispatchMessageW.argtypes = [POINTER(MSG)]
            msg = MSG()
            end = time.time() + timeout
            while time.time() < end and not self.ready and not self.failed:
                if _u.PeekMessageW(byref(msg), None, 0, 0, 1):   # PM_REMOVE
                    _u.TranslateMessage(byref(msg))
                    _u.DispatchMessageW(byref(msg))
                else:
                    time.sleep(0.01)
        except Exception:
            pass
        finally:
            self._pumping = False

    def show(self, url: str = None) -> bool:
        """显示面板窗口；只有 WebView2 真的就绪才返回 True（否则调用方会降级）"""
        if self.failed and not self.hwnd:
            return False
        if url:
            self.url = url
        if not self._ensure_window():
            return False
        self.ensure()                     # 环境/控制器异步建立
        if not self.ready:
            self._pump_wait(2.0)
        if not self.ready or not self.webview:
            # 没就绪绝不接管：宁可让调用方回退 GDI/浏览器，也不能给用户一个白窗口
            self.failed = True
            self.hide()
            return False
        self._navigate()                  # URL 变化时重新导航
        _u.ShowWindow(self.hwnd, SW_SHOW)
        try:
            _u.SetForegroundWindow(self.hwnd)
        except Exception:
            pass
        return True

    def hide(self) -> None:
        if self.hwnd:
            _u.ShowWindow(self.hwnd, SW_HIDE)

    def report(self) -> dict:
        return {"ok": self.ready, "failed": self.failed, "hwnd": self.hwnd,
                "note": self._note, "engine": "webview2"}
