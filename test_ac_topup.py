# -*- coding: utf-8 -*-
"""插电补电（top-up）链路回归测试 —— 2026-10-17/18 Type-C 事故的固化。

背景：Type-C 打游戏供不上时，ACPI 的充/放电标志会**同时翻假**，固件在该态
沿袭「负 = 放电方向」上报 Rate。旧代码只按标志翻符号 ⇒ rate_w 记成「充电」
⇒ PD 控制器全程认为供得上：不标定上限、不定档、不预判、不压制，电池一路
补电到 5%。本文件锁死三件事：

  1. battery.py：补电态的负 Rate 必须翻成「放电为正」，rate_avg_w 也要维护；
  2. battery.py：输出 topup / state_cn=「插电补电中」，面板三态有据可依；
  3. batterycare：AC 侧补电放电必须计入能量吞吐（ac_drain_wh）与等效循环。

不碰真实电池硬件：_ioctl / GetSystemPowerStatus 全部换成脚本化假实现。
"""

import os
import sys
from ctypes import byref, memmove, sizeof

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lp import battery as bt          # noqa: E402
from lp.battery import BatteryMonitor, BATTERY_STATUS, CHARGING, DISCHARGING  # noqa: E402

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


class FakeBattery:
    """脚本化电池：每次读状态返回预设的 (PowerState, Rate_mW, Capacity)。"""

    def __init__(self, script, ac=True):
        self.script = list(script)
        self.ac = ac
        self.i = 0

    def install(self, mon):
        """把假读法装进 BatteryMonitor（_handle/_tag 走脚本，不碰真设备）。"""
        mon._handle = 1                # 真值即可，_ioctl 被替换不会用到
        mon._tag = 1
        mon.present = True
        mon.designed_mwh = 61500
        mon.full_mwh = 61500
        mon.cycles = 12
        mon._fail = 0
        mon._next_try = 0.0

        def fake_ioctl(code, inbuf, insize, outbuf, outsize):
            if code == bt.IOCTL_BATTERY_QUERY_INFORMATION:
                # 信息查询：给一份合法容量表，不消耗状态脚本
                bi = bt.BATTERY_INFORMATION()
                bi.DesignedCapacity = 61500
                bi.FullChargedCapacity = 61500
                bi.CycleCount = 12
                memmove(outbuf, byref(bi), sizeof(bi))
                return True
            state, rate_mw, cap = self.script[min(self.i, len(self.script) - 1)]
            self.i += 1
            bs = BATTERY_STATUS()
            bs.PowerState = state
            bs.Capacity = cap
            bs.Voltage = 16000
            bs.Rate = rate_mw
            memmove(outbuf, byref(bs), sizeof(BATTERY_STATUS))
            return True
        mon._ioctl = fake_ioctl

        def fake_gps(p):
            fb = bt._SYSTEM_POWER_STATUS()
            fb.ACLineStatus = 1 if self.ac else 0
            fb.BatteryLifePercent = 255
            fb.BatteryLifeTime = 0xFFFFFFFF
            memmove(p, byref(fb), sizeof(fb))
            return 1
        bt._k32.GetSystemPowerStatus = fake_gps


# ------------------------------------------------------------ 符号与三态
def test_topup_sign():
    print("== 插电补电态：负 Rate = 放电方向，必须翻正（10-18 事故根因）==")
    # 现场复刻：插电、两标志皆假、固件 Rate=-3400mW（=放电 3.4W）
    fb = FakeBattery([(0, -3400, 40000)])
    mon = BatteryMonitor()
    fb.install(mon)
    s = mon.sample(force=True)
    ok(s["rate_w"] is not None and s["rate_w"] > 0,
       "补电态 Rate 翻成「放电为正」: %s" % s["rate_w"])
    ok(abs(s["rate_w"] - 3.4) < 0.01, "数值取绝对值正确: %s" % s["rate_w"])
    ok(s["topup"] is True, "topup 标记为真")
    ok(s["state_cn"] == "插电补电中", "state_cn=插电补电中: %s" % s["state_cn"])
    ok(s["discharging"] is False and s["charging"] is False,
       "两标志仍如实上报为假（补电态本来就没有标志）")
    ok(s["ac"] is True, "AC 在线")


def test_other_states_signs():
    print("== 其余三态符号不受影响 ==")
    # 充电态：CHARGING 标志 + 正 Rate → 翻成负（充电为负）
    fb = FakeBattery([(CHARGING, 12000, 50000)])
    mon = BatteryMonitor()
    fb.install(mon)
    s = mon.sample(force=True)
    ok(abs(s["rate_w"] + 12.0) < 0.01, "充电：正 Rate 翻成负: %s" % s["rate_w"])
    ok(s["topup"] is False and s["state_cn"] == "充电中", "state_cn=充电中")
    # 离电放电：DISCHARGING 标志 + 负 Rate → 翻成正
    fb2 = FakeBattery([(DISCHARGING, -21000, 40000)], ac=False)
    mon2 = BatteryMonitor()
    fb2.install(mon2)
    s2 = mon2.sample(force=True)
    ok(abs(s2["rate_w"] - 21.0) < 0.01, "离电放电：翻成正: %s" % s2["rate_w"])
    ok(s2["state_cn"] == "放电中", "state_cn=放电中")
    # 满电搁置：无标志 + Rate≈0 → 不翻（0 翻不翻都无歧义）
    fb3 = FakeBattery([(0, 0, 61500)])
    mon3 = BatteryMonitor()
    fb3.install(mon3)
    s3 = mon3.sample(force=True)
    ok(abs(s3["rate_w"]) < 0.01 and s3["topup"] is False,
       "搁置：Rate≈0 不判补电: %s" % s3["rate_w"])
    ok(s3["state_cn"] == "已接电源", "state_cn=已接电源: %s" % s3["state_cn"])


