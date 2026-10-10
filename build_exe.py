# -*- coding: utf-8 -*-
"""打包 笔记本电源自适应.exe（PyInstaller onefile + noconsole）
产物：dist/笔记本电源自适应.exe（自带 web 面板资源与浣熊图标）
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

PY = sys.executable
cmd = [
    PY, "-m", "PyInstaller",
    "--onefile", "--noconsole",
    "--name", "笔记本电源自适应",
    "--icon", "lp/raccoon.ico",
    "--add-data", "web;web",
    "--add-data", os.path.join("lp", "raccoon.ico") + ";lp",
    "--add-binary", os.path.join("lp", "WebView2Loader.dll") + ";lp",
    # 面板路由里的模块是函数内动态 import，静态分析不一定抓得到，显式声明
    "--hidden-import", "lp.checkup",
    "--hidden-import", "lp.autostart_task",   # 自启加固（main.py 里函数内 import）
    "--hidden-import", "lp.hwprofile",        # 硬件自适应（manager 里函数内 import）
    "--hidden-import", "lp.gpupick",          # 核显优先调度（同上）
    "--clean", "-y",
    "main.py",
]
print(" ".join(cmd))
r = subprocess.call(cmd)
print("PyInstaller exit =", r)
sys.exit(r)
