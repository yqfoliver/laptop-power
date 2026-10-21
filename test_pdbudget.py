# -*- coding: utf-8 -*-
"""弱电源（PD 100W）零补电预算控制器回归测试。

覆盖：判据（只认「插电却在放电」）、逐级加深、到底 stuck、
逐级回退、离电/关闭/数据缺失的还原、面板文案。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lp.pdbudget import PdBudget, MAX_STAGE  # noqa: E402

_PASS = 0
_FAIL = 0


def ok(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print("  FAIL: %s" % msg)


def cfg(**kw):
    base = {
        "pd_guard_enabled": True,
        "pd_margin_w": 1.5,
        "pd_hold_s": 25.0,
        "pd_release_s": 90.0,
        "pd_cpu_step": 10,
        "pd_cpu_min": 40,
        "pd_dim_level": 50,
        "pd_low_hz": True,
        "pd_low_hz_value": 60,
    }
    base.update(kw)
    return base


def run(pd, ac, rate, seconds, dt=5.0):
    """推进 seconds 秒，收集所有变更请求"""
    reqs = []
    n = int(round(seconds / dt))
    for _ in range(n):
        r = pd.feed(ac, rate, dt)
        if r:
            reqs.append(r)
    return reqs


# ---------------------------------------------------------------- 判据
def test_no_trigger_on_mains():
    print("== 200W 电源：电池在充电，永不触发 ==")
    pd = PdBudget(cfg())
    reqs = run(pd, True, -30.0, 300)
    ok(reqs == [], "充电中不产生任何动作")
    ok(pd.stage == 0 and not pd.active, "保持正常档")

    pd2 = PdBudget(cfg())
    ok(run(pd2, True, 0.0, 300) == [], "零放电（满充/养护停充）不触发")
    ok(pd2.stage == 0, "零放电保持正常")

    pd3 = PdBudget(cfg())
    ok(run(pd3, True, 1.0, 300) == [], "微弱波动 1.0W < margin 1.5W 不触发")

    pd4 = PdBudget(cfg())
    ok(run(pd4, False, 13.0, 300) == [], "离电放电不是本场景，不触发")
    ok(pd4.stage == 0, "离电保持正常")


def test_escalation():
    print("== 持续补电（小电流 3W）→ 逐级加深 ==")
    pd = PdBudget(cfg())
    # 未满 12s 前不动
    reqs = run(pd, True, 3.0, 10)
    ok(reqs == [], "hold 时间未到不动手（10s）")
    reqs = run(pd, True, 3.0, 20)           # 累计 30s > hold 25s
    ok(len(reqs) == 1 and reqs[0].get("refresh") == 60, "S1 降刷 60Hz")
    ok(pd.stage == 1 and pd.active, "stage=1")

    reqs = run(pd, True, 3.0, 30)
    ok(reqs and reqs[0].get("brightness") == 50, "S2 亮度封顶 50%")
    ok(pd.stage == 2, "stage=2")

    reqs = run(pd, True, 3.0, 30)
    ok(reqs and reqs[0].get("cpu_cap") == 90, "S3 CPU 上限 90%")
    ok(pd.stage == 3 and pd.cpu_cap == 90, "stage=3 cap=90")

    # S4（2026-10-18）：三级用尽还在放电 → 独显断电。前三层都在「重新分配/
    # 小步省」，只有换核显渲染能把整机压到充电器能力以内。
    reqs = run(pd, True, 3.0, 30)
    ok(reqs and reqs[0].get("gpu_off"), "S4 独显断电: %s" % reqs)
    ok(pd.stage == 4 and pd.gpu_cut, "stage=4 且 gpu_cut=True")
    ok("独显断电" in (pd.note or ""), "S4 文案: %s" % pd.note)


def test_fast_path():
    print("== 大电流放电（20W，切到重场景）→ 3 秒快通道 ==")
    pd = PdBudget(cfg())
    reqs = run(pd, True, 20.0, 5, dt=5.0)   # 一个采样周期就该动手
    ok(reqs and reqs[0].get("refresh") == 60, "20W 放电立刻限帧: %s" % reqs)
    # 小电流（3W）仍走常规 12s 确认，不被快通道误伤
    pd2 = PdBudget(cfg())
    ok(run(pd2, True, 3.0, 10) == [], "3W 小电流仍等满 hold")


def test_deepen_to_floor():
    print("== 最深层（S4 独显断电）继续压 CPU 到下限 ==")
    pd = PdBudget(cfg())
    caps = []
    for x in run(pd, True, 20.0, 100):       # 大电流走快通道，一次跑到底
        if x.get("cpu_cap"):
            caps.append(x["cpu_cap"])
    ok(pd.stage == MAX_STAGE, "已到最深层")
    ok(MAX_STAGE == 4 and pd.gpu_cut, "S4 独显断电已落地")
    for _ in range(6):
        for x in run(pd, True, 20.0, 25):
            if x.get("cpu_cap"):
                caps.append(x["cpu_cap"])
    ok(caps and caps[-1] == 40, "CPU 上限逐级下探到下限: %s" % caps)
    # 继续压到下限后 stuck
    for _ in range(3):
        run(pd, True, 20.0, 25)
    ok(pd.cpu_cap == 40, "CPU 下限 40%%: %d" % pd.cpu_cap)
    ok(pd.stuck, "手段用尽标记 stuck")
    ok("独显已断" in pd.note, "S4 到底的提示文案: %s" % pd.note)
    # stuck 后不再加码（避免毁体验）
    ok(run(pd, True, 20.0, 100) == [], "到底后不再产生新动作")


def test_s4_refused():
    print("== S4 落地条件不满足：如实回退，绝不假装已断电 ==")
    pd = PdBudget(cfg())
    reqs = run(pd, True, 20.0, 25)          # 快通道(3s)：四级各一拍
    ok(pd.stage == 4 and pd.gpu_cut, "已到 S4")
    ok(any(x.get("gpu_off") for x in reqs), "发出过 gpu_off 请求")
    pd.gpu_off_failed("无核显")
    ok(pd.stage == 3 and not pd.gpu_cut, "回退 S3，gpu_cut 清除")
    ok(pd.stuck and "无核显" in pd.note, "stuck 且注明原因: %s" % pd.note)
    # 被拒后不再每轮 hold 都升级重试，直接把 CPU 压到底
    reqs2 = [x for x in run(pd, True, 20.0, 100) if x.get("gpu_off")]
    ok(reqs2 == [], "被拒后不再发 gpu_off: %s" % reqs2)
    ok(pd.cpu_cap == 40 and pd.stuck, "CPU 压到下限并 stuck: cap=%d" % pd.cpu_cap)


def test_s4_hold_while_loaded():
    print("== S4 断显后负载仍贴着上限：保持断电，不来回通断 ==")
    pd = PdBudget(cfg(pd_supply_w=85.0, pd_release_s=45.0))
    for _ in range(5):
        pd.feed(True, 20.0, 5.0, machine_w=100.0)   # 快通道逐级到 S4
    ok(pd.stage == 4 and pd.gpu_cut, "S4 已落地: stage=%d" % pd.stage)
    # 独显断了 → 整机掉到 60W、电池不再放电，但 60 > min(85-25, 85×0.62)=52.7
    got = None
    for _ in range(30):                              # 150s 远超 release 45s
        r = pd.feed(True, -1.0, 5.0, machine_w=60.0)
        if r:
            got = r
    ok(got is None, "负载仍贴着上限：保持独显断电不还原: %s" % got)
    ok(pd.gpu_cut, "gpu_cut 仍在")
    # 负载真正退下去（<52.7W）才放行
    first = None
    for _ in range(20):
        r = pd.feed(True, -1.0, 5.0, machine_w=30.0)
        if r and first is None:
            first = r
    ok(first and first.get("gpu_on"), "退出重负载后还原独显: %s" % first)
    ok(not pd.gpu_cut and pd.stage == 3, "stage 回 3（先还独显）")


def test_release():
    print("== 供电恢复 → 逐级回退（与加深顺序严格相反：独显→CPU→亮度→刷新率）==")
    pd = PdBudget(cfg())
    run(pd, True, 20.0, 250)                 # 压到底（含 stuck 判定那一次）
    ok(pd.gpu_cut and pd.stuck, "先压到底（S4 独显断电 + stuck）")
    # 供电恢复（rate 变负 = 充电）
    r = run(pd, True, -5.0, 90)
    ok(r and r[0].get("gpu_on"), "回退第一步：独显恢复供电")
    ok(pd.stage == 3 and not pd.gpu_cut, "stage=3，独显已还")
    r = run(pd, True, -5.0, 90)
    ok(r and r[0].get("cpu_cap") == 50, "回退：CPU 50%")
    ok(pd.cpu_cap == 50, "cap=50")
    r = run(pd, True, -5.0, 90)
    ok(r and r[0].get("cpu_cap") == 60, "CPU 60%")
    for expect in (70, 80, 90, 100):
        r = run(pd, True, -5.0, 90)
        if expect < 100:
            ok(r and r[0].get("cpu_cap") == expect, "CPU %d%%" % expect)
    ok(pd.cpu_cap == 100, "CPU 完全放开")
    ok(pd.stage == 2, "stage 回 2（CPU 撤完）")
    r = run(pd, True, -5.0, 90)
    ok(r and r[0].get("brightness_off"), "放亮度")
    # 放完亮度只剩「降刷」一级，stage 必须是 1 —— 旧实现在这里还报 2，
    # 面板会多显示一级已被撤销的压制手段。
    ok(pd.stage == 1, "stage 回 1（只剩降刷）")
    r = run(pd, True, -5.0, 90)
    ok(r and r[0].get("refresh_off") or (r and r[0].get("reset")),
       "放刷新率: %s" % r)
    ok(pd.stage <= 1, "stage 回 %d" % pd.stage)
    ok(not pd.stuck, "stuck 清除")
    # 恢复时间未到不动
    pd2 = PdBudget(cfg())
    run(pd2, True, 20.0, 30)
    ok(run(pd2, True, -5.0, 60) == [], "release 时间未到不回退（60s < 90s）")


def test_reset_paths():
    print("== 离电 / 关闭 / 数据缺失 → 一次性还原 ==")
    pd = PdBudget(cfg())
    run(pd, True, 20.0, 60)
    ok(pd.active, "先进入压制")
    r = pd.feed(False, 13.0, 5.0)
    ok(r and r.get("reset"), "离电 → reset")
    ok(not pd.active and pd.cpu_cap == 100, "状态清空")

    pd2 = PdBudget(cfg())
    run(pd2, True, 20.0, 60)
    r = pd2.feed(True, None, 5.0)
    ok(r and r.get("reset"), "电池读数缺失 → reset")

    pd3 = PdBudget(cfg())
    run(pd3, True, 20.0, 60)
    r = pd3.feed(True, 20.0, 5.0)
    pd3.reload_cfg({"pd_guard_enabled": False})
    r = pd3.feed(True, 20.0, 5.0)
    ok(r and r.get("reset"), "功能关闭 → reset")
    ok(not pd3.enabled, "enabled=False")
    ok(pd3.feed(True, 20.0, 50.0) is None, "关闭后保持安静")


def test_report():
    print("== 报告与文案 ==")
    pd = PdBudget(cfg())
    rp = pd.report()
    ok(rp["enabled"] and not rp["active"] and rp["stage"] == 0, "初始报告")
    ok(rp["gpu_off"] is False and rp["gpu_cut"] is False, "初始未断独显")
    ok("待命" in pd.line(), "初始文案: %s" % pd.line())
    run(pd, True, 3.0, 30)
    rp = pd.report()
    ok(rp["active"] and rp["stage"] == 1, "压制中报告")
    ok("降刷" in pd.line(), "压制中文案: %s" % pd.line())
    run(pd, True, 20.0, 30)
    rp = pd.report()
    ok(rp["stage"] == 4 and rp["gpu_off"] and rp["gpu_cut"], "S4 报告: %s" % rp)
    ok("独显断电" in pd.line(), "S4 文案: %s" % pd.line())
    pd2 = PdBudget(cfg(pd_guard_enabled=False))
    ok("关" in pd2.line(), "关闭文案: %s" % pd2.line())


def test_cfg_hot():
    print("== 配置热更新 ==")
    pd = PdBudget(cfg(pd_cpu_min=60))
    run(pd, True, 20.0, 300)
    ok(pd.cpu_cap == 60, "下限随配置生效: %d" % pd.cpu_cap)
    pd2 = PdBudget(cfg(pd_margin_w=10.0))
    ok(run(pd2, True, 5.0, 300) == [], "margin 提高到 10W 后 5W 放电不触发")
    pd3 = PdBudget(cfg(pd_hold_s=5.0))
    ok(run(pd3, True, 20.0, 10), "hold 缩短为 5s 后更快响应")


# ---------------------------------------------------------------- 预测式供电预算
def test_supply_learning():
    print("== 用「整机功耗 + 电池 rate」反推电源上限 ==")
    # 充电 7.2W、整机 82W（2026-10-07 巫师3 Type-C 实测）⇒ 电源 ≥ 89.2W（下界）
    pd = PdBudget(cfg())
    pd.feed(True, -7.2, 5.0, machine_w=82.0)
    ok(pd.supply_w is not None and abs(pd.supply_w - 89.2) < 0.2,
       "充电时得到下界 89.2W: %s" % pd.supply_w)
    ok(not pd.supply_firm, "仅充电 => 只是下界，不算精确标定")

    # 放电 12W、整机 100W ⇒ 电源已满载，上限 = 88W（EWMA 向下收敛）
    pd.feed(True, 12.0, 5.0, machine_w=100.0)
    ok(88.0 <= pd.supply_w <= 89.2, "放电时向下收敛到 88~89.2W: %s" % pd.supply_w)
    ok(pd.supply_firm, "放电事件 => 精确上界")
    # 全新实例只看放电事件时，上限就是精确的 88W
    pd1 = PdBudget(cfg())
    pd1.feed(True, 12.0, 5.0, machine_w=100.0)
    ok(abs(pd1.supply_w - 88.0) < 0.01, "单次放电即精确标定 88W: %s" % pd1.supply_w)

    # 充电时的下界只能把估计抬高，绝不能拉低（电池充电曲线会限制充电功率）
    before = pd.supply_w
    pd.feed(True, -1.0, 5.0, machine_w=30.0)
    ok(pd.supply_w >= before - 0.01, "低负载充电不拉低上限: %s -> %s" % (before, pd.supply_w))

    # 坏值（离谱功耗）不参与学习
    pd2 = PdBudget(cfg())
    pd2.feed(True, 0.0, 5.0, machine_w=99999.0)
    ok(pd2.supply_w is None, "异常整机功耗被丢弃: %s" % pd2.supply_w)

    # 弱电源实到（2026-10-21）：整机 32W、电池补电 13.9W ⇒ 电源实到 18.1W
    # （Type-C 只协商到 ~20W 档）。旧下限 25W 把这种真值当异常丢掉：
    # supply 停在假的 73.2W、档位 unknown、面板「供电能力未测定」，
    # 而电池正在被放空。放电标定是精确上界，不该有 25W 门槛。
    pd3 = PdBudget(cfg())
    pd3.feed(True, 13.9, 5.0, machine_w=32.0)
    ok(pd3.supply_w is not None and abs(pd3.supply_w - 18.1) < 0.01,
       "18W 级实到电源不再被下限丢掉: %s" % pd3.supply_w)
    ok(pd3.supply_firm, "弱电源放电标定同样是 firm: %s" % pd3.supply_firm)


def test_supply_jump_guard():
    print("== 学习跳变保护（2026-10-08：坏值把 89W 棘轮顶到 146.9W）==")
    J = 15.0
    # 充电下界：一次尖峰想跳 58W -> 只允许抬到 89+15
    pd = PdBudget(cfg(pd_supply_w=89.0))
    pd.feed(True, -30.0, 5.0, machine_w=146.9)   # 虚高读数：lb=176.9
    ok(pd.supply_w <= 89.0 + J + 0.01,
       "充电下界尖峰被钳在 +%.0fW: %s" % (J, pd.supply_w))
    # 放电精确标定：与现有估计差太远 -> 小步跟随，不许一锤定音
    pd2 = PdBudget(cfg(pd_supply_w=89.0))
    pd2.feed(True, 5.0, 5.0, machine_w=200.0)    # est=195，虚高
    ok(89.0 < pd2.supply_w <= 89.0 + J + 0.01,
       "放电标定尖峰被钳在 +%.0fW: %s" % (J, pd2.supply_w))
    ok(pd2.supply_firm, "钳制不影响 firm 标记")
    # 真实升级不被卡死：连续采样会一步步走过去
    pd3 = PdBudget(cfg(pd_supply_w=89.0))
    for _ in range(20):
        pd3.feed(True, 5.0, 5.0, machine_w=200.0)
    ok(pd3.supply_w >= 150.0, "连续 20 次标定能爬到新上限: %s" % pd3.supply_w)


def test_supply_lb_streak_guard():
    print("== 充电下界累计爬升保护（2026-10-08 晚：89W 被小步顶到 128W）==")
    # 孤立尖峰：每次 +14W 的虚高下界只持续 1 拍，随后被正常读数打断
    pd = PdBudget(cfg(pd_supply_w=89.0))
    for _ in range(60):
        pd.feed(True, -5.0, 2.0, machine_w=98.0)   # lb=103，老代码立刻采纳
        pd.feed(True, -1.0, 2.0, machine_w=30.0)   # lb=31，正常读数清零连续
    ok(abs(pd.supply_w - 89.0) < 0.01,
       "60 轮孤立尖峰+正常读数，上限纹丝不动: %s" % pd.supply_w)

    # 连续 10 拍超限（真实电源能力变化）才抬升
    pd2 = PdBudget(cfg(pd_supply_w=89.0))
    for _ in range(9):
        pd2.feed(True, -5.0, 2.0, machine_w=98.0)
    ok(abs(pd2.supply_w - 89.0) < 0.01, "连续 9 拍尚不抬升: %s" % pd2.supply_w)
    pd2.feed(True, -5.0, 2.0, machine_w=98.0)      # 第 10 拍确认
    ok(abs(pd2.supply_w - 103.0) < 0.01,
       "连续 10 拍确认后抬到下界 103W: %s" % pd2.supply_w)

    # lb 远超估计的真升级：确认充分就小步爬，不被卡死，且速度有界
    pd3 = PdBudget(cfg(pd_supply_w=89.0))
    for _ in range(30):
        pd3.feed(True, -5.0, 2.0, machine_w=180.0)  # lb=185
    ok(pd3.supply_w > 89.0, "真升级能逐步爬升: %s" % pd3.supply_w)
    ok(pd3.supply_w <= 89.0 + 15.0 * 3 + 0.01,
       "爬升速度有界（每 10 拍至多 +15W）: %s" % pd3.supply_w)


def test_predictive_prearm():
    print("== 预判：整机逼近上限，电池还没放电就先限帧 ==")
    c = cfg(pd_supply_w=90.0, pd_pre_margin_w=8.0, pd_pre_hold_s=8.0)
    pd = PdBudget(c)
    ok(pd.supply_w == 90.0, "冷启动即载入上次学到的电源上限")
    # 整机 88W（远超 90-8=82 的预判线），电池仍在充电 ⇒ 老逻辑不会有任何动作
    r = pd.feed(True, -2.0, 5.0, machine_w=88.0)
    ok(r is None, "预判刚成立时不立刻动手（等短确认 8s）")
    ok(pd.predicted, "已标记为预判态")
    r = pd.feed(True, -2.0, 5.0, machine_w=88.0)      # 累计 10s ≥ 8s
    ok(r and r.get("refresh") == 60, "提前降刷限帧: %s" % r)
    ok(r.get("predicted"), "标记为预测触发")
    ok(pd.stage == 1 and pd.active, "进入 stage1（电池全程没放过电）")

    # 关掉预测后退回老行为：只在真放电时才动手
    pd2 = PdBudget(cfg(pd_supply_w=90.0, pd_predict_enabled=False))
    ok(run(pd2, True, -2.0, 30) == [], "预测关闭时 88W 不触发")
    ok(run(pd2, True, 20.0, 30), "预测关闭后仍认放电判据（hold=25s）")

    # 余量充足时不预判
    pd3 = PdBudget(cfg(pd_supply_w=200.0))
    ok(run(pd3, True, -30.0, 60) == [], "200W 电源下 82W 负载不预判")


def test_predict_only_before_active():
    print("== 已经在压制中时不再重复预判 ==")
    pd = PdBudget(cfg(pd_supply_w=90.0))
    run(pd, True, 20.0, 30)                  # 放电 -> stage1（测试 cfg 的 hold=25s）
    ok(pd.stage >= 1, "已进入压制")
    r = [x for x in run(pd, True, -1.0, 20) if x.get("predicted")]
    ok(r == [], "压制中不再产生预测触发")


def main():
    test_fast_path()
    test_supply_learning()
    test_supply_jump_guard()
    test_supply_lb_streak_guard()
    test_predictive_prearm()
    test_predict_only_before_active()
    test_no_trigger_on_mains()
    test_escalation()
    test_deepen_to_floor()
    test_s4_refused()
    test_s4_hold_while_loaded()
    test_release()
    test_reset_paths()
    test_report()
    test_cfg_hot()
    print("\n通过 %d 项，失败 %d 项" % (_PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
