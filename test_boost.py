# -*- coding: utf-8 -*-
"""睿频策略（GHelper Turbo Boost 档）回归测试 —— 全假通道，绝不碰真电源计划。

覆盖三件事：
1. profiles 里 boost 旋钮存在且取值合法（0~4）
2. powercfgctl 能把 powercfg 查不到的隐藏项补进计划
3. 档位写入链路真的会把 boost 写进 AC / DC 两行（含越界钳制）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import powercfgctl as pc
from lp import profiles as P
from lp.powercfgctl import PowerPlan, PowerSetting

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK ] %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


SCHEME = "684f7fa1-32d5-41ed-b743-f71f619fb2d0"
BOOST_GUID = "be337238-0d82-4146-a960-4f3749d470c7"

# --------------------------------------------------------------- 1) 档位表
print("==== 1) 档位表里的 boost 旋钮 ====")
ok("KNOBS 有 boost", "boost" in P.KNOBS, str(P.KNOBS.get("boost")))
ok("boost 候选键含别名", P.KNOBS["boost"][1][0] == "PERFBOOSTMODE", "")
ok("boost 候选键含 GUID 兜底", BOOST_GUID in P.KNOBS["boost"][1], "")

EXPECT = {
    ("gaming", "ac"): 2, ("gaming", "dc"): 1,
    ("office", "ac"): 1, ("office", "dc"): 3,
    ("battery", "ac"): 1, ("battery", "dc"): 3,
    ("saver", "dc"): 0,
}
for (mode, flag), want in sorted(EXPECT.items()):
    got = P.PROFILES[mode][flag].get("boost")
    ok("%s/%s boost=%s" % (mode, flag, want), got == want, "got=%s" % got)

for mode, prof in P.PROFILES.items():
    for flag in ("ac", "dc"):
        v = prof.get(flag, {}).get("boost")
        if v is not None:
            ok("%s/%s 取值合法(0~4)" % (mode, flag), 0 <= v <= 4, "v=%s" % v)
# 每个档位里出现的旋钮都必须能在 KNOBS 查到（否则静默失效）
for mode, prof in P.PROFILES.items():
    for flag in ("ac", "dc"):
        unknown = [k for k in prof.get(flag, {}) if k not in P.KNOBS]
        ok("%s/%s 无未知旋钮" % (mode, flag), not unknown, str(unknown))

# ------------------------------------------------------- 2) 隐藏项补齐
print("==== 2) 隐藏项补齐（powercfg 查不到，powrprof 能读） ====")


class FakePP:
    SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"

    def __init__(self, vals):
        self.vals = vals

    def read_value(self, scheme, sub, guid, on_battery=False):
        return self.vals.get((guid, on_battery))

    def available(self):
        return True


real_pp, real_ok = pc._pp, pc._PP_OK
plan = PowerPlan(SCHEME, "自建")
plan.settings["PROCTHROTTLEMAX"] = PowerSetting(
    "54533251-82be-4824-96c1-47b60b740d00", "SUB_PROCESSOR",
    "bc5038f7-23e0-4960-96d8-33abaf5935ec", "PROCTHROTTLEMAX", "最大处理器状态")

pc._PP_OK = True
pc._pp = FakePP({(BOOST_GUID, False): 2, (BOOST_GUID, True): 1})
pc._fill_hidden(plan)
s = plan.get("PERFBOOSTMODE")
ok("补齐 PERFBOOSTMODE", s is not None, repr(s))
ok("AC 值读对", s.ac == 2 if s else False, "")
ok("DC 值读对", s.dc == 1 if s else False, "")
ok("按 GUID 也能查到", plan.get(BOOST_GUID) is s, "")
ok("取值范围 0~4", (s.vmin, s.vmax) == (0, 4) if s else False, "")

# 已有同名项时不覆盖
before = plan.get("PERFBOOSTMODE")
pc._fill_hidden(plan)
ok("重复补齐不覆盖", plan.get("PERFBOOSTMODE") is before, "")

# 读不到值（机器不支持）时不塞空项
plan2 = PowerPlan(SCHEME, "自建")
pc._pp = FakePP({})
pc._fill_hidden(plan2)
ok("读不到值就不塞空项", plan2.get("PERFBOOSTMODE") is None, "")

# powrprof 不可用时直接跳过
pc._PP_OK = False
plan3 = PowerPlan(SCHEME, "自建")
pc._fill_hidden(plan3)
ok("powrprof 不可用时跳过", plan3.get("PERFBOOSTMODE") is None, "")
pc._PP_OK = True

# ------------------------------------------------------- 3) 档位写入链路
print("==== 3) 档位写入真的落到 AC / DC ====")
writes = []
real_set = pc.set_value


def fake_set(scheme, setting, value, on_battery):
    writes.append((setting.alias or setting.guid, value, on_battery))
    setting.ac = None if on_battery else value
    setting.dc = value if on_battery else None
    return True


pc.set_value = fake_set
try:
    for (mode, flag), want in sorted(EXPECT.items()):
        sub = PowerPlan(SCHEME, "自建")
        bs = PowerSetting("54533251-82be-4824-96c1-47b60b740d00", "SUB_PROCESSOR",
                          BOOST_GUID, "PERFBOOSTMODE", "处理器性能提升模式")
        # 初值刻意设成不等于目标值，否则 apply_values 会（正确地）跳过写入
        seed = 0 if want != 0 else 2
        bs.ac, bs.dc, bs.vmin, bs.vmax = seed, seed, 0, 4
        sub.settings["PERFBOOSTMODE"] = bs
        # manager 的真实路径：内部名 -> KNOBS 候选键（别名优先）-> plan.get
        vals = {P.KNOBS["boost"][1][0]: P.PROFILES[mode][flag]["boost"]}
        writes.clear()
        pc.apply_values(SCHEME, vals, flag == "dc", known=sub.settings)
        hit = [w for w in writes if w[0] == "PERFBOOSTMODE"]
        ok("%s/%s 写入 %s" % (mode, flag, want),
           bool(hit) and hit[-1][1] == want and hit[-1][2] == (flag == "dc"),
           str(hit))

    # 越界钳制
    sub = PowerPlan(SCHEME, "自建")
    bs = PowerSetting("54533251-82be-4824-96c1-47b60b740d00", "SUB_PROCESSOR",
                      BOOST_GUID, "PERFBOOSTMODE", "处理器性能提升模式")
    bs.ac, bs.dc, bs.vmin, bs.vmax = 2, 2, 0, 4
    sub.settings["PERFBOOSTMODE"] = bs
    writes.clear()
    pc.apply_values(SCHEME, {"PERFBOOSTMODE": 9}, False, known=sub.settings)
    ok("越界值被钳到 vmax", bool(writes) and writes[-1][1] == 4, str(writes))

    # 值没变时不重复写
    writes.clear()
    pc.apply_values(SCHEME, {"PERFBOOSTMODE": 4}, False, known=sub.settings)
    ok("已是目标值则不写", not writes, str(writes))
finally:
    pc.set_value = real_set
    pc._pp, pc._PP_OK = real_pp, real_ok

# ------------------------------------------- 4) 与散热策略共用旋钮时的取值一致
print("==== 4) 与散热策略共用旋钮，取值口径一致 ====")
from lp import thermal as TH

for mode, tgt in sorted(TH.HW_TARGET.items()):
    v = tgt.get("PERFBOOSTMODE")
    ok("%s 策略睿频值合法(0~4)" % mode, v is not None and 0 <= v <= 4, "v=%s" % v)
ok("auto 不在策略表里（走还原）", "auto" not in TH.HW_TARGET, "")
ok("散热与档位用同一别名", TH.HW_TARGET["perf"].get("PERFBOOSTMODE") == 2
   and P.PROFILES["gaming"]["ac"]["boost"] == 2, "perf=2 / gaming.ac=2")

print("=" * 60)
print("结果: %d 项通过, %d 项失败" % (PASS, FAIL))
raise SystemExit(1 if FAIL else 0)
