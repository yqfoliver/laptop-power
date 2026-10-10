# -*- coding: utf-8 -*-
"""供电档位策略回归测试（65W / 100W 分别优化）。

两件必须锁死的事：
1. 65W 档必须判「喂不饱独显」⇒ 强制核显（不然电池一路放电）；
   100W 档允许独显，但让渡停止点要按供电预算收紧。
2. **充电下界不得用于预判**。下界只证明「电源至少这么大」，不是上限。
   65W 砖轻载 34W 时下界约 42W，若拿 42−8=34W 当预判线，日常办公就会被
   限帧降刷 —— 纯误伤。只有放电标定过（firm）的值才配做预判。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import pdtier                      # noqa: E402
from lp.pdbudget import PdBudget           # noqa: E402
from lp.alloc import PowerAllocator        # noqa: E402

_PASS = 0
_FAIL = 0


def ok(cond, msg, extra=None):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print("  FAIL: %s%s" % (msg, ("（实际 %r）" % (extra,)) if extra is not None else ""))


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
        "pd_overhead_w": 12.0,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 档位划分
def test_classify():
    print("== 档位划分：按可持续供电能力 ==")
    # 65W PD 实到 ~58W（砖端 94% 效率，再扣线损与机内 DC-DC）
    ok(pdtier.classify(58.0) == "weak", "58W（65W PD 实到）→ weak", pdtier.classify(58.0))
    ok(pdtier.classify(45.0) == "weak", "45W 小充电头 → weak")
    ok(pdtier.classify(89.0) == "high", "89W（100W PD 实到）→ high", pdtier.classify(89.0))
    ok(pdtier.classify(200.0) == "wall", "200W 原装适配器 → wall")
    ok(pdtier.classify(70.0) == "mid", "70W → mid")
    ok(pdtier.classify(None) == "unknown", "没测过 → unknown（不做任何假设）")
    ok(pdtier.classify(0) == "unknown", "0 → unknown")
    # 边界：65W 恰好落在 mid 起点（weak 是 <65）
    ok(pdtier.classify(64.9) == "weak", "64.9 仍属 weak")
    ok(pdtier.classify(65.0) == "mid", "65.0 进入 mid")


# ---------------------------------------------------------------- 65W 档
def test_weak_policy():
    print("== 65W 档：喂不饱独显，强制核显 ==")
    p = pdtier.policy(pdtier.classify(58.0), 58.0, overhead_w=12.0)
    ok(p["tier"] == "weak", "档位 weak")
    ok(p["force_igpu"] is True, "强制核显（要真的动手关独显，不是建议）")
    ok(p["allow_dgpu"] is False, "不允许独显")

    # 预算：58 − 4（安全余量）= 54W 整机；54 − 12（平台）− 20（CPU 保底）
    # = 22W 给 GPU —— 远低于独显能干活的下限 30W，所以判死
    ok(abs(p["machine_budget_w"] - 54.0) < 0.05,
       "整机预算 54W: %s" % p["machine_budget_w"])
    ok(abs(p["gpu_budget_w"] - 22.0) < 0.05,
       "GPU 预算 22W: %s" % p["gpu_budget_w"])
    ok(p["gpu_budget_w"] < pdtier.DGPU_MIN_USEFUL_W,
       "22W < 独显下限 %sW ⇒ 确实喂不饱" % pdtier.DGPU_MIN_USEFUL_W)
    ok(p["disable_guard"] is False, "弱电源更要开着保护")
    ok("核显" in pdtier.advice("weak", p), "文案要说明已切核显")


# ---------------------------------------------------------------- 100W 档
def test_high_policy():
    print("== 100W 档：独显可用，但按供电预算收紧 ==")
    p = pdtier.policy(pdtier.classify(89.0), 89.0, overhead_w=12.0)
    ok(p["tier"] == "high", "档位 high")
    ok(p["force_igpu"] is False, "不强制核显")
    ok(p["allow_dgpu"] is True, "允许独显")
    # 89 − 6 = 83W 整机；83 − 12 − 20 = 51W 给 GPU
    ok(abs(p["machine_budget_w"] - 83.0) < 0.05,
       "整机预算 83W: %s" % p["machine_budget_w"])
    ok(abs(p["gpu_budget_w"] - 51.0) < 0.05,
       "GPU 预算 51W: %s" % p["gpu_budget_w"])
    ok(p["gpu_budget_w"] >= pdtier.DGPU_MIN_USEFUL_W, "51W ≥ 独显下限，值得开")
    ok(p["disable_guard"] is False, "100W 仍要保护（实测整机可到 82W）")


def test_wall_policy():
    print("== 原装适配器：余量充足，控制器停用 ==")
    p = pdtier.policy(pdtier.classify(200.0), 200.0, overhead_w=12.0)
    ok(p["tier"] == "wall", "档位 wall")
    ok(p["disable_guard"] is True, "停用保护（没必要压）")
    ok(p["force_igpu"] is False, "不干预显卡选择")
    ok(p["gpu_budget_w"] is None, "不设 GPU 预算上限")


def test_unknown_policy():
    print("== 未测定：不擅自关独显 ==")
    p = pdtier.policy("unknown", None)
    ok(p["tier"] == "unknown", "档位 unknown")
    ok(p["force_igpu"] is False, "没测过就别自作主张关独显")
    ok(p["allow_dgpu"] is True, "未测定时按用户本意来")
    ok(p["machine_budget_w"] is None, "没有预算可言")


def test_budget_safety():
    print("== 预算不会算出负数 ==")
    # 极弱电源：供电 30W，扣完开销和 CPU 保底后是负的 → 夹到 0，不能出负数
    p = pdtier.policy("weak", 30.0, overhead_w=12.0, cpu_floor_w=20.0)
    ok(p["gpu_budget_w"] == 0.0, "负数夹到 0: %s" % p["gpu_budget_w"])
    ok(p["machine_budget_w"] >= 0.0, "整机预算不为负: %s" % p["machine_budget_w"])


# ------------------------------------------------- 核心：下界不得用于预判
def test_charging_lower_bound_no_predict():
    print("== 充电下界只是下界，不得触发预判（65W 日常办公误伤）==")
    # 65W 充电器：轻载 34W，电池还能充 3.5W ⇒ 下界 ~37W
    pd = PdBudget(cfg())
    for _ in range(20):
        pd.feed(True, -3.5, 5.0, machine_w=34.0)
    ok(pd.supply_w is not None and pd.supply_w < 45.0,
       "学到的只是下界 ~37W: %s" % pd.supply_w)
    ok(pd.supply_firm is False, "未经过放电标定 ⇒ firm=False")
    ok(pd.predicted is False, "不得进入预判态")
    ok(pd.stage == 0, "不得限帧降刷（日常办公不该被压）", pd.stage)

    # 对照：老逻辑会用 37−8=29W 当预判线，34W 早就触发了。这里必须不触发。
    ok(pd.machine_w >= (pd.supply_w - 8.0),
       "确认整机确已越过『老预判线』，但新逻辑不动手")


def test_firm_value_can_predict():
    print("== 放电标定过的精确值才配做预判 ==")
    # 1) 放电事件把它标定成 firm（放电是精确上界）
    pd = PdBudget(cfg())
    pd.feed(True, 12.0, 5.0, machine_w=89.0)     # 整机 89W、电池放 12W
    ok(pd.supply_firm is True, "放电标定后 firm=True")
    ok(abs(pd.supply_w - 77.0) < 0.05,
       "标定值 = 89 − 12 = 77W", pd.supply_w)
    ok(pd.tier() == "mid", "77W → mid 档", pd.tier())

    # 2) 拿 firm 值做预判。这里用 set_supply 直接注入（等价于「上次会话标定
    #    过、本次沿用」）而不再走放电 feed —— 放电会先触发压制进入 stage1，
    #    而「已在压制中就不再重复预判」是有意设计，会盖住要测的东西。
    pd2 = PdBudget(cfg(pd_pre_hold_s=8.0))
    pd2.set_supply(77.0, firm=True)
    r1 = pd2.feed(True, -1.0, 5.0, machine_w=75.0)
    ok(pd2.predicted is True, "第一拍即进入预判态", pd2.predicted)
    ok(r1 is None, "短确认期内不立刻动手", r1)
    r2 = pd2.feed(True, -1.0, 5.0, machine_w=75.0)     # 累计 10s ≥ 8s
    ok(r2 is not None and r2.get("predicted") is True,
       "确认期满后预判触发降刷", r2)
    ok(pd2.stage == 1, "进入 stage1（电池全程没放过电）", pd2.stage)


def test_no_predict_while_active():
    print("== 已在压制中不再重复预判（设计如此）==")
    pd = PdBudget(cfg(pd_pre_hold_s=8.0))
    pd.set_supply(77.0, firm=True)
    pd.feed(True, 12.0, 5.0, machine_w=89.0)     # 放电 → 进 stage1
    ok(pd.stage >= 1, "已进入压制", pd.stage)
    ok(pd.predicted is False, "压制中不重复预判", pd.predicted)


def test_firm_survives_adopt():
    print("== 冷启动沿用：firm 必须跟着走 ==")
    pd = PdBudget(cfg())
    pd.set_supply(58.0, firm=False)      # 上次只是充电下界
    ok(pd.supply_firm is False, "沿用下界 → firm=False")
    pd2 = PdBudget(cfg())
    pd2.set_supply(89.0, firm=True)      # 上次被放电精确标定过
    ok(pd2.supply_firm is True, "沿用精确值 → firm=True")


def test_tier_in_report():
    print("== 报告里带上档位与策略 ==")
    pd = PdBudget(cfg())
    pd.set_supply(58.0, firm=True)
    r = pd.report()
    ok(r["tier"] == "weak", "report.tier: %s" % r.get("tier"))
    ok(r["force_igpu"] is True, "report.force_igpu")
    ok(r["allow_dgpu"] is False, "report.allow_dgpu")
    ok(isinstance(r.get("gpu_budget_w"), float), "report.gpu_budget_w: %s"
       % r.get("gpu_budget_w"))
    ok(bool(r.get("tier_advice")), "report.tier_advice 有文案")


# ------------------------------------------------- 分配器按供电预算收紧
def test_alloc_supply_budget():
    print("== 分配器：让渡停止点跟着供电预算走 ==")
    a = PowerAllocator({"gpu_tgp_max_watts": 70, "gpu_sweet_watts": 80})
    ok(abs(a.gpu_eff_cap - 70.0) < 0.05, "无供电约束时按硬件上限 70W: %s"
       % a.gpu_eff_cap)
    a.set_supply_gpu_budget(51.0)        # 100W 档算出来的预算
    ok(abs(a.gpu_eff_cap - 51.0) < 0.05,
       "100W 档：停止点收紧到 51W: %s" % a.gpu_eff_cap)
    a.set_supply_gpu_budget(None)        # 换回原装适配器
    ok(abs(a.gpu_eff_cap - 70.0) < 0.05,
       "交回 None 后恢复硬件上限: %s" % a.gpu_eff_cap)
    # 0 / 负数视为「不约束」
    a.set_supply_gpu_budget(0)
    ok(abs(a.gpu_eff_cap - 70.0) < 0.05, "0 视为不约束: %s" % a.gpu_eff_cap)
    # 重复喂同值不重载（避免每轮巡检重算）
    a.set_supply_gpu_budget(51.0)
    ok(abs(a.gpu_eff_cap - 51.0) < 0.05, "喂入 51W 生效")
    a.set_supply_gpu_budget(51.2)        # 差 0.2 < 0.5 阈值，忽略
    ok(abs(a.gpu_eff_cap - 51.0) < 0.05, "微小变化忽略: %s" % a.gpu_eff_cap)


def test_tier_hysteresis():
    print("== 档位滞回：脱离 weak 要确认，掉回 weak 要立刻 ==")
    pd = PdBudget(cfg())
    pd.set_supply(58.0, firm=True)
    ok(pd.tier() == "weak", "65W PD（58W）→ weak", pd.tier())
    pd.set_supply(68.0, firm=True)      # 越过 65 阈值但仍在滞回带内
    ok(pd.tier() == "weak", "刚过阈值不立刻开独显（滞回 8W）", pd.tier())
    pd.set_supply(75.0, firm=True)      # 58+8=66 已过，75 明确够
    ok(pd.tier() == "mid", "明显超过才脱离 weak: %s" % pd.tier())
    # 反向必须立刻：那时电池正在放电，慢一拍都是事故
    pd.set_supply(50.0, firm=True)
    ok(pd.tier() == "weak", "掉回 weak 立即生效（不能等确认）", pd.tier())
    # 换充电器：滞回状态要清掉，重新判定
    pd2 = PdBudget(cfg())
    pd2.set_supply(58.0, firm=True)
    ok(pd2.tier() == "weak", "先记下 weak")
    pd2.reset_learning()
    pd2.set_supply(89.0, firm=True)     # 换成 100W
    ok(pd2.tier() == "high", "换充电器后不受滞回影响，直接判 high: %s"
       % pd2.tier())


def test_non_firm_never_weak():
    """充电下界不得定档（2026-10-10 二次事故）。

    65W 上轻载下界 49W、100W 上重充下界 63.7W，全被判成 weak 强制核显。
    下界只能证明「电源 ≥ 这么多」，永远证明不了「弱」——电池充电功率大
    就能把下界抬得很高。只有放电标定的 firm 值才配定档。
    """
    print("== 充电下界不定档：未标定一律 unknown（策略中性） ==")
    pd = PdBudget(cfg())
    # 100W 充电器重充场景：整机 37W + 充电 50W → 下界 87，但确认门内估计
    # 还爬在 63.7，且是下界不是 firm
    pd.set_supply(63.7, firm=False)
    ok(pd.tier() == "unknown", "下界 63.7 不是 weak，是未测定", pd.tier())
    pol = pd.tier_policy()
    ok(pol.get("force_igpu") is False, "未测定不强制核显", pol)
    ok(pol.get("machine_budget_w") is None,
       "未测定不做整机预算（下界算出的预算是假的）", pol)
    ok(pol.get("gpu_budget_w") is None, "未测定不做 GPU 预算", pol)
    # 即使下界很高（87，越过 weak+滞回）也不行 —— 它仍是下界
    pd.set_supply(87.0, firm=False)
    ok(pd.tier() == "unknown", "下界 87 也一样不定档", pd.tier())
    # 同一个数，放电标定过（firm）才说话算数
    pd.set_supply(87.0, firm=True)
    ok(pd.tier() == "high", "firm 87 → high，独显解锁", pd.tier())
    # 换源作废：reset_learning 连档位一起清
    pd.reset_learning()
    ok(pd.tier() == "unknown", "换源后档位回到未测定", pd.tier())
    ok(pd.tier_policy().get("force_igpu") is False,
       "换源后不再强制核显")


def main():
    test_classify()
    test_weak_policy()
    test_high_policy()
    test_wall_policy()
    test_unknown_policy()
    test_budget_safety()
    test_charging_lower_bound_no_predict()
    test_firm_value_can_predict()
    test_no_predict_while_active()
    test_firm_survives_adopt()
    test_tier_in_report()
    test_alloc_supply_budget()
    test_tier_hysteresis()
    test_non_firm_never_weak()


if __name__ == "__main__":
    main()
    print("\n%d 项断言，失败 %d" % (_PASS, _FAIL))
    if _FAIL:
        raise SystemExit(1)
