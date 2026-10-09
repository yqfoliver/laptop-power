# -*- coding: utf-8 -*-
"""托盘消息分发链路测试：右键弹菜单 / 左键开面板 / 菜单项点击派发"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import tray as T

results = []

t = T.Tray(on_command=lambda mid: results.append(("cmd", mid)))
t.menu_provider = lambda: [
    (T.MENU_OPEN, "打开控制面板", False),
    (T.MENU_QUIT, "退出", False),
]
t.hwnd = 1   # 假句柄，Win32 调用全部 mock 掉

# ---- mock Win32（不真弹菜单）
# 注意：实现早已改用 5 参数的 TrackPopupMenuEx（老的 TrackPopupMenu 是 7 参数，
# 漏掉 nReserved 会把 hWnd 传成 NULL → err=1400，菜单一次都弹不出来）。
# 这里 mock 的必须是真正被调用的那个 API，否则测试会「假绿」。
T._u.SetForegroundWindow = lambda h: 1
T._u.CreatePopupMenu = lambda: 1234
T._u.AppendMenuW = lambda *a: 1
T._u.DestroyMenu = lambda m: 1
_MOCK_RET = [0]      # 0 = 用户取消菜单；改成菜单 ID 可测 RETURNCMD 分支
T._u.TrackPopupMenuEx = lambda menu, flags, x, y, hwnd, *a: (
    results.append(("track", x, y, hwnd)) or _MOCK_RET[0])


def fake_getcursorpos(p):
    o = p._obj          # byref 的 CArgObject，真实对象挂在 _obj
    o.x, o.y = 3456, 789
    return 1
T._u.GetCursorPos = fake_getcursorpos

# 1) 右键单击：Shell 发回调消息 0x0202，lparam 低字 = WM_RBUTTONUP
results.clear()
t._wndproc(1, T.WM_LBUTTONUP, 0, T.WM_RBUTTONUP)
ok1 = any(r[0] == "track" for r in results) and not any(r[0] == "cmd" for r in results)
print("[PASS] 右键 -> 弹出菜单（鼠标位置 %s）" % [r for r in results if r[0] == "track"] if ok1 else "[FAIL] 右键未弹菜单: %s" % results)

# 2) 左键单击：lparam 低字 = WM_LBUTTONUP -> 打开面板
results.clear()
t._wndproc(1, T.WM_LBUTTONUP, 0, T.WM_LBUTTONUP)
ok2 = results == [("cmd", T.MENU_OPEN)]
print("[PASS] 左键 -> 打开面板" if ok2 else "[FAIL] 左键结果: %s" % results)

# 3) 菜单点击「退出」：WM_COMMAND，wparam 低字 = MENU_QUIT
results.clear()
t._wndproc(1, T.WM_COMMAND, T.MENU_QUIT, 0)
ok3 = results == [("cmd", T.MENU_QUIT)]
print("[PASS] 菜单点退出 -> 派发 QUIT" if ok3 else "[FAIL] QUIT 派发: %s" % results)

# 4) wparam 带通知码高位（如 0x00010006）也不影响 ID 解析
results.clear()
t._wndproc(1, T.WM_COMMAND, 0x00010000 | T.MENU_OPEN, 0)
ok4 = results == [("cmd", T.MENU_OPEN)]
print("[PASS] wparam 高位通知码不影响解析" if ok4 else "[FAIL] 高位解析: %s" % results)

# 5) 双击也开面板
results.clear()
t._wndproc(1, T.WM_LBUTTONUP, 0, T.WM_LBUTTONDBLCLK)
ok5 = results == [("cmd", T.MENU_OPEN)]
print("[PASS] 双击 -> 打开面板" if ok5 else "[FAIL] 双击: %s" % results)

# 6) 菜单在鼠标位置弹出（非 0,0），且 owner 窗口句柄传对了
results.clear()
t._wndproc(1, T.WM_LBUTTONUP, 0, T.WM_RBUTTONUP)
track = next((r for r in results if r[0] == "track"), None)
ok6 = track == ("track", 3456, 789, 1)
print("[PASS] 菜单弹在鼠标位置且 owner 句柄正确" if ok6
      else "[FAIL] 菜单位置: %s" % results)

# 7) TPM_RETURNCMD：用户真的选了「退出」时，直接按返回值派发（不经 WM_COMMAND）
results.clear()
_MOCK_RET[0] = T.MENU_QUIT
t._wndproc(1, T.WM_LBUTTONUP, 0, T.WM_RBUTTONUP)
ok7 = ("cmd", T.MENU_QUIT) in results
print("[PASS] 菜单选中项直接派发（RETURNCMD）" if ok7
      else "[FAIL] RETURNCMD 派发: %s" % results)
_MOCK_RET[0] = 0

n_ok = sum((ok1, ok2, ok3, ok4, ok5, ok6, ok7))
print("=" * 50)
print("结果: %d/%d 通过" % (n_ok, 7))
sys.exit(0 if n_ok == 7 else 1)
