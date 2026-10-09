# -*- coding: utf-8 -*-
"""
游戏模式下的 CPU / GPU 功耗分配算法（闭环自适应）

=============================================================================
一、本机的物理约束（决定算法长什么样）
=============================================================================
实测 + ASUS 官方规格（TUF Gaming A14 / FA401WV）：
    · 电源适配器          200 W          —— 不是瓶颈
    · 整机散热可承载功耗  ~110 W(CPU+GPU) —— 真正的天花板（14 寸薄机身）
    · 独显 TGP            ~70 W = 55 W 基础 + Dynamic Boost 实测 ~14 W
                          （2026-10-06 按 power_alloc.csv 228 条真实游戏采样标定：
                            峰值 68.8 W / 均值 59.8 W。此前误写 100 W = 75+25，
                            导致「已到顶停止让渡」判据 0.97*100=97 W 永远达不到）
    · CPU 单跑实测        42~47 W（12 线程）
    · CPU die 温度平台期  ~93 ℃（满负载恒温，已由 12 进程标定确认）

结论：**CPU 与 GPU 共享一个固定功耗包络**，两者不可能同时拉满。
      CPU 多拿 1 W，GPU 就少 1 W。这正是最大帧数的博弈点。

本机实测：独显功耗墙**不可软件设定**
    nvmlDeviceGetPowerManagementLimit -> rc=3 NOT_SUPPORTED
    nvmlDeviceSetPowerManagementLimit -> rc=4 INVALID_ARGUMENT
    （笔记本 vBIOS 锁死，ASUS 也不开放 WMI 裸 IOCTL）
所以唯一的控制杆是 **CPU 侧**：
    压低 CPU 最高频率 → CPU 少耗电 → 包络里空出来的瓦数被
    NVIDIA Dynamic Boost 自动转给独显 → GPU 功耗/频率上升 → 帧数上升。
    反过来，游戏吃 CPU 时把 CPU 上限放回去，别让 CPU 拖后腿。

=============================================================================
二、借鉴的成熟算法（检索资料）
=============================================================================
1) NVIDIA Dynamic Boost（Max-Q）：总功耗不变，AI 在 CPU/GPU/显存之间
   实时搬功耗，2.0 双向、上限 25 W，官方口径最高 +16% 性能。
   -> 我们不重复实现它，而是**主动为它让出空间**并验证让渡是否真的生效。
2) AMD STAPM / STT：基于机壳表面温度（OEM 典型 42~48 ℃）动态压低持续功耗
   （sPPT / fPPT / cTDP）。说明"温度许可范围"在本机是先由 CPU 自己守住的
   （实测 die 恒温 93 ℃），我们的角色是**不越过它**而不是替代它。
3) 瓶颈判定经验阈值（多个来源一致）：
   · GPU ≥ 95~97% 且 CPU 不高          -> GPU 瓶颈（健康，该加 GPU 的瓦）
   · 单核 ≥ 90% 且 GPU < 85%           -> CPU 单线程瓶颈
   · CPU 总占用 ≥ 85% 且 GPU < 85%     -> CPU 多线程瓶颈
   · 两者都高且温度上扬                -> 共享功耗/散热限制  ← 本机常态
   · GPU 利用率低但功耗/频率也低       -> 被功耗墙或温度墙卡住
   · 两者都低、帧数上限（VSync）       -> 假瓶颈，别动
4) 功耗→性能换算：笔记本 GPU 在 boost 区间频率与功耗近似 P^0.35
   （log 型），75W→100W 即 (100/75)^0.35 - 1 ≈ +10%，
   与 NVIDIA「最高 +16%」的口径同量级，取 0.35 做保守估计。
5) 能效曲线（2026-10-06 引入，来源：极客湾 Geekerwan 功耗-性能曲线实测，
   并与超能网 / LaptopMedia 的 RTX 4060 Laptop 分档数据互相印证）：
   · 45 W 基线 -> 80 W 约 +28%，80 W -> 105 W 仅 +3.7%，105 W -> 140 W +9.6%
   · 即 **拐点在 ~80 W**：越过之后每瓦换到的帧数急剧衰减
     （超能网：100 W vs 110 W 相差约 1%，80 W vs 100 W 相差约 6%）
   · 对本机的结论（重要）：本机上限 ~70 W，**还没到拐点**，
     所以「压 CPU 让瓦给独显」在全程都是划算的 —— 现有策略方向正确；
     但一旦真的到顶（~69 W）就必须立刻停止让渡，否则是白白丢 CPU 性能。
   · 落地方式：让渡停止点 = min(物理上限 gpu_tgp_max, 能效甜点 gpu_sweet_w)。
     本机 70 < 80 -> 按物理上限停；若换到 140 W 显卡的机器，会自动在 80 W 停。

=============================================================================
三、控制律（带假设验证与回滚，不做无用降频）
=============================================================================
GPU 瓶颈   -> CPU 上限每 30 s 降一档(默认 5%)，上限不低于 floor(默认 45%)
让渡验证   -> 降档后必须看到 GPU 功耗真的涨了(≥0.6 W)：
              涨了  = 继续让渡
              没涨  = 只是白白丢 CPU 性能 -> 回滚到上一档并停止让渡
              GPU 已到 min(上限 70 W, 能效甜点 80 W) / CPU 已 < 12 W -> 判定让渡到头
CPU 瓶颈   -> 把 CPU 上限加回，直到档位基准值
温度越界   -> 无条件降档（CPU > 96℃ 或独显 > 88℃；独显 slowdown 阈值实测 91℃）
去抖       -> 判定需连续 3 帧一致；两次调整间隔 ≥ 30 s；只在插电 + 游戏档生效
"""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

