# -*- coding: utf-8 -*-
"""把自启 Run 键直接指向 exe（带 --autostart 标记），绕过 wscript/vbs 中间层。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import winreg  # noqa: E402

EXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "笔记本电源自适应.exe")
CMD = '"%s" --autostart' % EXE

k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                   r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                   winreg.KEY_SET_VALUE)
winreg.SetValueEx(k, "LaptopPowerAuto", 0, winreg.REG_SZ, CMD)
winreg.CloseKey(k)

k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                   r"Software\Microsoft\Windows\CurrentVersion\Run")
v = winreg.QueryValueEx(k, "LaptopPowerAuto")[0]
print("Run 键 =", v)
print("exe 存在:", os.path.isfile(EXE))
