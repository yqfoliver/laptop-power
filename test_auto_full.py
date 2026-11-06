# -*- coding: utf-8 -*-
"""离电前自动充满（2026-11-06 A）的状态机测试。

这条链最容易出的不是崩溃，而是**悄悄做错事**：
  · 没权限还去写 → 每次弹一次 UAC（人不在跟前就变成挂着的对话框）
  · 同一天反复触发 → 充到 100% 被拔、插回来又充，白折腾电池
  · 已经生效还重复下单 → intent 时间被不断续期，养护形同关闭
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import config
from lp.manager import Manager

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


class FakeCare:
    enabled = True
    full_limit = 100
    target_limit = 80
    intent_h = 24.0

    def __init__(self, pred=None, active=False, write_ok=True, due=False):
        self.pred = pred
        self._active = active
        self.write_ok = write_ok
        self.calls = []
        self._due = due            # 还欠一次「交回养护上限」
        self.limits = []           # apply_limit 的调用记录

    def next_departure(self, now=None):
        return self.pred

    def intent_active(self):
        return self._active

    def restore_due(self):
        return self._due

    def clear_restore_due(self):
        self._due = False

    def apply_limit(self, value, track=True):
        self.limits.append(value)
        return {"ok": True}

    def set_full_intent(self, reason="manual", hours=None):
        self.calls.append(reason)
        return {"ok": True,
                "write": {"ok": self.write_ok,
                          "reason": "" if self.write_ok else "写入被拒绝"}}


def mk(care, writable=True, pct=76, on_ac=True):
    m = Manager.__new__(Manager)
    m.cfg = dict(config.DEFAULT_CONFIG)
    m.care = care
    m.last_battery = {"percent": pct}
    m._auto_full = {}
    m._auto_full_day = ""
    m._chg_writable = writable
    m._err = lambda *a, **k: None
    return m


DUE = {"hhmm": "08:10", "n": 4, "in_min": 30, "due": True, "lead_min": 40}
SOON = {"hhmm": "08:10", "n": 4, "in_min": 90, "due": False, "lead_min": 40}


def main() -> int:
    print("不触发的情形")
    c = FakeCare(pred=None)
    m = mk(c)
    m.auto_full_tick(True)
    ok(m._auto_full == {} and c.calls == [], "没有规律 → 什么都不做")
    c = FakeCare(pred=SOON)
    m = mk(c)
    m.auto_full_tick(True)
    ok(m._auto_full.get("state") == "watching" and c.calls == [],
       "还没到提前量 → 只观察不下单", m._auto_full)
    c = FakeCare(pred=DUE)
    m = mk(c)
    m.auto_full_tick(False)
    ok(m._auto_full == {} and c.calls == [], "离电时不动作（本来也没在充）")
    m = mk(c)
    m.cfg["auto_full_enabled"] = False
    m.auto_full_tick(True)
    ok(m._auto_full == {} and c.calls == [], "开关关掉就完全不介入")

    print("权限：不能写就绝不动手")
    c = FakeCare(pred=DUE)
    m = mk(c, writable=False)
    m.auto_full_tick(True)
    ok(c.calls == [], "写不了时**不调用** set_full_intent（不弹 UAC）")
    ok(m._auto_full.get("state") == "need_admin", "状态如实标 need_admin",
       m._auto_full)
    ok(m._auto_full.get("hhmm") == "08:10", "仍把学到的时刻带出来给面板显示")

    print("拦截：已生效 / 已经满电 / 今天做过")
    c = FakeCare(pred=DUE, active=True)
    m = mk(c)
    m.auto_full_tick(True)
    ok(c.calls == [] and m._auto_full.get("state") == "active",
       "已经因为你出门放开了 → 不重复下单")
    c = FakeCare(pred=DUE)
    m = mk(c, pct=100)
    m.auto_full_tick(True)
    ok(c.calls == [] and m._auto_full.get("state") == "already_full",
       "已经满电 → 不用再写（省一次无意义的注册表写）")
    c = FakeCare(pred=DUE)
    m = mk(c)
    m.auto_full_tick(True)
    ok(c.calls == ["auto_travel"], "正常路径下单一次，reason=auto_travel", c.calls)
    ok(m._auto_full.get("state") == "filling", "状态标 filling", m._auto_full)
    ok(m._auto_full_day == time.strftime("%Y-%m-%d"), "记下当天已处理")
    # 同一天里用户手动点了「回养护」：intent 没了，但今天不该再自动充一次
    c._active = False
    m.auto_full_tick(True)
    ok(c.calls == ["auto_travel"], "同一天即使被手动改回也不再重复充满", c.calls)
    ok(m._auto_full.get("state") == "done_today", "状态标 done_today", m._auto_full)

    print("写入失败：如实标记，不假装成功")
    c = FakeCare(pred=DUE, write_ok=False)
    m = mk(c)
    m.auto_full_tick(True)
    ok(c.calls == ["auto_travel"], "尝试写入一次")
    ok(m._auto_full.get("state") == "failed", "失败标 failed（面板会看到）", m._auto_full)

    print("收尾：出门回来要交回养护上限（否则养护被自己关掉）")
    c = FakeCare(pred=None, due=True)
    m = mk(c)
    m.auto_full_tick(True)
    ok(c.limits == [80], "欠着还原时，插电第一件事是写回养护值", c.limits)
    ok(c.restore_due() is False, "写成功后欠账清掉")
    c = FakeCare(pred=None, due=True)
    m = mk(c, writable=False)
    m.auto_full_tick(True)
    ok(c.limits == [] and c.restore_due() is True,
       "写不了就不动（欠账留着，等用户点「回养护」）")
    c = FakeCare(pred=DUE, active=True, due=True)
    m = mk(c)
    m.auto_full_tick(True)
    ok(c.limits == [], "这次充满还在生效中 → 不许提前还原（会把刚放开的又收回去）")

    print("开关字段确实存在于配置与护栏里")
    ok("auto_full_enabled" in config.DEFAULT_CONFIG, "auto_full_enabled 已声明")
    ok("manual_ttl_min" in config._NUM_LIMITS, "manual_ttl_min 进了数值护栏")
    cfg = dict(config.DEFAULT_CONFIG, manual_ttl_min="abc")
    config.sanitize(cfg)
    ok(isinstance(cfg.get("manual_ttl_min"), (int, float)),
       "字符串坏值会被 sanitize 修回数字", cfg.get("manual_ttl_min"))

    print("\n%d 项通过，%d 项失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
