# -*- coding: utf-8 -*-
"""
合盖不休眠（Clamshell）回归测试（第六轮审查）
=============================================
重点覆盖「进程被强杀后留下的坏状态」：

  1. 上次退出时还在接管状态（clamshell.json 里 engaged=true），但计划里
     LIDACTION=0 —— 新进程启动必须收尾还原，否则用户合盖机器不睡、
     界面还显示「未开启」。旧代码只看内存里的 _engaged，永远发现不了。
  2. 老版本写下的 json 没有 engaged 字段：不能误判成「需要还原」。
  3. 正常接管 / 正常还原都要把 engaged 落盘。
  4. 没有原值 / 没有计划 GUID 时 recover() 不写任何东西。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import clamshell as C

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


SCHEME = "684f7fa1-32d5-41ed-b743-f71f619fb2d0"


class FakePP:
    def __init__(self, ac=1, dc=1, fail=False):
        self.state = {False: ac, True: dc}      # False=插电, True=电池
        self.writes = []
        self.fail = fail
        self.activated = 0

    def available(self):
        return True

    def lid_action(self, scheme, on_battery=False):
        return self.state.get(bool(on_battery))

    def set_lid_action(self, scheme, value, on_battery=False):
        if self.fail:
            return False
        self.writes.append((value, bool(on_battery)))
        self.state[bool(on_battery)] = value
        return True

    def set_active(self, scheme):
        self.activated += 1
        return True

    LID_LABEL = {0: "不采取任何操作", 1: "睡眠", 2: "休眠", 3: "关机"}


def tmp_path():
    fd, p = tempfile.mkstemp(suffix=".json", prefix="lp_clam_")
    os.close(fd)
    os.unlink(p)
    return p


def make(json_data=None, cfg=None, pp=None):
    p = tmp_path()
    if json_data is not None:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(json_data, f)
    c = C.ClamshellCtl(cfg or {"clamshell_enabled": True}, SCHEME, data_path=p)
    c.pp = pp or FakePP()                      # 注入假通道
    C.pp = c.pp
    C._monitor_count = lambda: 2               # 假装接着外接屏
    return c, c.pp, p


print("== 1) 强杀后遗留的接管状态必须收尾 ==")
c, pp, path = make({"saved": {"ac": 1, "dc": 2}, "engaged": True,
                    "updated": "2026-10-28 10:00:00"}, pp=FakePP(ac=0, dc=0))
ok(c.recover_pending(), "读到 engaged=true → 标记待收尾")
ok(not c.engaged, "内存里的 engaged 仍是 False（与界面一致）")
ok(c.recover() is True, "recover() 执行还原")
ok(pp.state == {False: 1, True: 2}, "合盖动作回到原值", str(pp.state))
ok(not c.recover_pending(), "收尾标记清掉")
with open(path, encoding="utf-8") as f:
    d = json.load(f)
ok(d.get("engaged") is False, "落盘记下「已退出接管」", str(d))
ok(c.recover() is False and pp.writes == [(1, False), (2, True)],
   "重复 recover 不重复写", str(pp.writes))

print("== 2) 老版本 json（没有 engaged 字段）不能误还原 ==")
c2, pp2, _ = make({"saved": {"ac": 1, "dc": 1}}, pp=FakePP(ac=1, dc=1))
ok(not c2.recover_pending(), "缺字段按「未接管」处理")
ok(c2.recover() is False and pp2.writes == [], "不写任何值")

print("== 3) 正常接管 / 还原都落盘 ==")
c3, pp3, path3 = make(None, {"clamshell_enabled": True}, pp=FakePP(ac=1, dc=1))
note = c3.tick(True, True)
ok(c3.engaged and "蛤壳" in (note or ""), "条件满足 → 接管", str(note))
ok(pp3.state == {False: 0, True: 0}, "LIDACTION 写成 0")
with open(path3, encoding="utf-8") as f:
    d3 = json.load(f)
ok(d3.get("engaged") is True, "接管状态已落盘（崩溃后能被发现）")
ok(d3.get("saved") == {"ac": 1, "dc": 1}, "原值已落盘")
c3.reset(reason="测试")
ok(pp3.state == {False: 1, True: 1}, "reset 还原", str(pp3.state))
with open(path3, encoding="utf-8") as f:
    d3b = json.load(f)
ok(d3b.get("engaged") is False, "reset 后落盘为未接管")

print("== 4) 无原值 / 无计划时不乱写 ==")
c4, pp4, _ = make({"saved": {}, "engaged": True}, pp=FakePP())
ok(c4.recover() is False and pp4.writes == [], "空原值表不写")
c5 = C.ClamshellCtl({"clamshell_enabled": True}, "", data_path=tmp_path())
c5.pp = FakePP()
C.pp = c5.pp
ok(c5.recover() is False, "没有计划 GUID 时直接放弃")

print("== 5) 开关关闭时也要能收尾（manager 的调用顺序） ==")
c6, pp6, _ = make({"saved": {"ac": 1, "dc": 1}, "engaged": True},
                  {"clamshell_enabled": False}, pp=FakePP(ac=0, dc=0))
ok(c6.recover_pending() and not c6.enabled,
   "开关已关 + 遗留接管状态（最危险组合）")
ok(c6.recover() and pp6.state == {False: 1, True: 1}, "仍然完成还原")

print()
print("结果: %d 通过, %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
