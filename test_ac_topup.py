# -*- coding: utf-8 -*-
"""插电补电（top-up）链路回归测试 —— 2026-10-17/18 Type-C 事故的固化。

背景：Type-C 打游戏供不上时，ACPI 的充/放电标志会**同时翻假**，固件在该态
沿袭「负 = 放电方向」上报 Rate。旧代码只按标志翻符号 ⇒ rate_w 记成「充电」
⇒ PD 控制器全程认为供得上：不标定上限、不定档、不预判、不压制，电池一路
补电到 5%。本文件锁死三件事：

  1. battery.py：补电态的负 Rate 必须翻成「放电为正」，rate_avg_w 也要维护；
  2. battery.py：输出 topup / state_cn=「插电补电中」，面板三态有据可依；
  3. batterycare：AC 侧补电放电必须计入能量吞吐（ac_drain_wh）与等效循环。

2026-10-21 续：固件换了**新的撒谎签名**——CHARGING 标志置位 + Rate 正号
上报「充电 13.3W」，实际剩余容量 9 分钟掉 2.4Wh（PowerState=0x05 实测）。
「两标志皆假」的修没覆盖这种，PD 链路再次全程沉默。本文件第四节锁死
容量趋势纠偏：窗口内持续掉电才改判，量化 blip / 重锚定 / 短窗口都不触发。

不碰真实电池硬件：_ioctl / GetSystemPowerStatus 全部换成脚本化假实现。
"""

import contextlib
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
    # 大电流走快通道（3s）：两拍就够爬到 S3（S4 独显断电已废除）
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


# ------------------------------------------------------------ 固件撒谎·容量趋势（2026-10-21）
class FakeClock:
    """可控时钟：battery.py 里的 time.time() 全走这里，趋势窗口按它推进。"""

    def __init__(self, t0=10000.0):
        self.t = t0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


@contextlib.contextmanager
def clock_at(t0=10000.0):
    c = FakeClock(t0)
    old = bt.time
    bt.time = c
    try:
        yield c
    finally:
        bt.time = old


def _run(mon, n, clock, step=5.0):
    """连续采 n 拍（每拍 step 秒，模拟主循环 ~5s 节奏）。"""
    outs = []
    for _ in range(n):
        clock.t += step
        outs.append(mon.sample(force=True))
    return outs


def _lie_script(n_hold=18, n_step=18, cap0=50000, cap1=49420,
                state=CHARGING, rate_mw=13300):
    """撒谎脚本：前半平稳、后半掉一档（模拟电量计 ~1% 量化步进）。

    18+18 拍 × 5s = 180s；第 31 拍（t=155）时窗口跨度够 150s、净降 580mWh
    ⇒ 折算 13.9W，正好越过 CAP_TREND_MIN_W=2.0 触发改判。
    """
    return ([(state, rate_mw, cap0)] * n_hold +
            [(state, rate_mw, cap1)] * n_step)


def test_lie_charge_positive_rate():
    print("== 新签名①：CHARGING 置位 + Rate 正号谎报充电，趋势揪出来 ==")
    fb = FakeBattery(_lie_script())
    mon = BatteryMonitor()
    fb.install(mon)
    with clock_at() as c:
        outs = _run(mon, 36, c)
    # 前 30 拍窗口不够 / 净降不够，不许提前改判
    ok(outs[19]["trend_override"] == "" and outs[19]["state_cn"] == "充电中",
       "第 20 拍（跨度 95s<150s）不触发: %s" % outs[19]["state_cn"])
    ok(outs[29]["trend_override"] == "" and outs[29]["state_cn"] == "充电中",
       "第 30 拍（跨度 145s）仍不触发: %s" % outs[29]["state_cn"])
    s = outs[30]
    ok(s["trend_override"] == "lie_charge", "第 31 拍改判 lie_charge")
    ok(s["charging"] is False and s["discharging"] is False,
       "两标志撤假（输出形状复刻 10-18 topup）")
    ok(s["rate_w"] is not None and abs(s["rate_w"] - 13.92) < 0.01,
       "rate 翻成放电为正、取 max(|Rate|, 趋势)=13.92W: %s" % s["rate_w"])
    ok(s["topup"] is True, "topup 标记为真")
    ok(s["state_cn"] == "插电补电中", "state_cn=插电补电中: %s" % s["state_cn"])
    ok(s["rate_avg_w"] is not None and s["rate_avg_w"] > 0,
       "EWMA 在补电态持续维护: %s" % s["rate_avg_w"])
    # batterycare 的 ac_drain_wh 口径（batterycare.py:314）认的形状
    ok(s["ac"] is True and s["rate_w"] > 0.15,
       "care 计账形状（AC + rate>0）⇒ 计入 ac_drain_wh")


