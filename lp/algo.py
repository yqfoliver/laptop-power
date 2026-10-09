# -*- coding: utf-8 -*-
"""
场景判定的算法层（纯计算，不碰系统，可单独测试 / 离线模拟）。

设计参考了公开资料里的成熟做法：
  1) Windows Power Throttling / Adaptive Energy Saver（微软官方）
     核心理念：不只看「前台是谁」，而是持续采样 CPU / 磁盘 / 网络，
     区分「重负载任务（剪辑、编译、游戏）」与「轻负载（看 PDF、刷静态网页）」，
     轻负载时才激进的回收后台资源，重负载时绝不降频。
  2) EWMA 指数加权移动平均（NIST / Linux PELT 负载追踪同源思路）
     用途：过滤瞬时尖刺、只对「持续性」负载做反应。
     smoothed = alpha * current + (1 - alpha) * previous，alpha 越小记忆越长。
  3) 趋势判据（Least-Slope 思路）：比较原始值与平滑值，
     raw > smoothed → 正在爬升；raw < smoothed → 正在降温。
  4) AMD/Intel 官方建议：现代 SoC 的能耗大部分花在「离开空闲态」本身，
     所以「快速回落、少切换」比「切得准但来回切」更省电 —— 引入
     滞回（hysteresis）+ 冷却期（cooldown）来抑制切换抖动。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Optional, Tuple

# ------------------------------------------------------------------ 参数
# alpha: EWMA 平滑系数。0.35 ≈ 5~8 秒的有效记忆（配合 5 秒轮询）
ALPHA = 0.35

# 判定为「重负载」的门槛（EWMA 平滑值，单位 %）
HEAVY_CPU = 55.0      # 持续 CPU 占用
HEAVY_DISK = 45.0     # 持续磁盘活动
HEAVY_NET = 60.0      # 持续网络下载（大文件/更新）
HEAVY_GPU = 40.0      # 持续独显占用

# 进入重负载 / 退出重负载采用不同门槛（滞回，防止在边界抖动）
EXIT_RATIO = 0.72     # 退出重负载的门槛 = 进入门槛 × 0.72

# 判定为「轻负载」的门槛
LIGHT_CPU = 20.0
LIGHT_DISK = 15.0
LIGHT_NET = 25.0

# 连续处于重负载多久才认为「真的在干重活」（秒）
SUSTAIN_HEAVY = 12.0
# 从重负载退出需要连续轻多久（秒），防止编译间隙误判
SUSTAIN_LIGHT = 20.0

# 切换冷却期：两次自动切换至少间隔这么久（秒）
COOLDOWN = 20.0

# 采样历史长度（点数，5 秒/点 → 120 点 ≈ 10 分钟）
HISTORY = 120


class Ewma:
    """单指标指数加权移动平均 + 一阶趋势。"""

    __slots__ = ("alpha", "value", "prev", "n")

    def __init__(self, alpha: float = ALPHA):
        self.alpha = alpha
        self.value: Optional[float] = None
        self.prev:  Optional[float] = None
        self.n = 0

    def push(self, raw: Optional[float]) -> Optional[float]:
        if raw is None:
            return self.value
        raw = float(raw)
        self.prev = self.value
        if self.value is None:
            self.value = raw          # 首个样本直接初始化（同 PELT 约定）
        else:
            self.value = self.alpha * raw + (1.0 - self.alpha) * self.value
        self.n += 1
        return self.value

    @property
    def trend(self) -> float:
        """>0 爬升中，<0 降温中，≈0 平稳。"""
        if self.value is None or self.prev is None:
            return 0.0
        return self.value - self.prev


class LoadTracker:
    """多指标负载追踪：EWMA + 持续性判定 + 轻/重负载分类。"""

    def __init__(self):
        self.cpu = Ewma()
        self.disk = Ewma()
        self.net = Ewma()
        self.gpu = Ewma()
        self._heavy_since: Optional[float] = None
        self._light_since: Optional[float] = None
        self.heavy = False              # 当前是否处于「持续重负载」
        self.history = deque(maxlen=HISTORY)

    # -------------------------------------------------------------- 采样
    def sample(self, cpu, disk=None, net=None, gpu=None,
               now: Optional[float] = None) -> dict:
        now = now if now is not None else time.time()
        c = self.cpu.push(cpu)
        d = self.disk.push(disk)
        n = self.net.push(net)
        g = self.gpu.push(gpu)

        heavy_now = self._is_heavy(c, d, n, g)
        light_now = self._is_light(c, d, n, g, raw_cpu=cpu)

        if heavy_now:
            self._light_since = None
            if self._heavy_since is None:
                self._heavy_since = now
            if not self.heavy and (now - self._heavy_since) >= SUSTAIN_HEAVY:
                self.heavy = True
        elif light_now:
            self._heavy_since = None
            if self._light_since is None:
                self._light_since = now
            # 解除用「原始值持续轻」计时，不依赖平滑值 ——
            # EWMA 记忆长，只用平滑值会导致负载早已结束却迟迟不解除。
            if self.heavy and (now - self._light_since) >= SUSTAIN_LIGHT:
                self.heavy = False
        else:
            # 介于轻重之间：重置轻计时（避免断续轻负载凑够时长）
            self._light_since = None
            if not self.heavy:
                self._heavy_since = None

        snap = {
            "cpu": c, "disk": d, "net": n, "gpu": g,
            "cpu_raw": cpu, "heavy": self.heavy,
            "trend": self.cpu.trend,
            "busy": self.busy_score(),
            "state": self.state_label(),
        }
        self.history.append((now, snap))
        return snap

    # -------------------------------------------------------- 分类判据
    @staticmethod
    def _over(value: Optional[float], enter: float, exiting: bool) -> bool:
        if value is None:
            return False
        return value >= (enter * EXIT_RATIO if exiting else enter)

    def _is_heavy(self, c, d, n, g) -> bool:
        # 用「退出门槛」评估当前是否仍算重负载；用「进入门槛」评估是否新进入
        return (self._over(c, HEAVY_CPU, self.heavy)
                or self._over(d, HEAVY_DISK, self.heavy)
                or self._over(n, HEAVY_NET, self.heavy)
                or self._over(g, HEAVY_GPU, self.heavy))

    @staticmethod
    def _is_light(c, d, n, g, raw_cpu=None) -> bool:
        """轻负载判定：优先看原始值（响应快），缺失时退回平滑值。"""
        c_use = raw_cpu if raw_cpu is not None else c
        ok = True
        if c_use is not None:
            ok = ok and c_use <= LIGHT_CPU
        if d is not None:
            ok = ok and d <= LIGHT_DISK
        if n is not None:
            ok = ok and n <= LIGHT_NET
        if g is not None and g > 5.0:
            ok = False
        return ok

    # ------------------------------------------------------------ 输出
    def busy_score(self) -> int:
        """0~100 的综合忙碌度，用于面板展示。"""
        vals = []
        for v in (self.cpu.value, self.disk.value, self.net.value, self.gpu.value):
            if v is not None:
                vals.append(min(100.0, float(v)))
        if not vals:
            return 0
        return int(sum(vals) / len(vals))

    def state_label(self) -> str:
        if self.heavy:
            return "持续重负载"
        c = self.cpu.value
        if c is None:
            return "未知"
        if c <= LIGHT_CPU:
            return "轻负载"
        return "中等负载"

    def summary(self) -> dict:
        """给面板 / 日志用的一行摘要。"""
        return {
            "busy": self.busy_score(),
            "cpu": None if self.cpu.value is None else round(self.cpu.value, 1),
            "disk": None if self.disk.value is None else round(self.disk.value, 1),
            "net": None if self.net.value is None else round(self.net.value, 1),
            "gpu": None if self.gpu.value is None else round(self.gpu.value, 1),
            "heavy": self.heavy,
            "state": self.state_label(),
            "trend": round(self.cpu.trend, 2),
        }


class SwitchGovernor:
    """
    切换闸门：决定「候选档位」能不能真切。

    在原有「连续命中 N 次」的基础上增加：
      - 冷却期（COOLDOWN）：刚切过就别急着再切
      - 关键优先级（priority）：游戏升档可以插队，避免开黑前几秒还在省电
    """

    def __init__(self, cooldown: float = COOLDOWN):
        self.cooldown = cooldown
        self.last_switch = 0.0
        self.cand: Optional[str] = None
        self.hits = 0
        self.reason = ""

    def offer(self, target: Optional[str], need_hits: int,
              current: Optional[str], priority: bool = False,
              now: Optional[float] = None) -> Tuple[bool, str]:
        """喂一个候选档位，返回 (是否切换, 原因)。"""
        now = now if now is not None else time.time()

        if target != self.cand:
            self.cand, self.hits = target, 1
        else:
            self.hits += 1

        if target is None or target == current:
            return False, ""

        # 升档（比如切进游戏）允许降低命中要求，抢占式生效
        need = 1 if priority else max(1, int(need_hits))
        if self.hits < need:
            return False, ""

        since = now - self.last_switch
        if self.last_switch and since < self.cooldown and not priority:
            return False, "冷却中(%.0fs)" % (self.cooldown - since)

        self.last_switch = now
        self.reason = "确认%d次" % self.hits
        self.cand, self.hits = None, 0
        return True, self.reason

    def reset(self) -> None:
        self.cand, self.hits = None, 0


def build_reason(base: str, summary: dict, extra: str = "") -> str:
    """把负载摘要拼进切换原因，方便在面板/托盘里一眼看懂为什么切。

    2026-10-08 用户反馈重复：busy 百分比与负载卡片的进度条+百分比完全一样，
    不再拼进原因行（state「轻负载/重负载」保留 —— 它解释的是档位判定）。
    """
    parts = [base]
    s = summary or {}
    if s.get("state"):
        parts.append(s["state"])
    if extra:
        parts.append(extra)
    return " · ".join(p for p in parts if p)
