# -*- coding: utf-8 -*-
"""部署新 exe：优雅退出旧实例 → 覆盖 exe → 拉起新实例并验证。"""
import ctypes
import os
import shutil
import subprocess
import time
import urllib.request
import json

u = ctypes.windll.user32
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dist", "笔记本电源自适应.exe")
DST = os.path.join(ROOT, "笔记本电源自适应.exe")

h = u.FindWindowW("LaptopPowerTrayWin", None)
print("旧实例 hwnd:", h)
if h:
    u.PostMessageW(h, 0x0111, 1006, 0)      # WM_COMMAND / MENU_QUIT
    for _ in range(12):
        time.sleep(1)
        if not u.FindWindowW("LaptopPowerTrayWin", None):
            break
    print("退出后 hwnd:", u.FindWindowW("LaptopPowerTrayWin", None))

if os.path.isfile(SRC):
    shutil.copy2(SRC, DST)
    print("已覆盖 exe: %.2f MB" % (os.path.getsize(DST) / 1e6))
else:
    print("!! 找不到", SRC)

DETACHED = 0x00000008
NEW_GROUP = 0x00000200
p = subprocess.Popen([DST], cwd=ROOT,
                     creationflags=DETACHED | NEW_GROUP, close_fds=True)
print("已拉起 pid:", p.pid)

ok = False
for i in range(20):
    time.sleep(1)
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8753/api/status", timeout=3))
        if "gpupick" in d:
            print("面板已就绪（第 %d 秒）" % (i + 1))
            print("gpupick:", json.dumps(d["gpupick"], ensure_ascii=False)[:300])
            print("igpu_tier:", d["gpupick"].get("igpu_tier"))
            ok = True
            break
    except Exception:
        pass
if not ok:
    print("面板未就绪（沙箱可能已回收进程，需用户双击 exe）")
print("托盘 hwnd:", u.FindWindowW("LaptopPowerTrayWin", None))
