# -*- coding: utf-8 -*-
"""弱电源（USB-C PD）「零补电」预算控制器。

背景
----
本机原装适配器 200W。接 100W PD 时，整机需求（独显 55W + Dynamic Boost 20W
+ CPU 20~35W + 平台 15~25W）远超 PD 供给，缺口只能由电池补。但游戏负载下
电池是大电流放电 + 40~50℃ 高温，循环老化极快 —— 用户明确要求**不补电**。

判据（不依赖识别适配器功率，最鲁棒）
------------------------------------
插着电（AC）**却仍在放电**（rate_w > 0）⇒ 电源供不上，整机超额。
控制目标就是把电池 rate 压回 ≤ 0，让电池彻底退出供电。
这样既不用去标定 ChargerMode 之类的厂商 ID，也不会被"PD 到底多少瓦"误导。

手段排序（关键：要「降总量」，而不是「重新分配」）
------------------------------------------------
S1 降刷到最低（本机 165→60Hz）：省 2~3W；
   更重要的是**天然限帧** —— GPU 不再满帧渲染，这才是能省出 10W+ 的大头。
S2 屏幕亮度封顶：省 2~4W。
S3 压 CPU 上限（PROCTHROTTLEMAX）：**不降总量** —— CPU 让出的瓦会被独显的
   Dynamic Boost 吃掉。它的作用是把瓦让给独显保帧数，不是省电，所以排最后。
S4 手段用尽仍在放电：不再加码（继续压只会毁体验），明确提示用户降画质 /
   分辨率 / 帧率上限，或换回 200W 电源。

全部动作可逆、只写自建计划，不碰系统「平衡」计划。

2026-10-07 升级：预测式供电预算（不再等电池放电才动手）
------------------------------------------------------
「插电却在放电」这个判据虽然鲁棒，但它是**事后**信号 —— 电池已经在大电流
放电了才去限帧，白放掉 0.1Wh 高温电。其实用两个量就能把电源上限反推出来：

    整机功耗 machine_w = SoC 功耗 + 独显功耗 + 平台开销
    放电时（rate > 0）：电源已满载 ⇒ 上限 = machine_w − rate   （精确上界）
    充电时（rate < 0）：上限 ≥ machine_w + |rate|            （下界）

本机实测（巫师3，Type-C）：SoC 28W + 独显 41.5W + 12W 开销 ≈ 82W，
电池仍在充电 7.2W ⇒ 电源上限 ≈ 89W。学到这个数之后，一旦整机功耗逼近
89W 就**提前**限帧，把补电消灭在发生之前 —— 电池全程不出力。
"""

from typing import Optional, Dict, Any

STAGE_CN = {
    0: "正常",
    1: "降刷限帧",
    2: "+ 亮度封顶",
    3: "+ CPU 让瓦",
}

MAX_STAGE = 3

# 电源上限估计的可信区间：太低说明读数异常，太高说明是坏值
SUPPLY_MIN_W = 25.0
SUPPLY_MAX_W = 330.0

# 单次学习允许的最大跳变（瓦）。
# 2026-10-08 实测踩坑：充电分支是「只许抬高、绝不拉低」的棘轮，一次整机读数
# 尖峰（NVML 有间歇吐坏值的前科）就把估计从实测的 89W 顶到 146.9W，而且因为
# 棘轮只升不降，之后再也回不来 —— 面板长期显示 147W 的错误上限。
# 加了跳变上限后，孤立坏值至多推动 JUMP 瓦，连续正常的采样会通过 EWMA
# 把它拉回真值；真正的电源升级也不会被卡死（多走几步而已）。
SUPPLY_JUMP_W = 15.0

# 充电下界抬升需要「连续确认」的拍数。
# 2026-10-08 晚二次踩坑：只限单步 15W 挡不住**累计爬升** —— 充电分支是只升
# 不降的棘轮，虚高读数（PDH/功耗计尖峰）每次 +10~15W 都被立刻采纳，一天下来
# 89W 被小步爬到 128W。孤立尖峰只会持续一两拍，中间必然夹着正常读数把连续
# 计数清零；真实电源能力变化会连续多拍超限，晚几拍才学到，无碍。
SUPPLY_LB_STREAK = 10

