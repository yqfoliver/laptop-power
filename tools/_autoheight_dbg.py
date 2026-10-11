# -*- coding: utf-8 -*-
"""高度自适应链路调试：哪一环断了？（注入脚本 / 消息回调 / resize 决策）"""
import ctypes
import os
import sys
import threading
import time
from ctypes import POINTER, byref, c_void_p, wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_u = ctypes.WinDLL("user32", use_last_error=True)
_o = ctypes.WinDLL("ole32", use_last_error=True)

from lp import webview2panel as W

# 插桩 1：消息回调有没有进来
orig_msg = W.WebView2Panel._on_web_message
def dbg_msg(self, text):
    print("[MSG] %r" % text[:120], flush=True)
    return orig_msg(self, text)
W.WebView2Panel._on_web_message = dbg_msg

# 插桩 2：resize 决策
orig_fit = W.WebView2Panel._fit_height
def dbg_fit(self, css_h):
    wr, cr = W.RECT(), W.RECT()
    _u.GetWindowRect(self.hwnd, byref(wr))
    _u.GetClientRect(self.hwnd, byref(cr))
    print("[FIT] css_h=%d  scale=%.2f  client=%d  win=%dx%d"
          % (css_h, self._scale, cr.bottom - cr.top,
             wr.right - wr.left, wr.bottom - wr.top), flush=True)
    return orig_fit(self, css_h)
W.WebView2Panel._fit_height = dbg_fit

# 插桩 3：安装阶段
orig_inst = W.WebView2Panel._install_height_watch
def dbg_inst(self):
    print("[INST] webview=%s" % bool(self.webview), flush=True)
    r = orig_inst(self)
    print("[INST] done", flush=True)
    return r
W.WebView2Panel._install_height_watch = dbg_inst


class RECT(W.RECT):
    pass


def main():
    url = "http://127.0.0.1:8798/"
    panel = W.WebView2Panel(url, os.path.join(
        os.environ.get("LOCALAPPDATA", "."), "LaptopPowerAuto", "webview2_dbg"))
    stop = threading.Event()

    def worker():
        _o.CoInitializeEx(None, 0x2)
        if not panel._ensure_window():
            print("ensure_window 失败", flush=True)
            return
        if not panel.ensure():
            print("ensure 失败: %s" % panel._note, flush=True)
            return
        while not stop.is_set():
            panel._pump_once(0.05)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    for _ in range(30):
        time.sleep(1)
        if panel.ready or panel.failed:
            break
    time.sleep(6)     # 等页面加载、脚本注入、上报

    # 兜底验证：用 ExecuteScript 直接量（顺带验证 vtable 29 可用）
    if panel.ready:
        result = {"v": None}

        @ctypes.WINFUNCTYPE(W.HRESULT, c_void_p, W.HRESULT, c_void_p)
        def on_done(this, err, json_ptr):
            try:
                if json_ptr:
                    result["v"] = ctypes.wstring_at(json_ptr)
                    _o.CoTaskMemFree(json_ptr)
            except Exception as e:
                print("[ESC] 回调异常 %r" % e, flush=True)
            return W.S_OK

        fn = W._vcall(panel.webview, 29, W.HRESULT, [wintypes.LPCWSTR, c_void_p])
        print("[ESC] ExecuteScript fn=%s" % bool(fn), flush=True)
        if fn:
            fn(panel.webview,
               "(function(){var w=document.querySelector('.wrap');"
               "return JSON.stringify({sh:document.documentElement.scrollHeight,"
               "wb:Math.round(w.getBoundingClientRect().bottom)});})()",
               ctypes.cast(W._com_object([on_done]), c_void_p))
        for _ in range(20):
            time.sleep(0.2)
            if result["v"] is not None:
                break
        print("[ESC] 结果=%r" % result["v"], flush=True)
        wr, cr = W.RECT(), W.RECT()
        _u.GetWindowRect(panel.hwnd, byref(wr))
        _u.GetClientRect(panel.hwnd, byref(cr))
        print("[WIN ] client=%d  window=%dx%d" %
              (cr.bottom - cr.top, wr.right - wr.left, wr.bottom - wr.top),
              flush=True)
    stop.set()
    time.sleep(0.5)


if __name__ == "__main__":
    main()
