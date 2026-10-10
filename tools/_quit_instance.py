# -*- coding: utf-8 -*-
"""优雅退出正在运行的实例（为覆盖 exe 做准备）。"""
import ctypes
import time

u = ctypes.windll.user32
h = u.FindWindowW("LaptopPowerTrayWin", None)
print("旧实例 hwnd:", h)
if not h:
    print("没有运行中的实例")
    raise SystemExit(0)
u.PostMessageW(h, 0x0111, 1006, 0)      # WM_COMMAND / MENU_QUIT
for _ in range(15):
    time.sleep(1)
    if not u.FindWindowW("LaptopPowerTrayWin", None):
        break
left = u.FindWindowW("LaptopPowerTrayWin", None)
print("退出后 hwnd:", left)
