# -*- coding: utf-8 -*-
"""WebView2 崩溃定位矩阵：每个阶段跑在独立子进程里，崩了不连累后面的阶段。

阶段是累积的（stage N = 做完 1..N），哪个阶段的退出码变成 0xC0000005
（access violation，十进制 3221225477），就说明崩溃发生在那一步引入的动作上。

用法：python tools/wv2_matrix.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
AV = 3221225477            # 0xC0000005  访问违例
STAGES = [
    "创建隐藏窗口 + 泵消息（不碰 COM）",
    "加载 WebView2Loader.dll",
    "CoInitializeEx + 创建 Environment",
    "创建 Controller（put_IsVisible + put_Bounds）",
    "ShowWindow 显示 + 加载页面 + 抓屏",
]


def run_child(stage: int):
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"          # 崩时要拿到已输出的最后一行
    p = subprocess.run([PY, os.path.join(ROOT, "tools", "wv2_matrix.py"),
                        "--child", str(stage)],
                       capture_output=True, timeout=120, cwd=ROOT, env=env)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def main():
    print("WebView2 崩溃定位矩阵（每阶段独立进程）")
    print("=" * 66)
    for n in range(1, len(STAGES) + 1):
        try:
            code, out, err = run_child(n)
        except subprocess.TimeoutExpired:
            code, out, err = -1, "", "超时"
        tag = "崩溃 AV" if code == AV else ("异常退出" if code != 0 else "存活 OK")
        print("阶段 %d  %-42s %-8s exit=%-12s" % (n, STAGES[n - 1], tag, code))
        tail = (out.strip().splitlines() or ["<无输出>"])[-3:]
        for line in tail:
            print("        | " + line)
        if err.strip():
            last = err.strip().splitlines()[-1]
            print("        ! " + last[:120])
        if code != 0:
            print("\n>>> 崩溃点锁定：阶段 %d —— %s" % (n, STAGES[n - 1]))
            print(">>> 完整 stderr:\n" + err[-2000:])
            break
    else:
        print("=" * 66)
        print("全部阶段存活 —— WebView2 链路在本机可用")


# ------------------------------------------------------------------ 子进程
def child(stage: int):
    sys.path.insert(0, ROOT)
    import ctypes
    import threading
    from ctypes import POINTER, byref, c_void_p, wintypes

    _u = ctypes.WinDLL("user32", use_last_error=True)
    _o = ctypes.WinDLL("ole32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    SW_HIDE, SW_SHOW = 0, 5

    from lp.webview2panel import WebView2Panel, _com_object, _vcall, HRESULT, S_OK

    @ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.HRESULT if hasattr(ctypes, "HRESULT") else ctypes.c_long, c_void_p)
    def _hmk(fn):
        return fn

    url = "file:///" + os.path.join(ROOT, "web", "index.html").replace("\\", "/")
    dd = os.path.join(os.environ.get("LOCALAPPDATA", "."), "LaptopPowerAuto", "wv2matrix")
    p = WebView2Panel(url, dd)
    stop = threading.Event()

    print("[s1] ensure_window ...")
    if not p._ensure_window():
        print("[s1] 窗口创建失败", p._note)
        sys.exit(2)
    print("[s1] hwnd=%d" % p.hwnd)

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end and not stop.is_set():
            p._pump_wait(0.3)

    if stage >= 1:
        pump(2.0)
        print("[s1] 泵消息 2s 存活")

    if stage >= 2:
        if not p._load_loader():
            print("[s2] loader 失败", p._note)
            sys.exit(3)
        print("[s2] loader OK ->", p._loader_path())
        pump(1.0)

    if stage >= 3:
        hr = _o.CoInitializeEx(None, 0x2)
        print("[s3] CoInitializeEx hr=0x%X" % (hr & 0xFFFFFFFF))
        box = {}

        @ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, ctypes.c_long, c_void_p)
        def on_env(this, result, env):
            box["result"] = result
            box["env"] = env
            # 关键：AddRef 必须在【回调内部】做。回调一返回运行时就可能把对象
            # 释放掉，之后再 AddRef/调用都是在调已释放的内存（随机 AV）。
            if result == 0 and env:
                try:
                    WebView2Panel._addref(env)
                    box["addref"] = True
                except Exception as e:
                    box["addref"] = repr(e)
            return 0

        handler = _com_object([on_env])
        hr = p._create_env(None, dd, None, handler)
        print("[s3] CreateEnv hr=0x%X" % (hr & 0xFFFFFFFF))
        end = time.time() + 25
        while time.time() < end and "env" not in box:
            pump(0.3)
        if "env" not in box:
            print("[s3] 等不到 env 回调")
            sys.exit(4)
        print("[s3] env 回调 result=0x%X env=%s" %
              ((box["result"] or 0) & 0xFFFFFFFF, bool(box["env"])))
        p.env = box["env"]
        print("[s3] env AddRef in-callback = %s" % box.get("addref"))

    if stage >= 4:
        box2 = {}

        @ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, ctypes.c_long, c_void_p)
        def on_ctrl(this, res, ctrl):
            box2["res"] = res
            box2["ctrl"] = ctrl
            if res == 0 and ctrl:
                try:                      # 同样：必须在回调内 AddRef
                    WebView2Panel._addref(ctrl)
                    box2["addref"] = True
                except Exception as e:
                    box2["addref"] = repr(e)
            return 0

        handler = _com_object([on_ctrl])
        fn = _vcall(p.env, 3, HRESULT, [wintypes.HWND, c_void_p])
        hr = fn(p.env, p.hwnd, handler)
        print("[s4] CreateController hr=0x%X" % (hr & 0xFFFFFFFF))
        end = time.time() + 25
        while time.time() < end and "ctrl" not in box2:
            pump(0.3)
        if "ctrl" not in box2:
            print("[s4] 等不到 controller 回调")
            sys.exit(5)
        p.controller = box2["ctrl"]
        print("[s4] controller 回调 res=0x%X ctrl=%s AddRef=%s" %
              ((box2["res"] or 0) & 0xFFFFFFFF, bool(box2["ctrl"]),
               box2.get("addref")))
        f = _vcall(p.controller, 4, HRESULT, [wintypes.BOOL])
        print("[s4] put_IsVisible hr=0x%X" % (f(p.controller, 1) & 0xFFFFFFFF))
        p._resize()
        print("[s4] put_Bounds 调用完毕（未崩）")
        g = _vcall(p.controller, 25, HRESULT, [POINTER(c_void_p)])
        wv = c_void_p()
        if g(p.controller, byref(wv)) == S_OK:
            p.webview = wv.value
        print("[s4] get_CoreWebView2 -> %s" % bool(p.webview))
        if p.webview:
            nav = _vcall(p.webview, 5, HRESULT, [wintypes.LPCWSTR])
            print("[s4] Navigate hr=0x%X" % (nav(p.webview, url) & 0xFFFFFFFF))

    if stage >= 5:
        print("[s5] ShowWindow ...")
        _u.ShowWindow(p.hwnd, SW_SHOW)
        print("[s5] ShowWindow OK")
        pump(6.0)
        print("[s5] 显示后持续泵 6s 存活")
        # 抓屏判断白屏
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        try:
            from wv2_probe import capture_screen
            w, h, ratio = capture_screen(p.hwnd, os.path.join(ROOT, "tools", "wv2_matrix.png"))
            print("[s5] 抓屏 %dx%d 非白占比=%.3f -> %s" %
                  (w, h, ratio, "有内容" if ratio > 0.02 else "白屏"))
        except Exception as e:
            print("[s5] 抓屏失败 %r" % e)

    print("[done] 阶段 %d 正常走到最后" % stage)
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        child(int(sys.argv[2]))
    else:
        main()
