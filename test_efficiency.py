# -*- coding: utf-8 -*-
"""
能效曲线落地测试（2026-10-06）

背景：
    借鉴极客湾（Geekerwan）的笔记本显卡功耗-性能曲线，并与超能网 /
    LaptopMedia 的 RTX 4060 Laptop 分档数据互证，得到结论：
        · 45W 基线 -> 80W 约 +28%，80W -> 105W 仅 +3.7%（拐点 ~80W）
        · 本机实测（power_alloc.csv 228 条真实游戏采样）峰值仅 68.8W
          —— 根本没到拐点，所以"压 CPU 让瓦给独显"全程划算，方向正确；
             但一旦到顶就必须立刻停，否则是白丢 CPU 性能。

    落地前发现真 bug：分配器默认 gpu_tgp_max=100W（文档写 75+25），
    与硬件不符 —— 0.97*100=97W 的"到顶"判据在本机永远达不到，
    于是分配器会一直压 CPU 让瓦，而 GPU 早就封顶，让出去的瓦全浪费。

本测试锁住三件事：
    1. 标定值来自真实硬件（70W），且旧值 100W 确实会失效（回归保护）
    2. 能效甜点生效：让渡停止点 = min(物理上限, 甜点)
    3. 用真实历史采样回放，验证"到顶即停"而不是"无限让渡"
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lp import config  # noqa: E402
from lp.alloc import PowerAllocator, _mk  # noqa: E402

OK, BAD = [], []


def check(name, cond, extra=""):
    (OK if BAD is None else (OK if cond else BAD)).append(name)
    print("  [%s] %s %s" % ("OK " if cond else "FAIL", name, extra))


def feed_once(a, gpu_w, base=100, gpu_util=99, core_max=60,
              cpu_util=45, cpu_w=20, soc_w=25):
    """喂一帧并立刻返回（绕过 8 秒让渡验证等待）"""
    snap = _mk(gpu_util=gpu_util, core_max=core_max, cpu_util=cpu_util,
               gpu_w=gpu_w, soc_w=soc_w, cpu_w=cpu_w,
               cpu_temp=85, gpu_temp=80)
    a._hist = [snap] * a.HIST_N
    a._probe = None
    r = a.feed(snap, base, active=True, on_ac=True)
    return a._cur if r is None else r["proc_max"]


print("=" * 72)
print("1) 标定值必须来自真实硬件")
print("=" * 72)
d = config.load()
tgp = float(d.get("gpu_tgp_max_watts", 70))
sweet = float(d.get("gpu_sweet_watts", 80))
print("   config: gpu_tgp_max_watts=%s  gpu_sweet_watts=%s" % (tgp, sweet))
check("独显上限按本机实测标定为 ~70W（非旧的 100W）", 65 <= tgp <= 75)
check("能效甜点默认 80W（极客湾曲线拐点）", sweet == 80)
check("本机上限 < 甜点 => 让渡全程有效，按物理上限停", tgp < sweet)

print()
print("=" * 72)
print("2) 旧值 100W 会让到顶判据失效（回归保护）")
print("=" * 72)
# 本机真实峰值 68.8W
PEAK = 68.8
old = PowerAllocator({"gpu_tgp_max_watts": 100, "alloc_cooldown_seconds": 0,
                      "alloc_min_cpu_pct": 45, "alloc_step_pct": 5})
new = PowerAllocator({"gpu_tgp_max_watts": 70, "alloc_cooldown_seconds": 0,
                      "alloc_min_cpu_pct": 45, "alloc_step_pct": 5})
old._cur = old._base = new._cur = new._base = 100
feed_once(old, PEAK)
feed_once(new, PEAK)
print("   峰值 %.1fW 时：旧标定(100W) cap_reached=%s / 新标定(70W) cap_reached=%s"
      % (PEAK, old._cap_reached, new._cap_reached))
check("旧标定下到顶判据永远触发不了（这正是原 bug）", old._cap_reached is False)
check("新标定下峰值即判定到顶", new._cap_reached is True)

print()
print("=" * 72)
print("3) 能效甜点：越过拐点后不再做无收益让渡")
print("=" * 72)
# 假设一台 140W 显卡的机器：物理上限 140，但甜点 80
big = PowerAllocator({"gpu_tgp_max_watts": 140, "gpu_sweet_watts": 80,
                      "alloc_cooldown_seconds": 0, "alloc_min_cpu_pct": 45,
                      "alloc_step_pct": 5})
check("停止点 = min(140, 80) = 80W", big.gpu_eff_cap == 80.0)
big._cur = big._base = 100
feed_once(big, 82.0)          # 已过甜点，但未到物理上限 140
print("   GPU 82W（物理上限 140W）时 cap_reached =", big._cap_reached)
check("未到物理上限但已过甜点 => 停止让渡", big._cap_reached is True)

low = PowerAllocator({"gpu_tgp_max_watts": 140, "gpu_sweet_watts": 80,
                      "alloc_cooldown_seconds": 0})
low._cur = low._base = 100
feed_once(low, 60.0)          # 甜点以下，应继续让渡
check("甜点以下仍继续让渡", low._cap_reached is False and low._cur < 100)

print()
print("=" * 72)
print("4) 本机真实功耗区间回放（55W 起 -> 69W 到顶）")
print("=" * 72)
a = PowerAllocator({"gpu_tgp_max_watts": 70, "gpu_sweet_watts": 80,
                    "alloc_cooldown_seconds": 0, "alloc_min_cpu_pct": 45,
                    "alloc_step_pct": 5})
a._cur = a._base = 100
traj = []
for i in range(9):
    traj.append(feed_once(a, 55 + 2.0 * i, cpu_w=22 - 2 * i))
print("   GPU 55→71W 对应的 CPU 上限轨迹:", traj)
check("未到顶时持续让渡（CPU 上限逐步下降）", traj[0] < 100 and traj[1] < traj[0])
check("到顶后不再继续降（轨迹后段持平）", traj[-1] == traj[-2])
check("让渡有下限保护（不低于 45%）", min(traj) >= 45)

print()
print("=" * 72)
print("5) 历史 CSV 回放（真实游戏采样，非构造数据）")
print("=" * 72)
csv_path = os.path.join(HERE, "power_alloc.csv")
if not os.path.isfile(csv_path):
    print("   跳过：未找到 power_alloc.csv")
else:
    import csv
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    gws = [float(r["gpu_w"]) for r in rows
           if r.get("gpu_w") not in (None, "", "None")]
    peak, avg = max(gws), sum(gws) / len(gws)
    print("   %d 条采样：峰值 %.1fW / 均值 %.1fW" % (len(rows), peak, avg))
    check("历史峰值确实低于旧标定的到顶线 97W（证明旧判据从未生效）",
          peak < 0.97 * 100)
    check("历史峰值能被新标定的到顶线(%.1fW)捕捉到" % (0.97 * 70),
          peak >= 0.97 * 70)

print()
print("=" * 72)
print("结果: %d 项通过, %d 项失败" % (len(OK), len(BAD)))
if BAD:
    print("失败项:", BAD)
print("=" * 72)
