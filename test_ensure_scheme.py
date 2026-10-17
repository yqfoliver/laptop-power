# -*- coding: utf-8 -*-
"""ensure_scheme 瞬断重试回归测试（2026-10-17）。

背景：01:12 error.log 出现全天唯一一条「创建电源计划失败，请确认以管理员
身份运行」——duplicate_scheme 在更新安装期瞬断，原实现一失败就抛，整个
档位切换静默报废。现在改成失败后 1.5s 重试一次。

全部替身注入，不碰真实电源计划、不写 config.json。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import config, powercfgctl as pc
from lp.manager import Manager

N = [0]
FAILS = []


def ok(cond, msg, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append("%s  %s" % (msg, extra))


def mk_manager():
    m = Manager(config.load())
    m.cfg["custom_scheme"] = ""            # 强制走「创建」分支
    m.cfg["original_scheme"] = ""
    return m


def install_stubs(calls, fail_first=True):
    """把 powercfg/计划相关调用全部换成记录型替身"""
    pc.active_scheme = lambda: "BASE-GUID"
    pc.list_schemes = lambda: {"BASE-GUID": object()}
    pc.rename_scheme = lambda guid, name: True
    config.save = lambda cfg: None         # 别碰真实 config.json

    if fail_first:
        def dup(base):
            calls.append(base)
            return None if len(calls) == 1 else "NEW-GUID"
    else:
        def dup(base):
            calls.append(base)
            return None
    pc.duplicate_scheme = dup


print("== 1) 首次瞬断：重试一次即成功 ==")
calls = []
m = mk_manager()
install_stubs(calls, fail_first=True)
try:
    guid = m.ensure_scheme()
    ok(guid == "NEW-GUID", "重试后拿到新计划 GUID", guid)
    ok(len(calls) == 2, "duplicate_scheme 共调用 2 次", calls)
    ok(m.cfg.get("custom_scheme") == "NEW-GUID", "cfg 记下新 GUID",
       m.cfg.get("custom_scheme"))
except Exception as e:
    ok(False, "不应抛异常", repr(e))

print("== 2) 连续失败：仍然抛错（不静默冒充成功） ==")
calls = []
m = mk_manager()
install_stubs(calls, fail_first=False)
try:
    m.ensure_scheme()
    ok(False, "两次都失败必须抛 RuntimeError", "no exception")
except RuntimeError as e:
    ok("管理员" in str(e), "抛错文案保留管理员提示", str(e))
    ok(len(calls) == 2, "共尝试 2 次（一次原发+一次重试）", calls)

print("== 3) 已有自建计划：不重复创建 ==")
m = mk_manager()
m.cfg["custom_scheme"] = "EXIST-GUID"
pc.list_schemes = lambda: {"EXIST-GUID": object()}
called = []
pc.duplicate_scheme = lambda base: called.append(base) or "X"
ok(m.ensure_scheme() == "EXIST-GUID", "直接复用已有计划", "")
ok(not called, "不调用 duplicate", called)

print("\n%d 项断言，失败 %d" % (N[0], len(FAILS)))
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