# 「放电」是精确上界，所以下修必须**快**——这是换充电器的兜底通道。
# 2026-10-10 第三次踩坑：STREAK=10 只管住了充电方向的爬升，放电方向的收敛
# 却还套着 ±15W 的单步夹 + 0.3 权重。真换了个弱电源时，130W 的旧值要十几拍
# （一分钟以上）才爬下来，这段时间电池一直在放电——用户看到的就是
# 「Type-C 打游戏全程从电池取电」。放电是精确上界，只要连续两拍都说供不上，
# 就该相信它：连续确认挡的是孤立坏值，不是真换电源。
SUPPLY_DN_STREAK = 2        # 放电方向连续确认拍数（远小于充电方向的 10）
SUPPLY_DN_JUMP_W = 40.0     # 确认后的单拍最大下修幅度
SUPPLY_DN_EPS_W = 1.5       # 小于这个差距不算「要下修」
SUPPLY_DN_EWMA = 0.6        # 确认后的收敛权重（越大越快）

# 供电档位中文名（面板显示）。弱/中/强三档的策略差异见 pdtier 模块。
try:
    from .pdtier import TIER_CN as TIER_CN_LABEL
except Exception:            # pragma: no cover —— 同包模块，理论上不会失败
    TIER_CN_LABEL = {}

# 脱离 weak 档所需的额外余量（W）。65W PD 实到 ~58W，离阈值只有 7W，
# 没有这道缓冲，估计值一抖就会让独显反复通断。
TIER_UP_HYST_W = 8.0


