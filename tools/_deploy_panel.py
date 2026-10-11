# -*- coding: utf-8 -*-
"""部署新 exe：优雅退出旧实例 → 覆盖 exe → 拉起 → 验证面板就绪。"""
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


def tray_hwnd():
    return u.FindWindowW("LaptopPowerTrayWin", None)


def main():
    h = tray_hwnd()
    print("旧实例 hwnd:", h)
    if h:
        u.PostMessageW(h, 0x0111, 1006, 0)      # WM_COMMAND / MENU_QUIT
        for _ in range(15):
            time.sleep(1)
            if not tray_hwnd():
                break
        print("退出后 hwnd:", tray_hwnd())

    if os.path.isfile(SRC):
        shutil.copy2(SRC, DST)
        print("已覆盖 exe: %.2f MB" % (os.path.getsize(DST) / 1e6))
    else:
        print("!! 找不到", SRC)
        return

    DETACHED, NEW_GROUP = 0x00000008, 0x00000200
    p = subprocess.Popen([DST], cwd=ROOT,
                         creationflags=DETACHED | NEW_GROUP, close_fds=True)
    print("已拉起 pid:", p.pid)

    ok = False
    for i in range(25):
        time.sleep(1)
        try:
            d = json.load(urllib.request.urlopen(
                "http://127.0.0.1:8753/api/status", timeout=3))
            if "battery_info" in d and "care" in d["battery_info"]:
                print("面板已就绪（第 %d 秒）" % (i + 1))
                print("充电上限行:", d["battery_info"]["care"].get("charge_line"))
                ok = True
                break
        except Exception:
            pass
    if not ok:
        print("面板未就绪：沙箱可能已回收常驻进程 —— 请手动双击 exe 启动")
    print("托盘 hwnd:", tray_hwnd())


if __name__ == "__main__":
    main()
