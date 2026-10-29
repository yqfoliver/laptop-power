# -*- coding: utf-8 -*-
"""
hw 负载采样回归测试（第六轮审查）
=================================
只测纯计算部分（打桩网络计数器），不碰真实硬件：

  1. net_percent 在 elapsed≈0 时不能把几百字节放大成上百个百分点
     —— 早前的 `max(elapsed, 0.001)` 兜底正是这么干的，而 algo.Ewma 的首个
     样本直接取原始值当初始化 ⇒ 启动后十几秒会假判「重负载」。
  2. 正常间隔下的换算口径不变（8 MB/s == 100%）。
  3. cpu_percent 的 idle 不能算两遍（kernel 时间本身已含 idle）。
  4. load_sample 首拍不产出假读数。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import hw

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


class FakeNet:
    """按预设序列吐累计字节数"""

    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def __call__(self):
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v


_orig = hw.net_bytes_total

print("== 1) net_percent 的间隔下限 ==")
s = hw._LoadSampler()
hw.net_bytes_total = FakeNet([1000, 1200])
ok(s.net_percent(0.0) is None, "首拍无基线返回 None")
ok(s.net_percent(0.0) is None, "elapsed=0 不再拿 1ms 当分母（返回 None）")

# 每拍都有增量，才能区分「间隔太短」和「没有流量」两种 None
for el, tag in ((0.01, "10ms"), (0.499, "0.499s")):
    hw.net_bytes_total = FakeNet([0, 100, 200])
    sx = hw._LoadSampler()
    sx.net_percent(5.0)
    ok(sx.net_percent(el) is None, "elapsed=%s 不报数（分母太小会放大成上百个百分点）" % tag)

hw.net_bytes_total = FakeNet([1200])          # 恒定的计数器 = 没有流量
s2b = hw._LoadSampler()
s2b.net_percent(0.0)
ok(s2b.net_percent(5.0) == 0.0, "计数器不涨时报 0（不是 None）")

print("== 2) 正常间隔的换算口径 ==")
# 8 MB/s ⇒ 100%；先建立基线，再按 5 秒推进
step = int(8 * 1024 * 1024 * 5)
hw.net_bytes_total = FakeNet([0, step])
s2 = hw._LoadSampler()
s2.net_percent(5.0)
v2 = s2.net_percent(5.0)
ok(v2 is not None and abs(v2 - 100.0) < 0.01, "8MB/s 折算 100%", str(v2))

step4 = int(4 * 1024 * 1024 * 5)          # 4 MB/s ⇒ 50%
hw.net_bytes_total = FakeNet([0, step4])
s3 = hw._LoadSampler()
s3.net_percent(5.0)
v3 = s3.net_percent(5.0)
ok(v3 is not None and abs(v3 - 50.0) < 0.01, "4MB/s 折算 50%", str(v3))

hw.net_bytes_total = FakeNet([0, step * 10])   # 远超上限
s4 = hw._LoadSampler()
s4.net_percent(5.0)
v4 = s4.net_percent(5.0)
ok(v4 == 100.0, "超量程钳到 100%", str(v4))

hw.net_bytes_total = FakeNet([10 ** 9, 5])     # 32 位回绕/负增量
s5 = hw._LoadSampler()
s5.net_percent(5.0)
v5 = s5.net_percent(5.0)
ok(v5 == 0.0, "计数器回绕按 0 处理", str(v5))

hw.net_bytes_total = _orig

print("== 3) load_sample 首拍 ==")
hw.net_bytes_total = FakeNet([1000, 3000])     # 有流量，但首拍 elapsed=0
_sampler_orig = hw._sampler
hw._sampler = hw._LoadSampler()
try:
    r = hw.load_sample(None)
    ok(r.get("net") is None, "首拍不报网络占用（旧代码会报 100%）", str(r.get("net")))
    ok(r.get("gpu") is None, "没喂 gpu 时为 None")
    r2 = hw.load_sample(None)                  # elapsed≈0 的第二拍
    ok(r2.get("net") is None, "间隔过短的第二拍也不报数", str(r2.get("net")))
finally:
    hw._sampler = _sampler_orig
    hw.net_bytes_total = _orig

print("== 4) cpu_percent 的 idle 不重复计入 ==")
# GetSystemTimes 的 kernel 时间**本身已包含** idle。正确口径：
#   忙碌 = (Δkernel + Δuser) - Δidle，分母 = Δkernel + Δuser
# 旧写法 (k+u)/(k+u+idle) 把 idle 算了两遍，占用率虚高。


class FakeK32:
    """只实现 GetSystemTimes，按序列填进 byref 传进来的 FILETIME"""

    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def GetSystemTimes(self, idle, kern, user):
        i, k, u = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        for obj, v in ((idle, i), (kern, k), (user, u)):
            ft = obj._obj
            ft.dwLowDateTime = v & 0xFFFFFFFF
            ft.dwHighDateTime = (v >> 32) & 0xFFFFFFFF
        return 1


_orig_k32 = hw.k32
# 第二帧：Δkernel=200、Δuser=100、Δidle=200 ⇒ 忙碌 100/300 = 33.3%
hw.k32 = FakeK32([(50, 200, 100), (250, 400, 200)])
try:
    s6 = hw._LoadSampler()
    ok(s6.cpu_percent() is None, "首拍无基线返回 None")
    v6 = s6.cpu_percent()
    ok(v6 is not None and abs(v6 - 100.0 / 3.0) < 0.01,
       "idle 只扣一次（33.3%，旧写法会报 60%）", str(v6))
finally:
    hw.k32 = _orig_k32

print()
print("结果: %d 通过, %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
