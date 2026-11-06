# -*- coding: utf-8 -*-
"""离电时刻学习（lp/departure.py）的回归测试。

这段逻辑决定「要不要为了出门把电池充到 100%」——误判的代价是整天顶在满电
（正是养护要避免的），漏判的代价是出门只有 80%。所以每条边界都要钉住：
时间窗、样本下限、回看天数、跨零点、坏数据、去重。
"""
from __future__ import annotations

import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import departure

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


def ts_at(day_offset: int, hh: int, mm: int, base: float = None) -> float:
    """今天往前第 N 天的 hh:mm（本地时区）."""
    lt = time.localtime(base or time.time())
    d = datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday) - datetime.timedelta(days=day_offset)
    return time.mktime((d.year, d.month, d.day, hh, mm, 0, 0, 0, -1))


def main() -> int:
    now = ts_at(0, 7, 40)

    print("record：去重 / 容量 / 脏数据")
    seq = []
    ok(departure.record(seq, 1000.0) is True, "首次记录成功")
    ok(len(seq) == 1, "序列写入 1 条")
    ok(departure.record(seq, 1000.0 + 60) is False, "30 分钟内重复 → 不记（合盖抖动）")
    ok(len(seq) == 1, "重复记录未污染序列")
    ok(departure.record(seq, 1000.0 + departure.DEDUPE_S + 1) is True,
       "超过去重窗口 → 记为新一轮")
    ok(departure.record(seq, "not-a-time") is False, "非数字时间戳被拒")
    fresh = []
    ok(departure.record(fresh, 0) is False and fresh == [],
       "0 时间戳不写进序列（否则文件里躺脏值）")
    ok(departure.record(fresh, 1e18) is False, "毫秒级/离谱时间戳被拒")
    ok(departure.record(fresh, -1) is False, "负数被拒")
    big = list(range(1, 400))
    departure.record(big, 400 * departure.DEDUPE_S)
    ok(len(big) <= departure.MAX_KEEP, "序列有容量上限（防文件膨胀）", len(big))

    print("predict：样本不足")
    ok(departure.predict([], now) is None, "空序列 → None")
    two = [ts_at(1, 8, 10, now), ts_at(2, 8, 10, now)]
    ok(departure.predict(two, now) is None, "只有 2 次同点离电 → 不当规律")

    print("predict：命中窗口")
    three = [ts_at(1, 8, 10, now), ts_at(2, 8, 10, now), ts_at(3, 8, 10, now)]
    p = departure.predict(three, now)
    ok(p is not None, "3 次同点离电 → 有预测", p)
    if p:
        ok(p["hhmm"] == "08:10", "报出的时刻取中位数", p.get("hhmm"))
        ok(p["n"] == 3, "样本数如实", p.get("n"))
        ok(p["in_min"] == 30, "距现在 30 分钟", p.get("in_min"))
        ok(p["due"] is True, "提前量 40 分钟内 → due=True")
    early = departure.predict(three, ts_at(0, 6, 0, now))
    ok(early is None, "离得远（06:00）→ 不进窗口")
    late = departure.predict(three, ts_at(0, 8, 40, now))
    ok(late is not None and late["due"] is False, "已经过了点（08:40）→ 不 due（来不及充）",
       late)

    print("predict：窗口边界")
    edge = [ts_at(1, 10, 0, now), ts_at(2, 10, 0, now), ts_at(3, 10, 0, now)]
    ok(departure.predict(edge, now) is None,
       "10:00 的规律在 07:40 不该命中（超 ±45 分钟窗）")
    near = [ts_at(1, 8, 20, now), ts_at(2, 8, 25, now), ts_at(3, 8, 15, now)]
    p2 = departure.predict(near, now)
    ok(p2 is not None and 40 <= p2["in_min"] <= 60, "08:20 上下 → 命中且 in_min≈45", p2)

    print("predict：回看天数 / 未来时间戳")
    old = [ts_at(30, 8, 10, now), ts_at(31, 8, 10, now), ts_at(32, 8, 10, now)]
    ok(departure.predict(old, now) is None, "三周以前的老习惯不算（作息会变）")
    fut = [now + 3 * 86400, now + 4 * 86400, now + 5 * 86400]
    ok(departure.predict(fut, now) is None, "远超当前的时间戳（时钟错乱）被丢弃")

    print("predict：跨零点")
    night = [ts_at(1, 23, 50, now), ts_at(2, 23, 50, now), ts_at(3, 23, 50, now)]
    p3 = departure.predict(night, ts_at(0, 0, 10, now))
    ok(p3 is not None and p3["hhmm"] == "23:50", "00:10 能认出 23:50 的规律", p3)
    ok(p3 is not None and p3["in_min"] < 0, "已过点 → in_min 为负", p3)

    print("predict：坏数据不崩")
    dirty = [None, "x", -5, 0, 1e30] + three
    p4 = departure.predict(dirty, now)
    ok(p4 is not None and p4["hhmm"] == "08:10", "混入垃圾仍能得出正确结论", p4)
    ok(departure.predict("not-a-list", now) is None, "序列不是 list → None（不抛）")

    print("\n%d 项通过，%d 项失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
