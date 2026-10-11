# -*- coding: utf-8 -*-
r"""设置华硕充电上限（60 / 80 / 100）—— 需要管理员权限。

为什么单独一个脚本
------------------
常驻程序是普通权限，写 HKLM 会被拒（实测 PermissionError 13）。所以「改
充电上限」必须是一次**提权动作**：由用户在管理员终端/双击提权脚本执行，
常驻程序只负责**读**与**展示**。

用法（管理员 PowerShell 或 cmd）：
    python tools\_set_charge_limit.py --probe          # 只读，看当前值
    python tools\_set_charge_limit.py --set 100        # 本次充满
    python tools\_set_charge_limit.py --set 80         # 回到平衡模式
    python tools\_set_charge_limit.py --set 60         # 长效使用模式

退出码：0 成功 / 2 权限不足或键不存在 / 3 写完回读不一致
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import winreg

if __package__:
    from ..lp import hw
else:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lp import hw  # noqa: E402

VALID = (60, 80, 100)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", type=int, choices=VALID, default=None)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()

    cur = hw.asus_charge_limit()
    print("当前: %s" % cur.get("label"))
    print("权限: %s" % ("管理员" if is_admin() else "普通用户（只能读）"))

    if a.probe or a.set is None:
        return 0

    if not is_admin():
        print("需要管理员权限才能写 HKLM。请用管理员终端重跑，或右键以管理员运行。")
        return 2

    # 找那个真实存在的键（与 hw.asus_charge_limit 的探测顺序一致）
    target = None
    for hive, path, name in hw._ASUS_CHARGE_KEYS:
        try:
            h = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
            winreg.QueryValueEx(h, name)
            winreg.CloseKey(h)
            target = (hive, path, name)
            break
        except Exception:
            continue
    if target is None:
        print("没找到华硕充电上限的注册表键（非华硕机型或驱动未装）")
        return 2

    hive, path, name = target
    try:
        h = winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(h, name, 0, winreg.REG_DWORD, int(a.set))
        winreg.CloseKey(h)
    except Exception as e:
        print("写入失败: %r" % (e,))
        return 2

    after = hw.asus_charge_limit()
    print("写入后回读: %s" % after.get("label"))
    if after.get("value") != int(a.set):
        print("回读不一致 —— 可能被华硕服务回滚")
        return 3
    print("注册表已改为 %d%%。" % a.set)
    print("注意：这只是厂商设置项，是否真的下发到 EC 要看电池是不是开始/停止充电。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
