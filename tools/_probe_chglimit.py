# -*- coding: utf-8 -*-
"""只读探测：华硕充电上限当前值 + 本进程有没有写入权限（不写任何东西）。"""
import ctypes
import os
import sys
import winreg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEYS = (
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\ASUS\ASUS System Control Interface\AsusOptimization\ASUS Keyboard Hotkeys",
     "ChargingRate"),
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\ASUS\ASUS System Control Interface\AsusOptimization",
     "ChargingRate"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ASUS\ASUS_Optimize_Setting",
     "Battery_HealthCharging"),
)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


print("进程权限: %s" % ("管理员" if is_admin() else "普通用户"))

for hive, path, name in KEYS:
    print("\n键: HKLM\\%s\\%s" % (path, name))
    try:
        h = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        v, t = winreg.QueryValueEx(h, name)
        winreg.CloseKey(h)
        print("  当前值: %r (类型 %d)" % (v, t))
    except FileNotFoundError:
        print("  不存在")
        continue
    except Exception as e:
        print("  读失败: %r" % (e,))
        continue
    # 试着以「写」权限打开 —— 只开句柄不写值，用来判断 ACL 是否放行
    try:
        h2 = winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE)
        winreg.CloseKey(h2)
        print("  写权限: 可写（当前令牌能直接改）")
    except Exception as e:
        print("  写权限: 拒绝 -> %r" % (e,))

try:
    from lp import hw
    print("\nhw.asus_charge_limit():", hw.asus_charge_limit())
    print("has_myasus():", hw.has_myasus())
except Exception as e:
    print("\nhw 读取失败:", e)
