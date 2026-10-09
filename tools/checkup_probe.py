# -*- coding: utf-8 -*-
"""体检数据源可读写性探测（一次性工具）"""
import os
import sys
import time
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import winreg


def enum_run(hive, path):
    out = []
    try:
        k = winreg.OpenKey(hive, path)
    except Exception as e:
        return None, "open fail: %r" % e
    i = 0
    while True:
        try:
            n, v, t = winreg.EnumValue(k, i)
        except OSError:
            break
        out.append((n, str(v)[:120], t))
        i += 1
        if i > 60:
            break
    winreg.CloseKey(k)
    return out, None


for label, hive, path in (
    ("HKCU Run", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM Run", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM Wow Run", winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ("HKCU RunOnce", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
):
    t0 = time.perf_counter()
    v, err = enum_run(hive, path)
    print("%-14s %.1fms  %s  %s" % (label, (time.perf_counter() - t0) * 1000,
                                    "n=%d" % len(v) if v is not None else err,
                                    (v or [])[:6]))

# 服务（自启的另一半）
svc, err = enum_run(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services")
print("HKLM Services    %s %s" % ("n=%d" % len(svc) if svc is not None else err, ""))

for label, p in (
    ("User Startup", os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")),
    ("Common Startup", os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")),
    ("Sys Tasks", os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "System32", "Tasks")),
    ("User Tasks", os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "TaskScheduler")),
):
    try:
        fs = os.listdir(p)
        print("%-15s OK  n=%d  %s" % (label, len(fs), fs[:6]))
    except Exception as e:
        print("%-15s FAIL %r" % (label, e))

# 计划任务 XML 读一个看看
try:
    p = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "System32", "Tasks")
    f = glob.glob(os.path.join(p, "*.job"))[:1] or [os.path.join(p, x) for x in os.listdir(p)[:1]]
    if f:
        with open(f[0], "rb") as fh:
            head = fh.read(400)
        print("task file %s -> %r" % (os.path.basename(f[0]), head[:200]))
except Exception as e:
    print("task read fail", repr(e))