def test_lie_charge_negative_rate():
    print("== 新签名②：CHARGING 置位 + 固件 Rate 负号（镜像签名）==")
    fb = FakeBattery(_lie_script(rate_mw=-13300))
    mon = BatteryMonitor()
    fb.install(mon)
    with clock_at() as c:
        outs = _run(mon, 36, c)
    s = outs[30]
    ok(s["trend_override"] == "lie_charge" and s["topup"] is True,
       "负 Rate 同样被趋势揪出: %s" % s["trend_override"])
    ok(s["rate_w"] is not None and s["rate_w"] > 0,
       "rate 翻成放电为正: %s" % s["rate_w"])
    ok(s["state_cn"] == "插电补电中", "state_cn=插电补电中: %s" % s["state_cn"])


def test_healthy_charge_not_triggered():
    print("== 真充电：容量持续上升，绝不允许被误判成补电 ==")
    fb = FakeBattery(_lie_script(cap0=40000, cap1=41200, rate_mw=12000))
    mon = BatteryMonitor()
    fb.install(mon)
    with clock_at() as c:
        outs = _run(mon, 36, c)
    s = outs[35]
    ok(s["trend_override"] == "" and s["charging"] is True,
       "真充电全程不触发，charging 保持为真: %s" % s["trend_override"])
    ok(abs(s["rate_w"] + 12.0) < 0.01, "充电 rate 仍是负号: %s" % s["rate_w"])
    ok(s["state_cn"] == "充电中" and s["topup"] is False, "state_cn=充电中")


def test_quantization_blip_not_triggered():
    print("== 量化 blip：掉一格又翘回来，不许当成持续掉电 ==")
    # 50000 → 50600（翘过 c0+100）→ 49400：净降够，但中途回抬超容差
    script = ([(CHARGING, 5000, 50000)] * 18 +
              [(CHARGING, 5000, 50600)] * 6 +
              [(CHARGING, 5000, 49400)] * 12)
    fb = FakeBattery(script)
    mon = BatteryMonitor()
    fb.install(mon)
    with clock_at() as c:
        outs = _run(mon, 36, c)
    ok(outs[35]["trend_override"] == "" and outs[35]["state_cn"] == "充电中",
       "blip 被容差挡下，不触发: %s" % outs[35]["trend_override"])


def test_short_window_not_triggered():
    print("== 短窗口：掉电只持续 100s（<150s），不改判 ==")
    fb = FakeBattery(_lie_script(n_hold=10, n_step=10))
    mon = BatteryMonitor()
    fb.install(mon)
    with clock_at() as c:
        outs = _run(mon, 20, c)
    ok(outs[19]["trend_override"] == "" and outs[19]["state_cn"] == "充电中",
       "跨度不足不触发: %s" % outs[19]["state_cn"])


def test_lie_discharge_flag_stuck():
    print("== 反向签名：DISCHARGING 标志没翻 + 容量持续上升 ⇒ 按充电记账 ==")
    fb = FakeBattery(_lie_script(cap0=40000, cap1=41200,
                                 state=DISCHARGING, rate_mw=-21000))
    mon = BatteryMonitor()
    fb.install(mon)
    with clock_at() as c:
        outs = _run(mon, 36, c)
    s = outs[30]
    ok(s["trend_override"] == "lie_discharge", "改判 lie_discharge")
    ok(s["discharging"] is False and s["charging"] is True,
       "撤放电标志、按充电记账")
    ok(abs(s["rate_w"] + 21.0) < 0.01, "rate 翻成充电为负: %s" % s["rate_w"])
    ok(s["state_cn"] == "充电中" and s["topup"] is False, "state_cn=充电中")