VERDICT_CN = {
    "gpu_bound": "GPU 瓶颈",
    "cpu_bound": "CPU 瓶颈",
    "balanced": "两者均衡",
    "shared_limit": "整机共享功耗墙",
    "gpu_power_capped": "独显被功耗/温度墙限制",
    "gpu_thermal": "独显撞温度墙",
    "frame_capped": "帧率上限/垂直同步",
    "idle": "独显空闲",
    "dc_off": "离电停用",
    "unknown": "判定中",
}

ACTION_CN = {
    "yield": "让出 CPU 预算给独显",
    "restore": "归还 CPU 性能",
    "hold": "维持现状",
    "thermal": "温度保护降档",
    "rollback": "让渡无效，回滚",
    "none": "—",
}


class PowerAllocator:
    """CPU/GPU 功耗分配器：只产出建议，不直接写电源计划（由 manager 执行）"""

    HIST_N = 6

    # NVML 降频原因里的「墙」分类（中文标签见 nvmlctl.THROTTLE_BITS）
    POWER_WALLS = ("功耗墙 SLOWDOWN", "功耗墙(TGP)", "软件限频")
    THERMAL_WALLS = ("温度墙 SW_THERMAL", "硬件温度墙", "功耗制动 HW_SLOWDOWN",
                     "硬件限速")

    @classmethod
    def _power_wall(cls, throttle) -> bool:
        return any(t in cls.POWER_WALLS for t in throttle)

    @classmethod
    def _thermal_wall(cls, throttle) -> bool:
        return any(t in cls.THERMAL_WALLS for t in throttle)

    def __init__(self, cfg: Dict, log_path: Optional[str] = None):
        self.cfg = cfg or {}
        self.log_path = log_path
        self._load_params()
        self._hist: List[dict] = []
        self._last_snap: Dict = {}
        self._cur: Optional[int] = None        # 本模块当前写下的 CPU 上限
        self._base: Optional[int] = None       # 档位基准上限
        self._last_change = 0.0
        self._cap_reached = False
        self._probe = None                     # 让渡验证用的对照点
        self._gpu_w_at_base: Optional[float] = None
        self.verdict = "unknown"
        self.report: Dict = self._build_report(None, "none", "尚未进入游戏档")
        self.events: List[str] = []
        self.env: Dict = {"items": [], "hints": []}      # 游戏调度环境检测（只读）

    def set_env(self, env: Optional[Dict]) -> None:
        """manager 传入 hw.gaming_env() 的结果，随报告一起展示"""
        if env:
            self.env = env
            self.report = self._build_report(self._base, "none", "环境检测已更新")

    def _load_params(self):
        """从 cfg 读取全部可调参数（面板改完设置后可热重载）"""
        c = self.cfg
        self.enabled = bool(c.get("alloc_enabled", True))
        self.floor_pct = int(c.get("alloc_min_cpu_pct", 45))
        self.step_pct = int(c.get("alloc_step_pct", 5))
        self.cooldown = float(c.get("alloc_cooldown_seconds", 30))
        self.cpu_temp_limit = float(c.get("cpu_temp_limit_c", 96))
        self.gpu_temp_limit = float(c.get("gpu_temp_limit_c", 88))
        self.envelope_w = float(c.get("power_envelope_watts", 110))
        self.overhead_w = float(c.get("board_overhead_watts", 12))
        # 离电时的整机功耗真值（电池放电功率），由 manager 每轮喂入。
        # 固定 12W 开销是按游戏负载（风扇全速）标定的，待机时实际平台开销
        # 只有 ~4W，用它算整机会凭空多出 8W（2026-10-07 实测：soc+12=17.2W
        # vs 电池真值 8.9W）。离电时电池放电就是整机功耗，直接采用。
        self.dc_true_w = None
        self._on_ac = True
        self.gpu_tgp_max = float(c.get("gpu_tgp_max_watts", 70))
        # 能效甜点：越过这个功耗后，每瓦换到的帧数急剧衰减（极客湾曲线拐点 ~80W）。
        # 让渡停止点取物理上限与甜点的较小值 —— 到不了甜点的机器（如本机 ~70W）
        # 按物理上限停；能超过甜点的机器则不再做无收益的让渡。
        self.gpu_sweet_w = float(c.get("gpu_sweet_watts", 80))
        self.gpu_eff_cap = min(self.gpu_tgp_max, self.gpu_sweet_w)
        if self.gpu_eff_cap <= 0:
            self.gpu_eff_cap = self.gpu_tgp_max
        self.gain_exp = float(c.get("alloc_gain_exponent", 0.35))
        self.verbose = bool(c.get("alloc_log_csv", True))

    def reload_cfg(self, cfg: Optional[Dict] = None):
        """面板保存设置后调用：立即生效，不用重启程序"""
        if cfg is not None:
            self.cfg = cfg
        self._load_params()
        self.report = self._build_report(self._base, "none", "参数已更新")

    # ------------------------------------------------------------------ 工具
    def _avg(self, key, n=None):
        n = n or self.HIST_N
        vals = [h.get(key) for h in self._hist[-n:] if h.get(key) is not None]
        if not vals:
            # 历史为空（非游戏档刚 reset）时退回最近一次原始采样，保证面板仍有读数
            v = self._last_snap.get(key)
            return v
        return sum(vals) / float(len(vals))

    def _note(self, text: str):
        stamp = time.strftime("%H:%M:%S")
        self.events.append("[%s] %s" % (stamp, text))
        del self.events[:-40]

    def reset(self, base_proc_max: Optional[int] = None):
        """离开游戏档 / 关闭开关时调用：交还控制权"""
        self._hist.clear()
        self._cur = None
        self._base = base_proc_max
        self._cap_reached = False
        self._probe = None
        self._gpu_w_at_base = None
        self.verdict = "unknown"

    # ------------------------------------------------------------------ 判定
    def classify(self) -> str:
        gpu_util = self._avg("gpu_util")
        core_max = self._avg("core_max")
        cpu_util = self._avg("cpu_util")
        gpu_w = self._avg("gpu_w")
        gpu_clk = self._avg("gpu_clock")
        cpu_temp = self._avg("cpu_temp")
        throttle = set()
        for h in self._hist[-3:]:
            throttle |= set(h.get("gpu_throttle") or [])

        if gpu_util is None:
            return "unknown"

        # 1) 独显基本空闲 -> 没在打游戏 / 帧率上限
        if gpu_util < 25 and (gpu_w is None or gpu_w < 0.25 * self.gpu_tgp_max):
            return "frame_capped" if (core_max is not None and core_max < 55) else "idle"

        # 2) 温度墙/功耗墙把独显压住了
        if gpu_util < 75 and gpu_w is not None and gpu_w < 0.62 * self.gpu_tgp_max:
            if any(("墙" in t) or ("SLOWDOWN" in t) for t in throttle):
                return "gpu_power_capped"

        # 2b) 温度墙优先于功耗墙：独显在降频是因为热，不是因为没有电，
        #     此时把 CPU 的瓦让过去只会让两边都更热（文献：温度每 +10℃ 老化翻倍）。
        if gpu_util >= 80 and self._thermal_wall(throttle):
            return "gpu_thermal"

        # 3) GPU 瓶颈：独显吃满、CPU 还有余量
        if gpu_util >= 93 and (core_max is None or core_max < 88):
            return "gpu_bound"

        # 4) 共享功耗墙：两边都很忙（本机 14 寸机身的典型状态）
        if gpu_util >= 85 and ((core_max or 0) >= 85 or (cpu_util or 0) >= 80):
            return "shared_limit"

        # 5) CPU 瓶颈
        if (core_max or 0) >= 90 and gpu_util < 88:
            return "cpu_bound"
        if (cpu_util or 0) >= 85 and gpu_util < 88:
            return "cpu_bound"

        if gpu_util >= 85:
            return "gpu_bound"
        return "balanced"

    # ------------------------------------------------------------------ 主循环
    def feed(self, snap: dict, base_proc_max: Optional[int],
             active: bool, on_ac: bool) -> Optional[dict]:
        """
        每个巡检周期调用一次。
        active  : 当前是否处于游戏档（只有这里才动 CPU 上限）
        on_ac   : 是否插电（Dynamic Boost 只在插电时生效）
        返回 None = 不改动；返回 dict = 建议把 PROCTHROTTLEMAX 写成 proc_max
        """
        self._hist.append(dict(snap))
        del self._hist[:-self.HIST_N]
        self._last_snap = dict(snap)
        self._on_ac = bool(on_ac)

        if not self.enabled or not active or not on_ac:
            if self._cur is not None:
                self._note("退出游戏档/未插电，交还 CPU 上限")
            self.reset(base_proc_max)
            # 离电且在游戏档 -> 明确标「离电停用」，别让面板一直显示「判定中」
            self.verdict = "dc_off" if (active and not on_ac) else "unknown"
            why = ("功能未开启" if not self.enabled else
                   "离电中：独显休眠（走核显），分配器停用" if not on_ac else
                   "非游戏档，不参与功耗重分配")
            self.report = self._build_report(base_proc_max, "none", why)
            self.report["active"] = False
            return None

        if base_proc_max is not None and (self._base != base_proc_max or self._cur is None):
            self._base = base_proc_max
            self._cur = base_proc_max
            self._cap_reached = False
            self._probe = None
            self._last_change = time.time()

        if self._cur is None:
            # 读不到基准 CPU 上限（计划/子进程异常）：本周期不参与决策，
            # 否则下面的 self._cur ± step 会抛 TypeError 让分配器整段失效
            self.verdict = "unknown"
            self.report = self._build_report(None, "none", "暂时读不到 CPU 上限，本周期不调整")
            self.report["active"] = True
            return None

        self.verdict = self.classify()
        gpu_w = self._avg("gpu_w")
        cpu_w = self._avg("cpu_w")
        cpu_temp = self._avg("cpu_temp")
        gpu_temp = self._avg("gpu_temp")

        # 在基准档位时记录 GPU 功耗，作为"让渡收益"的参照
        if self._cur == self._base and gpu_w:
            self._gpu_w_at_base = gpu_w

        # ---------------- 让渡有效性验证（上一轮降档到底有没有用）
        if self._probe and not self._probe.get("done"):
            p = self._probe
            if (time.time() - p["at"]) >= 8.0 and len(self._hist) >= 3:
                d_gpu = (gpu_w or 0) - p["gpu_w"]
                d_cpu = p["cpu_w"] - (cpu_w or 0)
                if d_gpu < 0.6 and d_cpu > 1.0:
                    # 白降了：CPU 掉电但 GPU 没多拿 -> 回滚
                    p["done"] = True
                    self._cap_reached = True
                    self._note("让渡无效(CPU %.1f→%.1fW 但 GPU 仅 %+.1fW)，回滚到 %d%%" %
                               (p["cpu_w"], cpu_w or 0, d_gpu, p["proc_max_before"]))
                    self._cur = p["proc_max_before"]
                    self._last_change = time.time()
                    return {"proc_max": self._cur, "action": "rollback",
                            "reason": "让渡无效，已回滚（帧数不会因再降 CPU 而提高）",
                            "verdict": self.verdict}
                else:
                    p["done"] = True
                    if d_gpu >= 0.6:
                        self._note("让渡生效：GPU %+.1fW（CPU %.1f→%.1fW）" %
                                   (d_gpu, p["cpu_w"], cpu_w or 0))
                    else:
                        # GPU 没涨、CPU 也没明显降 -> 噪声，不能算「生效」，
                        # 但也不回滚，继续观察（回滚会把刚让出的预算又拿回来）
                        self._note("让渡效果不明（GPU %+.1fW / CPU %.1f→%.1fW），维持观察" %
                                   (d_gpu, p["cpu_w"], cpu_w or 0))

        # 让渡到头的情形
        # 到顶判据用「物理上限与能效甜点的较小值」：
        # 本机 ~70W 就到顶（实测峰值 68.8W）；即便某台机器能冲到 140W，
        # 越过甜点(~80W)后继续让瓦也换不到帧数，同样该停。
        if gpu_w is not None and gpu_w >= 0.97 * self.gpu_eff_cap and not self._cap_reached:
            self._cap_reached = True
            self._note("独显已达 %.0fW 上限（让渡停止点 %.0fW），停止让渡"
                       % (gpu_w, self.gpu_eff_cap))
        # 独显已触发 TGP/Dynamic Boost 功耗墙：说明包络里能给的瓦已经给到独显了，
        # 再压 CPU 也换不来帧数（文献：Dynamic Boost 上限 25W，到顶即无空间）。
        gpu_util_avg = self._avg("gpu_util")
        if (not self._cap_reached and gpu_util_avg is not None and gpu_util_avg >= 78
                and gpu_w is not None and gpu_w >= 0.85 * self.gpu_eff_cap
                and self._power_wall({t for h in self._hist[-3:] for t in (h.get("gpu_throttle") or [])})):
            self._cap_reached = True
            self._note("独显触发 TGP 功耗墙(%.0fW)，Dynamic Boost 已到顶，停止让渡" % (gpu_w,))
        if cpu_w is not None and cpu_w < 12.0 and not self._cap_reached:
            self._cap_reached = True
            self._note("CPU 已只吃 %.1fW，无处可让" % (cpu_w,))
        if self._cur is not None and self._cur <= self.floor_pct:
            self._cap_reached = True

        # ---------------- 决策
        target, action, reason = self._cur, "hold", "维持现状"

        if cpu_temp is not None and cpu_temp > self.cpu_temp_limit:
            target = max(self.floor_pct, self._cur - self.step_pct)
            action, reason = "thermal", "CPU 温度 %.0f℃ 已超过 %d℃ 上限" % (cpu_temp, self.cpu_temp_limit)
        elif gpu_temp is not None and gpu_temp > self.gpu_temp_limit:
            target = max(self.floor_pct, self._cur - self.step_pct)
            action, reason = "thermal", "独显温度 %.0f℃ 已超过 %d℃ 上限" % (gpu_temp, self.gpu_temp_limit)
        elif self.verdict == "gpu_thermal":
            # 独显撞温度墙：让瓦没用（热是唯一瓶颈），保持 CPU 预算、把原因说清楚
            target = min(self._base, self._cur + self.step_pct)
            if target != self._cur:
                action, reason = "restore", ("独显撞温度墙(%.0f℃)：让瓦无益，归还 CPU 性能，"
                                             "改善散热才是提帧手段" % (gpu_temp or 0))
            else:
                action, reason = "hold", ("独显撞温度墙(%.0f℃)：CPU 预算已在上限，"
                                          "建议垫高机身/清理风扇进风" % (gpu_temp or 0))
        elif self.verdict in ("gpu_bound", "gpu_power_capped"):
            if self._cap_reached:
                target, action, reason = self._cur, "hold", "CPU 已让到底，独显不再多拿电"
            else:
                step = self.step_pct * (2 if self.verdict == "gpu_power_capped" else 1)
                target = max(self.floor_pct, self._cur - step)
                action = "yield"
                reason = ("%s：把 CPU 预算让给 Dynamic Boost" % VERDICT_CN[self.verdict])
        elif self.verdict == "cpu_bound":
            target = min(self._base, self._cur + self.step_pct)
            action, reason = ("restore" if target != self._cur else "hold",
                              "CPU 瓶颈：归还 CPU 性能" if target != self._cur else "CPU 已在上限")
            # 只有真正回到基准档才允许重新让渡：否则会陷入
            # 「让渡→判成 CPU 瓶颈→归还半档→再判 GPU 瓶颈→再让渡」的每轮振荡
            if target == self._base:
                self._cap_reached = False
        elif self.verdict in ("balanced", "shared_limit"):
            if gpu_w is not None and gpu_w >= 0.93 * self.gpu_eff_cap:
                target, action, reason = self._cur, "hold", "GPU 已接近功耗上限，保持分配"
            else:
                target = min(self._base, self._cur + self.step_pct)
                action, reason = ("restore" if target != self._cur else "hold",
                                  "非 GPU 瓶颈，归还 CPU 性能" if target != self._cur else "维持现状")
                if target == self._base:
                    self._cap_reached = False
        else:  # idle / frame_capped / unknown
            target = self._base
            action, reason = ("restore" if target != self._cur else "hold",
                              "非游戏负载，恢复基准" if target != self._cur else "判定中")

        self.report = self._build_report(self._base, action, reason)
        self.report["active"] = True

        # ---------------- 冷却闸门
        changed = (target != self._cur)
        if not changed:
            return None
        if (time.time() - self._last_change) < self.cooldown:
            return None

        before = {"at": time.time(), "gpu_w": gpu_w or 0.0, "cpu_w": cpu_w or 0.0,
                  "proc_max_before": self._cur, "done": False}
        if action == "yield":
            self._probe = before
        self._cur = target
        self._last_change = time.time()
        self._note("%s → CPU 上限 %d%%（%s）" % (ACTION_CN.get(action, action), target, reason))
        if self.verbose and self.log_path:
            self._csv(snap)
        return {"proc_max": target, "action": action, "reason": reason, "verdict": self.verdict}

    # ------------------------------------------------------------------ 报告
    def _build_report(self, base: Optional[int], action: str, reason: str) -> Dict:
        gpu_w = self._avg("gpu_w")
        soc_w = self._avg("soc_w")
        used = None
        if not self._on_ac and self.dc_true_w:
            # 离电：电池放电功率就是整机功耗真值（含屏幕/风扇等一切开销）
            used = float(self.dc_true_w)
        elif soc_w is not None or gpu_w is not None:
            used = (soc_w or 0.0) + (gpu_w or 0.0) + self.overhead_w
        # 让渡收益估计：GPU 功耗相对基准档位涨了多少 -> 帧数估计
        gain = None
        ref = self._gpu_w_at_base
        if gpu_w and ref and ref > 1.0 and gpu_w > ref:
            gain = ((gpu_w / ref) ** self.gain_exp - 1.0) * 100.0
            gain = min(gain, 15.0)
        return {
            "verdict": self.verdict,
            "verdict_cn": VERDICT_CN.get(self.verdict, "判定中"),
            "action": action,
            "action_cn": ACTION_CN.get(action, action),
            "reason": reason,
            "cpu_w": self._avg("cpu_w"),
            "igpu_w": self._avg("igpu_w"),
            "gpu_w": gpu_w,
            "soc_w": soc_w,
            "system_w": used,
            "cpu_temp": self._avg("cpu_temp"),
            "gpu_temp": self._avg("gpu_temp"),
            "cpu_util": self._avg("cpu_util"),
            "core_max": self._avg("core_max"),
            "gpu_util": self._avg("gpu_util"),
            "cpu_mhz": self._avg("cpu_mhz"),
            "gpu_clock": self._avg("gpu_clock"),
            "gpu_clock_max": self._avg("gpu_clock_max"),
            "gpu_throttle": sorted({t for h in self._hist[-3:] for t in (h.get("gpu_throttle") or [])}),
            "envelope_w": self.envelope_w,
            "gpu_tgp_max": self.gpu_tgp_max,
            "gpu_sweet_w": self.gpu_sweet_w,
            "gpu_eff_cap": self.gpu_eff_cap,   # 让渡停止点 = min(物理上限, 能效甜点)
            "used_pct": None if used is None else round(100.0 * used / max(self.envelope_w, 1e-6), 1),
            "cpu_cap_pct": self._cur,
            "base_pct": base,
            "floor_pct": self.floor_pct,
            "cap_reached": self._cap_reached,
            "est_gain_pct": None if gain is None else round(gain, 1),
            "yielded_w": None if (gain is None or ref is None or gpu_w is None) else round(gpu_w - ref, 1),
            "env": getattr(self, "env", None) or {"items": [], "hints": []},
            "env_hints": list((getattr(self, "env", None) or {}).get("hints") or []),
            "env_short": list((getattr(self, "env", None) or {}).get("hints_short") or []),
        }

    def _csv(self, snap: dict):
        try:
            new = not os.path.exists(self.log_path)
            with open(self.log_path, "a", encoding="utf-8") as f:
                if new:
                    f.write("time,verdict,cpu_cap,soc_w,cpu_w,gpu_w,cpu_temp,gpu_temp,"
                            "cpu_util,core_max,gpu_util,cpu_mhz,gpu_clock\n")
                f.write("%s,%s,%s,%.1f,%.1f,%.1f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), self.verdict, self._cur,
                    snap.get("soc_w") or 0, snap.get("cpu_w") or 0, snap.get("gpu_w") or 0,
                    snap.get("cpu_temp") or 0, snap.get("gpu_temp") or 0,
                    snap.get("cpu_util") or 0, snap.get("core_max") or 0,
                    snap.get("gpu_util") or 0, snap.get("cpu_mhz") or 0,
                    snap.get("gpu_clock") or 0))
        except Exception:
            pass


