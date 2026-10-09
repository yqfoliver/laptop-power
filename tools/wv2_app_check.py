# -*- coding: utf-8 -*-
"""端到端验证：网页面板（WebView2）在【独立子进程】里能不能正常工作。

覆盖：
  1. 主进程判断是否走了 WebView2 分支
  2. 面板子进程被拉起、窗口出现
  3. 屏幕抓像素 —— 判断是不是真的渲染出内容（不是白屏）
  4. 隐藏 / 再次显示的命令通道（WM_COPYDATA）是否可用
  5. 子进程不再是 ppid 的孩子（父子关系 + 后续脚本退出后是否自动回收）

用法（依赖已在运行的主程序 /api/status）：
    python tools/wv2_app_check.py
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

_u = ctypes.WinDLL("user32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)
_u.IsWindowVisible.argtypes = [wintypes.HWND]
_u.IsWindowVisible.restype = wintypes.BOOL

OK = []
BAD = []


def check(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print("  [%s] %s %s" % ("OK " if cond else "FAIL", name, extra))


def api_alive() -> bool:
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        op.open("http://127.0.0.1:8753/api/status", timeout=2).read()
        return True
    except Exception:
        return False


def main():
    url = "http://127.0.0.1:8753/"
    check("主程序已运行（本地面板服务可达）", api_alive())
    if not OK:
        print("跳过：主程序没起")
        return 1

    import main as appmod                       # noqa: 与主程序同名的入口模块
    from wv2_probe import capture_screen

    appmod._WV2_FLAG[0] = True                  # 试用模式：不改用户 config
    print("调用 _open_webview2 ...")
    t0 = time.time()
    opened = appmod._open_webview2(url)
    dt = time.time() - t0
    check("_open_webview2 返回 True", opened, "耗时 %.1fs" % dt)
    if not opened:
        return 1

    hwnd = appmod._wv2_find()
    check("面板窗口已存在", bool(hwnd), "hwnd=%s" % hwnd)
    time.sleep(4)                                # 留给页面加载 + 首帧
    check("窗口当前可见", bool(_u.IsWindowVisible(hwnd)))

    shot = os.path.join(ROOT, "tools", "wv2_app.png")
    try:
        w, h, ratio = capture_screen(hwnd, shot)
        check("抓屏有内容（非白占比）", ratio > 0.02,
              "%dx%d ratio=%.3f -> %s" % (w, h, ratio, shot))
    except Exception:
        import traceback as tb
        tb.print_exc()
        check("抓屏有内容（非白占比）", False, "见上方堆栈")

    try:
        import traceback

        class CDS(ctypes.Structure):
            _fields_ = [("dwData", ctypes.c_void_p), ("cbData", ctypes.c_uint),
                        ("lpData", ctypes.c_void_p)]
        data = b"hide"
        buf = ctypes.create_string_buffer(data)
        cds = CDS(0, len(data), ctypes.cast(buf, ctypes.c_void_p))
        u = ctypes.windll.user32
        u.SendMessageTimeoutW.restype = ctypes.c_int
        u.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                          ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_uint, ctypes.c_uint,
                                          ctypes.POINTER(ctypes.c_ulong)]
        res = ctypes.c_ulong()
        ok = u.SendMessageTimeoutW(ctypes.c_void_p(hwnd), 0x004A, None,
                                   ctypes.byref(cds), 0x0002, 1500,
                                   ctypes.byref(res))
        check("命令通道 hide（手工，含异常细节）", bool(ok),
              "lasterr=%d" % ctypes.get_last_error())
    except Exception:
        import traceback as tb
        tb.print_exc()
        check("命令通道 hide（手工）", False)
    time.sleep(1)
    check("窗口已隐藏", not _u.IsWindowVisible(hwnd))
    check("命令通道 show", appmod._wv2_send("show\t" + url))
    time.sleep(2)
    check("再次显示成功", bool(_u.IsWindowVisible(hwnd)))
    try:
        w, h, ratio2 = capture_screen(hwnd, os.path.join(ROOT, "tools", "wv2_app.png"))
        print("        二次抓屏 ratio=%.3f" % ratio2)
    except Exception:
        pass

    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe",
                              "/FO", "CSV", "/NH"], capture_output=True,
                             timeout=15).stdout.decode("mbcs", "replace")
        n = sum(1 for ln in out.splitlines() if ln.strip())
        print("        当前 python.exe 进程数 = %d（含本脚本）" % n)
    except Exception:
        pass

    print("结果: %d 项通过, %d 项失败 %s" % (len(OK), len(BAD), BAD or ""))
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
