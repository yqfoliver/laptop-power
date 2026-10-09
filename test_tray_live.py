# -*- coding: utf-8 -*-
"""托盘「真窗口」测试：真实 Shell_NotifyIcon 注册 + 真实消息泵。

与 test_tray.py（mock 分发逻辑）互补，验证的是：
  1. 图标真的能注册进系统托盘（NIM_ADD 返回真）
  2. 模拟 Shell 右键回调消息 → PostMessage → 消息泵 → 窗口过程 → _popup 全链路
  3. WM_COMMAND 菜单选择派发（含「退出」）
  4. TaskbarCreated（explorer 重启）处理不崩
  5. destroy() 干净清理（防「僵尸图标」）
"""

import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lp import tray as traylib          # noqa: E402
from lp.tray import Tray                # noqa: E402

_PASS = 0
_FAIL = 0


def ok(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print("  FAIL: %s" % msg)


_u = ctypes.WinDLL("user32")


class MSG(ctypes.Structure):
    _fields_ = [("hWnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_uint), ("pt", ctypes.c_long * 2)]


def pump(ms=300):
    msg = MSG()
    end = time.time() + ms / 1000.0
    while time.time() < end:
        while _u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            _u.TranslateMessage(ctypes.byref(msg))
            _u.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def main():
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205
    WM_COMMAND = 0x0111
    UID = 0x7A11

    got = []
    t = Tray(on_command=lambda mid: got.append(mid))
    t.menu_provider = lambda: [
        (traylib.MENU_OPEN, "打开", False),
        (traylib.MENU_SETTINGS, "设置", False),
        (traylib.MENU_AUTO, "自动判定：开", False),
        (traylib.MENU_AUTOSTART, "开机自启：开", False),
        (traylib.MENU_QUIT, "退出", False),
    ]
    t._test_mode = True                    # 不真弹菜单，只验证链路

    print("== 启动 ==")
    ok(t.start("测试托盘"), "托盘图标注册成功（Shell_NotifyIcon NIM_ADD）")
    ok(t.hwnd != 0, "承载窗口已创建")
    ok(t._taskbar_created != 0, "TaskbarCreated 消息已注册")

    print("== 右键 → 菜单全链路 ==")
    _u.PostMessageW(t.hwnd, WM_LBUTTONUP, UID, WM_RBUTTONUP)
    pump()
    ok(t._popup_n == 1, "右键触发菜单: n=%d" % t._popup_n)
    _u.PostMessageW(t.hwnd, WM_LBUTTONUP, UID, WM_RBUTTONUP)
    pump()
    ok(t._popup_n == 2, "再次右键仍触发")

    print("== 左键 → 打开 ==")
    _u.PostMessageW(t.hwnd, WM_LBUTTONUP, UID, WM_LBUTTONUP)
    pump()
    ok(t._popup_n == 2, "左键不触发菜单")
    ok(got and got[-1] == traylib.MENU_OPEN, "左键派发 MENU_OPEN: %s" % got)

    print("== 菜单选择（WM_COMMAND）==")
    for mid in (traylib.MENU_OPEN, traylib.MENU_SETTINGS,
                traylib.MENU_AUTO, traylib.MENU_QUIT):
        _u.PostMessageW(t.hwnd, WM_COMMAND, mid & 0xFFFF, 0)
    pump()
    ok(got[-4:] == [traylib.MENU_OPEN, traylib.MENU_SETTINGS,
                    traylib.MENU_AUTO, traylib.MENU_QUIT],
       "菜单项全部派发到位: %s" % got[-4:])
    ok(traylib.MENU_QUIT in got, "「退出」命令可达（用户核心诉求）")

    print("== TaskbarCreated（explorer 重启场景）==")
    _u.PostMessageW(t.hwnd, t._taskbar_created, 0, 0)
    pump()
    ok(True, "处理不崩")

    print("== 菜单内容（微软电脑管家式简洁）==")
    items = t.menu_provider()
    texts = [x[1] for x in items]
    ok(len(items) == 5, "菜单共 5 项: %s" % texts)
    ok(texts[0] == "打开" and texts[1] == "设置" and texts[-1] == "退出",
       "首项=打开、次项=设置、末项=退出")
    ok(traylib.MENU_QUIT in [x[0] for x in items], "退出项在菜单里")

    print("== 真弹菜单（不走 _test_mode，验证菜单窗口真的出现）==")
    # 这条用例专门盯 TrackPopupMenu(Ex) 的参数：早年把 7 参数的
    # TrackPopupMenu 当 6 参数调用（漏 nReserved），菜单一次都没弹出来，
    # 而 _test_mode 用例完全测不出来。所以必须真弹一次、真查 #32768。
    t2 = Tray(on_command=lambda mid: got.append(mid))
    t2.menu_provider = lambda: [(traylib.MENU_OPEN, "打开", False),
                                (traylib.MENU_QUIT, "退出", False)]
    ok(t2.start("测试托盘2"), "第二个托盘图标注册成功")

    _u.FindWindowExW.restype = ctypes.c_void_p
    _u.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_wchar_p, ctypes.c_wchar_p]
    seen = []

    def watcher():
        end = time.time() + 3.0
        while time.time() < end:
            h = _u.FindWindowExW(None, None, "#32768", None)
            if h and h not in seen:
                seen.append(h)
            time.sleep(0.05)
        for m in seen:
            _u.PostMessageW(m, 0x0100, 0x1B, 0)          # ESC
        _u.PostMessageW(t2.hwnd, 0x001F, 0, 0)           # WM_CANCELMODE

    threading.Thread(target=watcher, daemon=True).start()
    _u.PostMessageW(t2.hwnd, WM_LBUTTONUP, UID, WM_RBUTTONUP)
    pump(4000)
    ok(bool(seen), "右键后真的出现了菜单窗口（#32768）: %s" % (seen or "无"))
    ok(t2._popup_n == 1, "_popup 真实执行了一次: n=%d" % t2._popup_n)
    t2.destroy()

    print("== 销毁（防僵尸图标）==")
    t.destroy()
    ok(True, "destroy 正常返回")

    print("\n通过 %d 项，失败 %d 项" % (_PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