def test_claim_change_resets_window():
    print("== 插电变沿：离电掉电样本不得算进「插电后还在掉」的证据 ==")
    # 第一段：离电打游戏，正常放电（DISCHARGING + 负 Rate 翻正）
    script1 = [(DISCHARGING, -21000, 50000)] * 10 + \
              [(DISCHARGING, -21000, 49420)] * 10
    fb = FakeBattery(script1, ac=False)
    mon = BatteryMonitor()
    fb.install(mon)
    with clock_at() as c:
        outs1 = _run(mon, 20, c)
        ok(outs1[19]["discharging"] is True and outs1[19]["state_cn"] == "放电中",
           "离电段是正常放电: %s" % outs1[19]["state_cn"])
        # 插电，固件立刻开始撒谎（CHARGING + 正 Rate），容量继续掉
        fb.ac = True
        script2 = [(CHARGING, 13300, 49420)] * 12 + \
                  [(CHARGING, 13300, 48840)] * 24
        fb.script = script2
        fb.i = 0
        outs2 = _run(mon, 36, c)
    ok(outs2[19]["trend_override"] == "" and outs2[19]["state_cn"] == "充电中",
       "插电后 100s 内不触发（历史已变沿作废）: %s" % outs2[19]["state_cn"])
    ok(outs2[30]["trend_override"] == "lie_charge" and outs2[30]["topup"] is True,
       "插电后 150s 持续掉电才改判: %s" % outs2[30]["trend_override"])


def test_recovery_clears_override():
    print("== 恢复：改判后容量转升，必须自动回到充电态 ==")
    script = ([(CHARGING, 13300, 50000)] * 18 +
              [(CHARGING, 13300, 49420)] * 18 +
              [(CHARGING, 12000, 49420)] * 18 +
              [(CHARGING, 12000, 50020)] * 18)
    fb = FakeBattery(script)
    mon = BatteryMonitor()
    fb.install(mon)
    with clock_at() as c:
        outs = _run(mon, 72, c)
    ok(outs[30]["trend_override"] == "lie_charge", "掉电段改判")
    s = outs[71]
    ok(s["trend_override"] == "" and s["charging"] is True,
       "容量转升后自动回充电态: %s" % s["trend_override"])
    ok(abs(s["rate_w"] + 12.0) < 0.01 and s["topup"] is False,
       "rate 回负号、topup 撤掉: %s / %s" % (s["rate_w"], s["topup"]))


def test_pd_chain_sees_lie():
    print("== PD 链路：改判后的放电率真的能标定供电上限（不再被 73.2W 假象蒙）==")
    fb = FakeBattery(_lie_script())
    mon = BatteryMonitor()
    fb.install(mon)
    with clock_at() as c:
        outs = _run(mon, 36, c)
    s = outs[30]
    rate = s["rate_avg_w"] or s["rate_w"]
    from lp.pdbudget import PdBudget
    pd = PdBudget({"pd_guard_enabled": True, "pd_margin_w": 1.5,
                   "pd_hold_s": 12.0, "pd_release_s": 45.0, "pd_cpu_step": 10,
                   "pd_cpu_min": 60, "pd_dim_level": 50, "pd_low_hz": True,
                   "pd_low_hz_value": 60})
    # 复刻现场：整机 32W（游戏菜单），电池补电 ~13.9W
    for _ in range(3):
        pd.feed(True, rate, 5.0, machine_w=32.0)
    ok(pd.supply_w is not None and pd.supply_firm,
       "补电率喂进去即标定出 firm 上限: %s" % pd.supply_w)
    ok(abs(pd.supply_w - (32.0 - rate)) < 0.1,
       "上限 = 整机 − 补电 ≈ 18W（而非假的 73.2W）: %s" % pd.supply_w)
    ok(pd.stage >= 1, "立即进入压制（不再全程沉默）: stage=%d" % pd.stage)


def main():
    test_topup_sign()
    test_other_states_signs()
    test_topup_ewma()
    test_pd_sees_topup()
    test_care_ac_drain()
    test_lie_charge_positive_rate()
    test_lie_charge_negative_rate()
    test_healthy_charge_not_triggered()
    test_quantization_blip_not_triggered()
    test_short_window_not_triggered()
    test_lie_discharge_flag_stuck()
    test_claim_change_resets_window()
    test_recovery_clears_override()
    test_pd_chain_sees_lie()
    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