class PdBudget:
    """纯决策器：只算「该做什么」，动作由 manager 执行（便于注入测试）。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or {}
        self.enabled = True
        self.stage = 0               # 当前压制深度 0~3
        self.cpu_cap = 100           # CPU 上限天花板（独立于档位基准）
        self.rate_w: Optional[float] = None
        self.machine_w: Optional[float] = None   # 整机功耗（SoC+独显+平台开销）
        self.supply_w: Optional[float] = None    # 学到的电源可持续上限
        self.supply_firm = False                 # 是否已被「放电事件」精确标定
        self.predicted = False                   # 预判：整机已逼近电源上限
        self.predict_armed = False               # 本次限帧是预判触发的（防抖用）
        self.pre_s = 0.0                         # 预判持续计时
        self.over_s = 0.0            # 持续超额计时
        self.ok_s = 0.0              # 持续达标计时
        self.note = ""
        self.stuck = False           # 手段用尽仍在放电
        self._lb_streak = 0          # 充电下界连续超限计数（抬升确认用）
        self._dn_streak = 0          # 放电上界连续偏低计数（下修确认用）
        self._tier: Optional[str] = None   # 上一轮档位（滞回用）
        self._src = "未知"           # 上限的来源标签（面板显示）
        self.reload_cfg(cfg)

    # ------------------------------------------------------------ 供电上限学习
    def _learn_supply(self, machine_w: float, rate_w: float) -> None:
        """用「整机功耗 + 电池充放电」反推电源上限（只读推算，不做任何动作）。

        放电：电源已饱和 ⇒ machine − discharge 就是上限（精确，可信度最高）
        充电：电源还有余量 ⇒ machine + charge 只是下界（充电功率会被电池
             自身的充电曲线限制，所以不能当成上限，只能用来抬高估计）
        """
        if machine_w is None or rate_w is None:
            return
        if not (SUPPLY_MIN_W <= machine_w <= SUPPLY_MAX_W):
            return
        if rate_w > 0.15:
            est = machine_w - rate_w
            if est < SUPPLY_MIN_W:
                return
            if self.supply_w is None:
                self.supply_w = est
                self.supply_firm = True
                self._src = "本次实测（放电）"
            else:
                # 放电是**精确上界**：连续两拍都说「供不上」，就是换了个更弱的
                # 电源（或者负载真的超了），必须快速认账 —— 慢一拍电池就多放
                # 一拍的电。孤立坏值不会连续两拍同向，所以这里不需要 10 拍。
                gap = self.supply_w - est
                if gap > SUPPLY_DN_EPS_W:
                    self._dn_streak += 1
                else:
                    self._dn_streak = 0
                if self._dn_streak >= SUPPLY_DN_STREAK:
                    floor = self.supply_w - SUPPLY_DN_JUMP_W
                    target = est if est > floor else floor
                    a = SUPPLY_DN_EWMA
                    self.supply_w = (1 - a) * self.supply_w + a * target
                    self._src = "本次实测（放电）"
                else:
                    # 单拍：仍按小步跟随，别被一个尖峰拽下去
                    if est > self.supply_w + SUPPLY_JUMP_W:
                        est = self.supply_w + SUPPLY_JUMP_W
                    elif est < self.supply_w - SUPPLY_JUMP_W:
                        est = self.supply_w - SUPPLY_JUMP_W
                    self.supply_w = 0.7 * self.supply_w + 0.3 * est
                    self._src = "本次实测（放电，确认中）"
                self.supply_firm = True
            self._lb_streak = 0     # 放电精确标定优先，作废未确认的抬升
        else:
            # 回到充电（或零放电）：供得上了，下修计数必须清零 —— 否则一次
            # 孤立的假放电会一直挂着，等下一拍再出现真放电就直接"确认"掉。
            self._dn_streak = 0
            # 下界：只许把估计抬高，绝不拉低（充电受限是常态，不代表电源小）
            lb = machine_w + (-rate_w if rate_w < 0 else 0.0)
            if self.supply_w is None:
                if lb <= SUPPLY_MAX_W:
                    self.supply_w = lb
                    self._src = "本次实测（充电下界）"
            elif lb > self.supply_w:
                # 抬升必须「连续多拍」确认：虚高读数只持续一两拍，中间被
                # 正常读数打断就把计数清零 —— 堵住棘轮被小步累计顶穿的路
                # （单限幅度挡不住一天几十次的 +10W 小步，89W 曾爬到 128W）。
                self._lb_streak += 1
                if self._lb_streak >= SUPPLY_LB_STREAK:
                    self._lb_streak = 0
                    if lb <= self.supply_w + SUPPLY_JUMP_W \
                            and lb <= SUPPLY_MAX_W:
                        self.supply_w = lb
                        self._src = "本次实测（充电下界）"
                    elif lb <= SUPPLY_MAX_W:
                        # 真电源升级（lb 远超估计）：确认充分就小步走，
                        # 几拍之内爬到新上限，不会被卡死
                        self.supply_w += SUPPLY_JUMP_W
                        self._src = "本次实测（充电下界）"
            else:
                self._lb_streak = 0
        if self.supply_w is not None:
            self.supply_w = max(SUPPLY_MIN_W, min(SUPPLY_MAX_W, self.supply_w))

    # ------------------------------------------------------------ 供电档位
    def tier(self) -> str:
        """当前供电能力落在哪一档（weak / mid / high / wall / unknown）。

        带**不对称滞回**：脱离 weak 要额外确认，掉回 weak 立刻生效。
        理由和供电上限学习的棘轮一样 —— 两个方向的代价完全不同：
          · 往上抬错了 = 把独显打开 = 电池开始放电（事故）
          · 往下掉慢了 = 电池正在放电还多放一会儿（事故）
        所以往上要迟钝、往下要敏感。65W PD 实到 ~58W，离 65W 阈值只有 7W
        余量，没有滞回的话估计值一抖就会让独显反复通断。

        关键（2026-10-10 二修）：**只有放电标定的 firm 值才配定档**。
        充电下界只能证明「电源 ≥ 这么多」，证明不了「弱」——65W 上轻载
        下界 49W、100W 上重充下界 63.7W，全都被判成 weak 强制核显。
        下界要抬过 weak 线轻而易举（电池充电功率大就行），所以拿它定档
        必然把 100W 也按住。未标定 = unknown = 策略中性，等放电事件
        （快速通道 3 秒）把值标实了再套对应档位。
        """
        try:
            from . import pdtier
        except Exception:
            return "unknown"
        if not self.supply_firm:
            self._tier = "unknown"
            return "unknown"
        raw = pdtier.classify(self.supply_w)
        if self._tier == "weak" and raw not in ("weak", "unknown"):
            w = self.supply_w
            if w is None or w < pdtier.WEAK_MAX_W + TIER_UP_HYST_W:
                return "weak"      # 还不够格开独显，先按弱电源管着
        self._tier = raw
        return raw

    def tier_policy(self) -> dict:
        """该档位的策略（整机预算 / GPU 预算 / 是否强制核显 …）。"""
        try:
            from . import pdtier
            return pdtier.policy(
                self.tier(), self.supply_w,
                overhead_w=self._k("pd_overhead_w", 12.0))
        except Exception:
            return {"tier": "unknown", "force_igpu": False,
                    "allow_dgpu": True, "disable_guard": False,
                    "pre_margin_w": self._k("pd_pre_margin_w", 8.0),
                    "machine_budget_w": None, "gpu_budget_w": None}

    def tier_advice(self) -> str:
        """给用户看的一句话策略说明。"""
        try:
            from . import pdtier
            pol = self.tier_policy()
            return pdtier.advice(pol.get("tier", "unknown"), pol)
        except Exception:
            return ""

    def _over_expected(self) -> bool:
        """预判：整机功耗是否已逼近学到的电源上限"""
        if not self._k("pd_predict_enabled", True):
            return False
        if self.supply_w is None or self.machine_w is None:
            return False
        # 关键（2026-10-10 修复）：充电状态下推出来的只是**下界**
        # （「电源至少供得出这么多」），不是上限。电池充电功率受自身充电曲线
        # 限制，轻载时永远在充电 ⇒ 下界永远停在很低的位置。拿它当上限做预判，
        # 会在 65W 充电器上把 34W 的日常办公判成「逼近上限」→ 限帧、降刷新率，
        # 纯误伤。只有放电事件标定的值（supply_firm）才是真上限，才配做预判。
        if not self.supply_firm:
            return False
        pol = self.tier_policy()
        # 原装适配器（≥120W）：余量本来就够，不预判。它的整机峰值本就可能
        # 摸到预判线，判「快供不上了」纯属误报。
        if pol.get("disable_guard"):
            return False
        margin = pol.get("pre_margin_w") or self._k("pd_pre_margin_w", 8.0)
        return self.machine_w >= (self.supply_w - margin)

    # ------------------------------------------------------------ 配置
    def reload_cfg(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        if cfg is not None:
            self.cfg = cfg
        self.enabled = bool(self.cfg.get("pd_guard_enabled", True))
        # 上次运行学到的电源上限（config 里持久化），冷启动即可预判。
        # 现在正常路径下这里是 None（改由 pd_supply.json 按供电会话存档），
        # 只有用户手填才会命中 —— 手填视为可信的精确值，故 firm=True。
        if self.supply_w is None:
            try:
                v = self.cfg.get("pd_supply_w")
                if v is not None:
                    v = float(v)
                    if SUPPLY_MIN_W <= v <= SUPPLY_MAX_W:
                        self.supply_w = v
                        self.supply_firm = True
            except Exception:
                self.supply_w = None

    # ------------------------------------------------------------ 换充电器
    def reset_learning(self) -> None:
        """换了充电器：作废旧上限，从头现测。

        这是本轮修复的核心。旧实现持久的那个上限是**全局**的，在 200W 适配器
        上学到 130W，换到 100W 的 Type-C 上还当 130W 用 —— 控制器以为余量
        充足，全程不压制，电池一路放电。供电能力是充电器的属性，不是机器的
        属性，换源必须重新学。
        """
        self.supply_w = None
        self.supply_firm = False
        self.predicted = False
        self.predict_armed = False
        self._tier = "unknown"   # 换源后档位作废：新充电器是谁还没标定过
        self.pre_s = 0.0
        self.over_s = 0.0
        self.ok_s = 0.0
        self._lb_streak = 0
        self._dn_streak = 0
        self._tier = None           # 换了充电器，档位重新判定（不受滞回影响）
        self._src = "本次供电会话：学习中"
        # 注意：不清 stage —— 压制动作由 manager 自己在 AC 断开时还原

    def set_supply(self, watts: Optional[float], firm: bool = False) -> None:
        """外部注入上限（冷启动沿用上次会话的值）。"""
        try:
            v = float(watts) if watts is not None else None
        except Exception:
            v = None
        if v is None or not (SUPPLY_MIN_W <= v <= SUPPLY_MAX_W):
            return
        self.supply_w = v
        self.supply_firm = bool(firm)
        self._src = "沿用上次会话（复核中）"

    def _k(self, name: str, default):
        try:
            v = self.cfg.get(name, default)
            return default if v is None else type(default)(v)
        except Exception:
            return default

    @property
    def active(self) -> bool:
        return self.stage > 0

    # ------------------------------------------------------------ 决策
    def feed(self, ac: bool, rate_w: Optional[float], dt: float,
             machine_w: Optional[float] = None) -> Optional[dict]:
        """推进一次状态机。返回「变更请求」dict，或 None（无动作）。

        machine_w：整机功耗（SoC + 独显 + 平台开销）。给了才能学电源上限、
                   才能预判「快供不上了」；不给就退回纯放电判据（老行为）。

        dict 可能的键：
            refresh      int   —— 目标刷新率（由 manager 夹到面板可用值）
            refresh_off  True  —— 还原刷新率
            brightness   int   —— 亮度封顶值
            brightness_off True—— 还原档位亮度
            cpu_cap      int   —— CPU 上限（manager 再与档位基准取 min）
            reset        True  —— 全部还原（离电 / 关闭 / 数据缺失）
        """
        if not self.enabled:
            return self._reset() if self.active else None
        # 离电时电池本来就在放电，不是「弱电源补电」场景
        if not ac or rate_w is None:
            if self.active:
                return self._reset()
            self.over_s = 0.0
            self.ok_s = 0.0
            self.pre_s = 0.0
            self.predicted = False
            return None

        self.rate_w = float(rate_w)
        if machine_w is not None:
            try:
                self.machine_w = float(machine_w)
            except Exception:
                self.machine_w = None
        self._learn_supply(self.machine_w, self.rate_w)
        margin = self._k("pd_margin_w", 1.5)
        hold = self._k("pd_hold_s", 12.0)
        release = self._k("pd_release_s", 45.0)
        step = self._k("pd_cpu_step", 10)
        cmin = self._k("pd_cpu_min", 60)
        # 大电流放电（切到重场景会瞬间 +20W）走快通道：3 秒就动手，
        # 不用等满 12 秒 —— 这 12 秒里电池正以 15W+ 高温放电。
        if self.rate_w > self._k("pd_fast_w", 10.0):
            hold = min(hold, self._k("pd_fast_hold_s", 3.0))

        # ---- 预判：整机已逼近电源上限，但电池还没开始放电 → 提前限帧
        near = self._over_expected()
        self.predicted = near and not self.active
        if near and not self.active:
            self.pre_s += max(0.0, float(dt or 0.0))
            if self.pre_s >= self._k("pd_pre_hold_s", 8.0):
                self.pre_s = 0.0
                req = self._escalate()
                req["predicted"] = True
                self.predict_armed = True
                return req
        else:
            self.pre_s = 0.0

        if self.rate_w > margin:
            # 超额：电池在替电源出力
            self.ok_s = 0.0
            self.over_s += max(0.0, float(dt or 0.0))
            if self.over_s < hold:
                return None
            self.over_s = 0.0
            if self.stage < MAX_STAGE:
                return self._escalate()
            # 已在最深层：继续压 CPU 直到下限
            if self.cpu_cap > cmin:
                self.cpu_cap = max(cmin, self.cpu_cap - step)
                self.note = "PD 供电不足：CPU 上限降到 %d%%（让瓦给独显）" % self.cpu_cap
                return {"cpu_cap": self.cpu_cap, "stage": self.stage}
            self.stuck = True
            self.note = ("仍在放电 %.1fW：已达自动压制下限，"
                         "请降低游戏画质/分辨率或改用 200W 电源" % self.rate_w)
            return None

        # 达标（充电或零放电）
        self.over_s = 0.0
        self.stuck = False
        # 只在「正在压制」时累计达标时间。未压制时若也累计，ok_s 会无限增长
        # （闲置十分钟就攒到 600 秒），一旦预判限帧生效，下一拍就直接满足
        # release 条件被瞬间还原 —— 表现就是 60Hz 只撑 5 秒又跳回 165Hz。
        if not self.active:
            self.ok_s = 0.0
            return None
        self.ok_s += max(0.0, float(dt or 0.0))
        if self.ok_s < release:
            return None
        # 防抖：预判限帧后功耗必然下降，若一降就放，回 165Hz 又会立刻顶到
        # 上限 ⇒ 刷新率在 60/165 之间反复跳（游戏中会黑屏闪一下）。
        # 所以「预判限的帧」只有在负载真正退下去（离上限还有 restore_margin）
        # 时才还原 —— 游戏过程全程保持 60Hz，退出游戏/回到轻负载才放。
        if self.predict_armed and self.stage == 1 and self._still_loaded():
            self.ok_s = 0.0
            return None
        self.ok_s = 0.0
        return self._deescalate()

    def _still_loaded(self) -> bool:
        """负载是否仍贴着电源上限（是则继续保持限帧）。

        两个判据取更严格的那个：
          绝对余量  supply - pd_restore_margin_w
          比例      supply * pd_restore_ratio
        为什么还要比例判据：只用绝对余量会振荡 —— 上限 89W、余量 25W 时阈值
        是 64W，而降刷限帧本身就能省下 20~28W，整机从 82W 掉到 60W 就"达标"
        了，于是还原 165Hz → 又冲回 82W → 8 秒后再限帧，来回抖（每次切刷新率
        屏幕都会黑一下）。加比例判据后阈值降到 55W，限帧省下的量不足以触发
        还原，只有真正退出游戏（整机掉到 20~30W）才放行。
        """
        if self.supply_w is None or self.machine_w is None:
            return False
        rm = self._k("pd_restore_margin_w", 25.0)
        ratio = self._k("pd_restore_ratio", 0.62)
        thr = min(self.supply_w - rm, self.supply_w * ratio)
        return self.machine_w > thr

    # ------------------------------------------------------------ 内部
    def _escalate(self) -> dict:
        self.stage = min(MAX_STAGE, self.stage + 1)
        self.ok_s = 0.0          # 刚加深压制，达标计时从头开始
        # 一旦真的动手，预判态就结束了 —— 否则面板会一直挂着「预判中」，
        # 而实际上已经在压制。触发来源改由 predict_armed 记录。
        self.predicted = False
        req: Dict[str, Any] = {"stage": self.stage}
        if self.stage == 1:
            if self._k("pd_low_hz", True):
                # 0/None = 这台机器没有更低的刷新率档（60Hz 屏），别白折腾
                hz = int(self._k("pd_low_hz_value", 0) or 0)
                if hz > 0:
                    req["refresh"] = hz
            if self._over_expected():
                self.note = ("整机 %.0fW 已逼近电源上限 %.0fW：提前限帧，"
                             "电池不必补电" % (self.machine_w or 0.0,
                                               self.supply_w or 0.0))
            else:
                self.note = "PD 供电不足：降刷限帧（省电 + 压低 GPU 负载）"
        elif self.stage == 2:
            req["brightness"] = int(self._k("pd_dim_level", 50))
            self.note = "PD 供电不足：屏幕亮度封顶 %d%%" % int(self._k("pd_dim_level", 50))
        else:
            self.cpu_cap = max(self._k("pd_cpu_min", 60),
                               self.cpu_cap - self._k("pd_cpu_step", 10))
            req["cpu_cap"] = self.cpu_cap
            self.note = "PD 供电不足：CPU 上限降到 %d%%（让瓦给独显保帧）" % self.cpu_cap
        return req

    def _deescalate(self) -> dict:
        """逐级回退：先放 CPU，再放亮度，最后放刷新率（与加深顺序严格相反）。"""
        step = self._k("pd_cpu_step", 10)
        if self.cpu_cap < 100:
            self.cpu_cap = min(100, self.cpu_cap + step)
            # CPU 让瓦这一级已撤回，stage 必须跟着降回 2，否则面板会继续显示
            # 「+ CPU 让瓦」—— 显示比实际压制深度慢一步。
            if self.cpu_cap >= 100 and self.stage >= 3:
                self.stage = 2
            self.note = "供电恢复：CPU 上限放回 %d%%" % self.cpu_cap
            return {"cpu_cap": self.cpu_cap, "stage": self.stage}
        if self.stage == 2:
            self.stage = 1
            self.note = "供电恢复：亮度限制已解除"
            return {"brightness_off": True, "stage": self.stage}
        # stage 1 -> 0：刷新率还原（reset 让 manager 一次性收尾）
        self.stage = 0
        self.note = ""
        req = {"refresh_off": True, "reset": True, "stage": 0}
        self._clear()
        return req

    def _reset(self) -> dict:
        """一次性全部还原（离电 / 功能关闭 / 数据缺失）。"""
        req = {"reset": True, "stage": 0}
        self._clear()
        return req

    def _clear(self) -> None:
        self.stage = 0
        self.cpu_cap = 100
        self.over_s = 0.0
        self.ok_s = 0.0
        self.pre_s = 0.0
        self.predicted = False
        self.predict_armed = False
        self.stuck = False
        self.note = ""

    # ------------------------------------------------------------ 报告
    def report(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "stage": self.stage,
            "stage_cn": STAGE_CN.get(self.stage, "?"),
            "cpu_cap": self.cpu_cap,
            "rate_w": (round(self.rate_w, 2) if self.rate_w is not None else None),
            "machine_w": (round(self.machine_w, 1) if self.machine_w is not None else None),
            "supply_w": (round(self.supply_w, 1) if self.supply_w is not None else None),
            "supply_firm": self.supply_firm,
            "supply_src": (self._src if self.supply_w is not None else "本次供电会话：学习中"),
            "tier": self.tier(),
            "tier_cn": TIER_CN_LABEL.get(self.tier(), self.tier()),
            "gpu_budget_w": self.tier_policy().get("gpu_budget_w"),
            "machine_budget_w": self.tier_policy().get("machine_budget_w"),
            "force_igpu": bool(self.tier_policy().get("force_igpu")),
            "allow_dgpu": bool(self.tier_policy().get("allow_dgpu")),
            "tier_advice": self.tier_advice(),
            "predicted": self.predicted,
            "predict_armed": self.predict_armed,
            "stuck": self.stuck,
            "note": self.note,
        }

    def line(self) -> str:
        """一行状态（面板用）"""
        if not self.enabled:
            return "PD 保护：关"
        if not self.active:
            if self.supply_w:
                tail = " · 电源上限 %.0fW" % self.supply_w
                if self.machine_w:
                    tail += " · 整机 %.0fW" % self.machine_w
                return "PD 保护：待命" + tail
            return "PD 保护：待命（电池未放电）"
        s = STAGE_CN.get(self.stage, "?")
        if self.machine_w:
            s += " · 整机 %.0fW" % self.machine_w
        if self.supply_w:
            s += " / 上限 %.0fW" % self.supply_w
        if self.rate_w is not None:
            s += " · 放电 %.1fW" % self.rate_w
        if self.stuck:
            s += " · 已达下限，请降画质"
        return "PD 保护：" + s
