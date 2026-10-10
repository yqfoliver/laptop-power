# -*- coding: utf-8 -*-
"""供电档位：把「这个充电器能供多少 W」翻译成一整套策略。

为什么按档位分开做
------------------
100W 和 65W 是两种完全不同的机器，不能只靠「把上限数字改小」来适配：

    ┌──────────┬────────────┬───────────────────────────────────────────┐
    │ 充电器    │ 到系统可用  │ 结论                                      │
    ├──────────┼────────────┼───────────────────────────────────────────┤
    │ 65W PD   │ ~58W       │ 减掉平台开销 12W，只剩 46W 给 CPU+GPU。    │
    │          │            │ 独显最低也得 35W 起，一开就超 ⇒ **只能核显**│
    │ 100W PD  │ ~89W       │ 剩 77W，独显能跑但要按预算让瓦             │
    │ 原装砖    │ ≥150W      │ 余量充足 ⇒ 控制器整个不用开               │
    └──────────┴────────────┴───────────────────────────────────────────┘

「到系统」为什么比标称小：PD 砖端效率约 94%，再扣线缆压降与机内 DC-DC，
65W 标称实到 58~60W；非 e-mark 的 3A 线更是直接锁死在 60W。

所以弱电源下的正确解法**不是把功耗压到刚好不放电**（那点余量里独显根本
跑不动，等于又慢又烫），而是**换渲染路径**：关掉独显用核显，整机功耗直接
砍掉一大截，剩下的预算还能给电池充电。

设计原则
--------
1. 档位阈值是**瓦数**，不是机型 —— 换任何一台机器都成立。
2. 平台开销、CPU 保底这些机器相关的量全部走参数传入，不写死。
3. 宁可保守：档位边界不清时取更严的那一档。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------- 档位定义
# 按「可持续供电能力」划分。阈值取常见充电器的实到功率之间，避免临界抖动。
WEAK_MAX_W = 65.0      # ≤65W：65W PD（实到 ~58W）/ 45W / 手机充电头
MID_MAX_W = 85.0       # 65~85W：介于两者之间（90W 砖、部分 100W 砖实到偏低）
HIGH_MAX_W = 120.0     # 85~120W：100W PD（实到 ~89W）
# >120W：原装适配器，余量充足，PD 控制器整个不启用

TIER_CN = {
    "weak": "弱电源（65W 及以下）",
    "mid": "中等供电（65~85W）",
    "high": "强供电（85~120W）",
    "wall": "原装适配器（>120W）",
    "unknown": "供电能力未测定",
}

# CPU 保底功耗（W）：算「留给 GPU 的预算」时，CPU 这一份必须先留出来。
# 取轻载~中载的实测区间下沿，通用值；可由调用方覆盖。
CPU_FLOOR_W = 20.0

# 独显「能干活」的最低功耗（W）。低于这个数独显要么点不亮，要么比核显还慢
# （还要额外背一份显存与供电转换开销），不如直接用核显。
DGPU_MIN_USEFUL_W = 30.0


def classify(supply_w: Optional[float]) -> str:
    """把供电能力映射成档位名。None / 未测定 ⇒ unknown（不做任何假设）。"""
    if supply_w is None:
        return "unknown"
    try:
        w = float(supply_w)
    except Exception:
        return "unknown"
    if w <= 0:
        return "unknown"
    if w < WEAK_MAX_W:
        return "weak"
    if w < MID_MAX_W:
        return "mid"
    if w < HIGH_MAX_W:
        return "high"
    return "wall"


def policy(tier: str, supply_w: Optional[float] = None,
           overhead_w: float = 12.0,
           cpu_floor_w: float = CPU_FLOOR_W) -> Dict[str, Any]:
    """给出该档位的完整策略。

    overhead_w：平台固定开销（屏幕/主板/风扇），调用方从 config 传。
    cpu_floor_w：CPU 保底，算 GPU 预算时先扣掉。
    """
    t = tier if tier in TIER_CN else "unknown"

    # 整机可用预算 = 供电能力 − 安全余量。留余量是因为测量有误差，且电池
    # 在低电量/高温时充电需求会跳变，贴着上限跑必然间歇放电。
    reserve_w = {"weak": 4.0, "mid": 5.0, "high": 6.0,
                 "wall": 0.0, "unknown": 4.0}[t]
    # 预判提前量：越弱的电源越要早动手（它一旦放电就很难追回来）
    pre_margin_w = {"weak": 5.0, "mid": 6.0, "high": 8.0,
                    "wall": 8.0, "unknown": 5.0}[t]

    machine_budget_w = None
    gpu_budget_w = None
    if supply_w is not None and t not in ("wall",):
        try:
            mb = float(supply_w) - reserve_w
            machine_budget_w = mb if mb > 0 else 0.0
            gb = mb - float(overhead_w) - float(cpu_floor_w)
            gpu_budget_w = gb if gb > 0 else 0.0
        except Exception:
            machine_budget_w = gpu_budget_w = None

    # 独显是否值得开：weak 档预算必然喂不饱独显，直接判死
    if t == "weak":
        allow_dgpu = False
    elif t == "unknown":
        allow_dgpu = True          # 没测过就先按用户本意来，别自作主张
    elif gpu_budget_w is not None:
        allow_dgpu = gpu_budget_w >= DGPU_MIN_USEFUL_W
    else:
        allow_dgpu = True

    return {
        "tier": t,
        "tier_cn": TIER_CN[t],
        "supply_w": supply_w,
        # 是否强制核显：weak 档不光是"建议"，是真的要动手关独显
        "force_igpu": (t == "weak"),
        "allow_dgpu": allow_dgpu,
        "dgpu_useful": allow_dgpu,
        # 控制器是否整个停用（原装适配器没必要压）
        "disable_guard": (t == "wall"),
        "machine_budget_w": (round(machine_budget_w, 1)
                             if machine_budget_w is not None else None),
        "gpu_budget_w": (round(gpu_budget_w, 1)
                         if gpu_budget_w is not None else None),
        "reserve_w": reserve_w,
        "pre_margin_w": pre_margin_w,
        "overhead_w": overhead_w,
        "cpu_floor_w": cpu_floor_w,
    }


def advice(tier: str, pol: Optional[Dict[str, Any]] = None) -> str:
    """给用户看的一句话说明（面板用）。"""
    p = pol or policy(tier)
    t = p["tier"]
    if t == "weak":
        return ("供电不足以喂独显，已切核显渲染：整机功耗大幅下降，"
                "剩余预算还能给电池充电")
    if t == "mid":
        gb = p.get("gpu_budget_w")
        return ("独显可用但受限（约 %sW），重负载时会自动让 CPU 让出瓦数"
                % (("%.0f" % gb) if gb else "—"))
    if t == "high":
        gb = p.get("gpu_budget_w")
        return ("独显可用（约 %sW），逼近上限时提前限帧，电池不倒贴"
                % (("%.0f" % gb) if gb else "—"))
    if t == "wall":
        return "原装适配器供电充足，不限功耗"
    return "正在测定当前充电器的供电能力，测完自动套用对应策略"


def supply_bounds(supply_w: Optional[float]) -> Tuple[float, float]:
    """当前档位的供电能力区间，用于判断「是不是该换档了」。"""
    t = classify(supply_w)
    lo, hi = {"weak": (0.0, WEAK_MAX_W), "mid": (WEAK_MAX_W, MID_MAX_W),
              "high": (MID_MAX_W, HIGH_MAX_W), "wall": (HIGH_MAX_W, 999.0),
              "unknown": (0.0, 999.0)}[t]
    return lo, hi
