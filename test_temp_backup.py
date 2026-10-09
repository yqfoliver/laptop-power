# -*- coding: utf-8 -*-
"""CPU 温度双源：降级与交叉校验（2026-10-07）

为什么要有这份测试
------------------
游戏加加的界面让我注意到一件事：它让你「选择传感器」，说明同一项硬件可能存在
多个数据源，而且**源会悄悄失效**。本项目在单一数据源静默失灵这件事上栽过太多次
——退出还原、display.apply、NVML set_mode，全都是"静默出错 + 测试全绿"。

所以这次不是加个功能就算了，要把规则钉死：
  1. 主源可用时**绝不**碰备份源（惰性；这是电池上跑的程序，不能平白多调硬件）
  2. 主源失效必须能降级，上层拿不到 None
  3. 备份源自己坏了/抛异常，不能把整个采样搞崩
  4. 两个源都在时要能比对出"某个源在悄悄漂移"
  5. 主源恢复后状态要归位，不能一直记着自己在降级

用法：python test_temp_backup.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lp import power  # noqa: E402

PASSED = 0
FAILED = 0


def ok(cond, name, extra=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  ✔ %s" % name)
    else:
        FAILED += 1
        print("  ✘ %s   %s" % (name, extra))


class FakeMon:
    """替掉 PDH 真查询，只保留被测的温度逻辑。

    _read 是我们唯一关心的接缝：让它可以按需要返回"有温度"或"没温度"。
    """

    def __init__(self, tz_value=None, available=True):
        self.available = available
        self.tz_value = tz_value
        self.calls = {"tz": 0, "other": 0}

    def _read(self, path):
        if path == power.C_TZ:
            self.calls["tz"] += 1
            if self.tz_value is None:
                return []
            # PDH 返回 [(实例名, 值)]，Thermal Zone 是开尔文
            return [("ACPI\\ThermalZone\\TZ01_0", self.tz_value)]
        self.calls["other"] += 1
        return []


def make(tz_value=None):
    """造一个注入了假读取的 PowerMonitor（不走 _prime，避免依赖真 PDH）。"""
    pm = power.PowerMonitor.__new__(power.PowerMonitor)
    pm.available = True
    pm.nvml = None
    pm._c = {}
    pm._err = ""
    pm._proc_freq_nominal = None
    pm._temp_backup = None
    pm._cross_check_every = 180.0
    pm._tz_fail_streak = 0
    pm.temp_note = ""
    pm.temp_fallbacks = 0
    pm._temp_check_at = 0.0
    pm._temp_diverged = False
    pm._hq = None
    pm._read = FakeMon(tz_value)._read
    return pm


def c_to_k(c):
    return c + 273.15


def main():
    print("=" * 62)
    print("CPU 温度双源：降级 + 交叉校验")
    print("=" * 62)

    # ---- 1. 主源正常：惰性，一次都不碰备份源
    print("\n[1] 主源正常时的惰性")
    calls = {"n": 0}

    def backup():
        calls["n"] += 1
        return 71.0

    pm = make(c_to_k(71.0))
    pm.set_temp_backup(backup, cross_check_every=0)   # 关掉交叉校验单独测惰性
    s = pm.sample(want_gpu=False)
    ok(s.get("cpu_temp") is not None, "读到温度")
    ok(abs(s["cpu_temp"] - 71.0) < 0.5, "温度值正确", "得到 %s" % s.get("cpu_temp"))
    ok(s.get("cpu_temp_src") == "tz", "标记为 tz 源", s.get("cpu_temp_src"))
    ok(calls["n"] == 0, "★ 主源可用时一次都没碰备份源", "被调了 %d 次" % calls["n"])
    ok(pm.temp_fallbacks == 0, "没有记降级")

    # ---- 2. 主源失效 -> 降级
    print("\n[2] 主源失效时降级")
    pm = make(None)
    pm.set_temp_backup(lambda: 74.0, cross_check_every=0)
    s = pm.sample(want_gpu=False)
    ok(s.get("cpu_temp") == 74.0, "★ 上层拿到备份源的值（不是 None）",
       repr(s.get("cpu_temp")))
    ok(s.get("cpu_temp_src") == "backup", "标记为 backup 源")
    ok(pm.temp_fallbacks == 1, "记了一次降级", pm.temp_fallbacks)
    ok("降级" in pm.temp_note, "temp_note 说明了原因", pm.temp_note)

    # ---- 3. 连续失效 -> streak 累加
    print("\n[3] 连续失效计数")
    for _ in range(3):
        pm.sample(want_gpu=False)
    ok(pm._tz_fail_streak == 4, "连续失效次数累加到 4", pm._tz_fail_streak)
    ok(pm.temp_fallbacks == 4, "降级次数累加到 4", pm.temp_fallbacks)

    # ---- 4. 主源恢复 -> 状态归位
    print("\n[4] 主源恢复后归位")
    pm._read = FakeMon(c_to_k(72.0))._read
    s = pm.sample(want_gpu=False)
    ok(s.get("cpu_temp_src") == "tz", "重新标记为 tz 源")
    ok(pm._tz_fail_streak == 0, "★ 失败计数归零", pm._tz_fail_streak)
    ok(pm.temp_note == "", "备注清空", pm.temp_note)

    # ---- 5. 备份源自己坏了
    print("\n[5] 备份源不可用时")
    for label, fn, expect_none in [
        ("返回 None", lambda: None, True),
        ("返回负数", lambda: -5.0, True),
        ("返回超范围", lambda: 999.0, True),
        ("返回 NaN", lambda: float("nan"), True),
        ("抛异常", lambda: (_ for _ in ()).throw(RuntimeError("boom")), True),
        ("返回字符串", lambda: "hot", True),
    ]:
        pm = make(None)
        pm.set_temp_backup(fn, cross_check_every=0)
        try:
            s = pm.sample(want_gpu=False)
            crashed = False
        except Exception as e:
            crashed = True
            s = {}
            print("      (%s 抛到外面: %r)" % (label, e))
        ok(not crashed, "%s 不会让采样崩掉" % label)
        ok(s.get("cpu_temp") is None, "%s 时不采纳该值" % label,
           repr(s.get("cpu_temp")))
        ok(s.get("cpu_temp_src") is None, "%s 时来源标记为 None" % label)

    # ---- 6. 交叉校验：一致
    print("\n[6] 交叉校验（两个源一致）")
    pm = make(c_to_k(70.0))
    pm.set_temp_backup(lambda: 70.1, cross_check_every=0.0)
    pm._temp_check_at = 0.0
    pm._cross_check_every = 1.0      # 强制本轮就校验
    s = pm.sample(want_gpu=False)
    ok("temp_cross_diff" in s, "输出了差比值", s.get("temp_cross_diff"))
    ok(not pm._temp_diverged, "差值 0.1℃ 不算异常", pm._temp_diverged)

    # ---- 7. 交叉校验：漂移
    print("\n[7] 交叉校验（某个源在漂移）")
    pm = make(c_to_k(70.0))
    pm.set_temp_backup(lambda: 88.0, cross_check_every=1.0)
    pm._temp_check_at = 0.0
    s = pm.sample(want_gpu=False)
    ok(abs(s.get("temp_cross_diff", 0) - 18.0) < 0.6,
       "差比值为 18℃", s.get("temp_cross_diff"))
    ok(pm._temp_diverged, "★ 判定为两源不一致", pm._temp_diverged)
    ok("不一致" in pm.temp_note, "备注写明了两个源各自的读数", pm.temp_note)

    # ---- 8. 没注册备份源时
    print("\n[8] 未注册备份源")
    pm = make(None)
    s = pm.sample(want_gpu=False)
    ok(s.get("cpu_temp") is None, "温度为 None 但不崩")
    ok(pm.temp_note == "温度源全部失效", "注明全失效", pm.temp_note)

    print("\n" + "=" * 62)
    print("结果：%d 项通过，%d 项失败" % (PASSED, FAILED))
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