def test_topup_ewma():
    print("== 补电态维护 rate_avg_w（PD 控制器优先用平滑值）==")
    fb = FakeBattery([(0, -14000, 40000), (0, -18000, 40000),
                      (CHARGING, 5000, 50000)])
    mon = BatteryMonitor()
    fb.install(mon)
    s1 = mon.sample(force=True)
    ok(s1["rate_avg_w"] is not None and abs(s1["rate_avg_w"] - 14.0) < 0.01,
       "第一拍即初始化平滑值: %s" % s1["rate_avg_w"])
    s2 = mon.sample(force=True)
    ok(s2["rate_avg_w"] is not None and 14.0 < s2["rate_avg_w"] < 18.0,
       "第二拍平滑到 14~18 之间（0.3 权重）: %s" % s2["rate_avg_w"])
    s3 = mon.sample(force=True)
    ok(s3["rate_avg_w"] is None, "回到充电即清零（ETA 不挂旧值）: %s" % s3["rate_avg_w"])


def test_pd_sees_topup():
    print("== PD 控制器视角：补电就是「放电」，链路要能动手 ==")
    # 复刻 10-18：整机 81W，补电放电 14W（翻正后）
    from lp.pdbudget import PdBudget
    pd = PdBudget({"pd_guard_enabled": True, "pd_margin_w": 1.5,
                   "pd_hold_s": 12.0, "pd_release_s": 45.0, "pd_cpu_step": 10,
                   "pd_cpu_min": 60, "pd_dim_level": 50, "pd_low_hz": True,
                   "pd_low_hz_value": 60})
    # 大电流走快通道（3s）：两拍就够爬到 S4
    reqs = []
    for _ in range(4):
        r = pd.feed(True, 14.0, 5.0, machine_w=81.0)
        if r:
            reqs.append(r)
    ok(pd.stage >= 1, "补电立即进入压制（不再全程沉默）: stage=%d" % pd.stage)
    ok(any(x.get("refresh") for x in reqs), "S1 降刷已请求")
    ok(pd.supply_w is not None and pd.supply_firm,
       "放电标定出精确上限 %.1fW（firm）" % (pd.supply_w or 0))
    ok(abs(pd.supply_w - (81.0 - 14.0)) < 0.1,
       "上限 = 整机 − 补电 = 67W: %s" % pd.supply_w)


# ------------------------------------------------------------ 养护计账
def test_care_ac_drain():
    print("== 养护统计：插电补电放电必须有桶（10-18 记 0.00 vs 实际 78Wh）==")
    from lp import batterycare as bc
    import tempfile
    tmp = tempfile.mkdtemp(prefix="care_topup_")
    path = os.path.join(tmp, "battery_care.json")

    class FakePC:
        def run_powercfg(self, args):
            return ""
    bc.pc = FakePC()
    NO_ASUS = {"supported": False, "value": None, "mode": None,
               "mode_cn": None, "label": "不可用", "myasus": False}
    care = bc.BatteryCare({"care_full_soc_pct": 95, "care_deep_soc_pct": 20,
                           "care_hot_charge_c": 88}, path,
                          chg_reader=(lambda: dict(NO_ASUS)))
    # AC 在线、不充不放（标志全假）、补电放电 14W —— 正是补电态采样长相
    batt = {"ac": True, "percent": 62, "charging": False, "discharging": False,
            "rate_w": 14.0, "full_mwh": 61500, "rate_avg_w": 14.0}
    # 喂 3600 秒（1 小时）⇒ 14Wh
    for _ in range(72):
        care.feed(batt, None, dt=50.0)
    day = care._data["days"][care._day_key()]
    ok(abs(day.get("ac_drain_wh", 0.0) - 14.0) < 0.2,
       "ac_drain_wh ≈ 14Wh: %s" % day.get("ac_drain_wh"))
    ok(day.get("discharge_wh", 0.0) == 0.0,
       "discharge_wh 仍只记离电放电（口径不混）: %s" % day.get("discharge_wh"))
    st = care.report()["stat"]
    ok(st.get("ac_drain_wh") == 14.0, "stat 透出 ac_drain_wh: %s" % st.get("ac_drain_wh"))
    ok(st.get("cycles_est") is not None and abs(st["cycles_est"] - 0.23) < 0.01,
       "等效循环按总放电折算 14/61.5=0.23: %s" % st.get("cycles_est"))
    adv = care.report()["advice"]
    ok(any("插电补电放电" in a for a in adv),
       "建议里点明插电补电放电代价: %s" % [a for a in adv if "补电" in a])


def main():
    test_topup_sign()
    test_other_states_signs()
    test_topup_ewma()
    test_pd_sees_topup()
    test_care_ac_drain()
    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
