# -*- coding: utf-8 -*-
"""
体检 / 启动项管理 的回归测试

原则：绝不动真实的启动项。注册表用 HKCU 下的临时键，
文件夹/计划任务用临时目录，跑完全部清掉。
"""
from __future__ import annotations

import os
import sys
import shutil
import tempfile
import winreg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import checkup

TEST_KEY = r"Software\LPPA_Test_Run"
_tmp_dirs = []
_ok = _fail = 0


def ck(cond, name):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  ok   %s" % name)
    else:
        _fail += 1
        print("  FAIL %s" % name)


def _mk_key():
    """建一个干净的测试 Run 键"""
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    except Exception:
        pass
    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    winreg.CloseKey(k)
    return TEST_KEY


def _key_values():
    out = {}
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
        i = 0
        while i < 50:
            try:
                n, v, _t = winreg.EnumValue(k, i)
            except OSError:
                break
            out[n] = v
            i += 1
        winreg.CloseKey(k)
    except Exception:
        pass
    return out


def _set_val(name, data):
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY, 0, winreg.KEY_ALL_ACCESS)
    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, data)
    winreg.CloseKey(k)


def main():
    print("== checkup 回归测试 ==")

    _mk_key()
    tmp = tempfile.mkdtemp(prefix="lppa_st_")
    _tmp_dirs.append(tmp)

    # 把数据源全部指向测试位置
    checkup.RUN_KEYS = (("HKCU\\Run", winreg.HKEY_CURRENT_USER, TEST_KEY, False),)
    checkup._startup_dirs = lambda: (("启动(用户)", tmp, False),)
    checkup._tasks_dir = lambda: tmp

    # ---------------------------------------------------------------- 基础清单
    _set_val("Alpha", r"C:\a.exe")
    _set_val("Beta", r"C:\b.exe --tray")
    _set_val(checkup.DISABLE_PREFIX + "Gamma", r"C:\g.exe")
    with open(os.path.join(tmp, "shortcut.lnk"), "w") as f:
        f.write("x")
    with open(os.path.join(tmp, "off.lnk" + checkup.DISABLE_SUFFIX), "w") as f:
        f.write("x")

    items = checkup.startup_items()
    by = {x["name"]: x for x in items}
    ck(len(items) >= 5, "清单能列出注册表+文件夹项（实际 %d）" % len(items))
    ck(by["Alpha"]["enabled"] is True, "正常值 = 启用")
    ck(by["Gamma"]["enabled"] is False, "带禁用前缀 = 已禁用")
    ck(by["Gamma"]["name"] == "Gamma", "禁用项名字剥掉前缀")
    ck(by["shortcut.lnk"]["enabled"] is True, "文件夹快捷方式 = 启用")
    ck(by["off.lnk"]["enabled"] is False, "带禁用后缀的快捷方式 = 已禁用")
    ck(all("id" in x and "where" in x and "kind" in x for x in items), "每项都有 id/where/kind")

    # ---------------------------------------------------------------- 禁用/启用（注册表）
    a = by["Alpha"]
    r = checkup.set_startup(a["id"], False)
    ck(r["ok"] is True, "禁用注册表项成功（%s）" % r.get("msg"))
    vals = _key_values()
    ck(checkup.DISABLE_PREFIX + "Alpha" in vals, "禁用后值名加前缀")
    ck("Alpha" not in vals, "禁用后原值名消失")
    ck(vals.get(checkup.DISABLE_PREFIX + "Alpha") == r"C:\a.exe", "禁用后数据原样保留")

    items2 = {x["name"]: x for x in checkup.startup_items()}
    ck(items2["Alpha"]["enabled"] is False, "重新扫描：Alpha 已显示为关闭")

    r = checkup.set_startup(items2["Alpha"]["id"], True)
    ck(r["ok"] is True, "重新启用成功（%s）" % r.get("msg"))
    ck(_key_values().get("Alpha") == r"C:\a.exe", "启用后值名/数据还原")

    r = checkup.set_startup(by["Gamma"]["id"], False)     # Gamma 本来就是禁用的
    ck(r["ok"] is True and r["msg"] == "already", "已禁用的再禁 → already，不乱改名")
    r = checkup.set_startup(by["Beta"]["id"], True)       # Beta 本来就是启用的
    ck(r["ok"] is True and r["msg"] == "already", "已启用的再启 → already")
    ck(_key_values().get("Beta") == r"C:\b.exe --tray", "already 不会动数据")

    # ---------------------------------------------------------------- 禁用/启用（文件）
    f_item = items2.get("shortcut.lnk") or by["shortcut.lnk"]
    r = checkup.set_startup(f_item["id"], False)
    ck(r["ok"] is True, "快捷方式禁用成功")
    ck(os.path.exists(os.path.join(tmp, "shortcut.lnk" + checkup.DISABLE_SUFFIX)),
       "快捷方式改名加后缀")
    items3 = {x["name"]: x for x in checkup.startup_items()}
    ck(items3["shortcut.lnk"]["enabled"] is False, "重新扫描：快捷方式显示关闭")
    r = checkup.set_startup(items3["shortcut.lnk"]["id"], True)
    ck(r["ok"] is True and os.path.exists(os.path.join(tmp, "shortcut.lnk")),
       "快捷方式恢复原名")

    # ---------------------------------------------------------------- 非法输入
    ck(checkup.set_startup("", True)["ok"] is False, "空 id 被拒绝")
    ck(checkup.set_startup("nonsense", True)["ok"] is False, "非法 id 被拒绝")
    ck(checkup.set_startup("reg:99:x", True)["ok"] is False, "越界索引不崩")
    ck(checkup.set_startup("dir:" + os.path.join(tmp, "ghost.lnk"), True)["ok"] is False,
       "不存在的路径返回失败而不是异常")

    # ---------------------------------------------------------------- 体检项
    res = checkup.run_checks(None)
    ck(isinstance(res.get("items"), list) and res["items"], "体检能产出检查项")
    ck(isinstance(res.get("startups"), list), "体检带启动项清单")
    ck(isinstance(res.get("score"), int) and 0 <= res["score"] <= 100, "分数在 0~100")
    ck(res.get("ms") is not None, "记录耗时")
    keys = {x["key"] for x in res["items"]}
    ck("startup" in keys and "disk" in keys, "含启动项/磁盘检查（%s）" % sorted(keys))
    ck(all(x.get("title") and x.get("value") is not None for x in res["items"]),
       "每项都有标题和值")
    line = checkup.summary_line(res)
    ck("体检" in line and "启动项" in line, "摘要行可读：%s" % line)

    # 深度扫描（走进程采样，允许失败）
    res2 = checkup.run_checks(None, deep=True)
    ck(isinstance(res2.get("top_procs"), list), "深度扫描返回进程列表")

    # ---------------------------------------------------------------- 清理
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    except Exception:
        pass
    for d in _tmp_dirs:
        shutil.rmtree(d, ignore_errors=True)

    print("\n通过 %d，失败 %d" % (_ok, _fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