# ------------------------------------------------------------------ 离线校验
def _mk(**kw):
    d = {"gpu_util": 0, "core_max": 0, "cpu_util": 0, "gpu_w": 20.0, "gpu_clock": 2000,
         "cpu_temp": 80.0, "gpu_temp": 70.0, "gpu_throttle": [], "cpu_w": 20.0,
         "soc_w": 30.0, "igpu_w": 5.0, "cpu_mhz": 3000.0, "gpu_clock_max": 2500.0}
    d.update(kw)
    return d


if __name__ == "__main__":
    cfg = {"alloc_enabled": True, "alloc_cooldown_seconds": 0}
    a = PowerAllocator(cfg)
    cases = [
        ("GPU 瓶颈", _mk(gpu_util=99, core_max=60, cpu_util=45, gpu_w=80)),
        ("CPU 瓶颈(单核)", _mk(gpu_util=70, core_max=97, cpu_util=60, gpu_w=60)),
        ("CPU 瓶颈(多核)", _mk(gpu_util=65, core_max=80, cpu_util=92, gpu_w=58)),
        ("共享功耗墙", _mk(gpu_util=92, core_max=90, cpu_util=88, gpu_w=88)),
        ("独显被功耗墙", _mk(gpu_util=55, core_max=50, cpu_util=40, gpu_w=48,
                             gpu_throttle=["功耗墙(TGP)"])),
        ("帧率上限", _mk(gpu_util=18, core_max=22, cpu_util=15, gpu_w=12, gpu_clock=300)),
        ("均衡", _mk(gpu_util=80, core_max=55, cpu_util=50, gpu_w=72)),
    ]
    for name, snap in cases:
        a._hist = [snap] * a.HIST_N
        print("  %-16s -> %-14s (%s)" % (name, a.classify(), VERDICT_CN[a.classify()]))
