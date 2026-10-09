# -*- coding: utf-8 -*-
"""离线验证 algo.py 的行为：模拟各种工况，检查判定是否符合预期。

运行： python test_algo.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lp.algo import LoadTracker, SwitchGovernor  # noqa: E402

PASS, FAIL = "[PASS]", "[FAIL]"
fails = 0


def check(name, cond, detail=""):
    global fails
    if cond:
        print("%s %s %s" % (PASS, name, detail))
    else:
        fails += 1
        print("%s %s %s" % (FAIL, name, detail))


def run(tracker, samples):
    """samples: list of (cpu, disk, net, gpu) 或 (cpu,)，每步代表 5 秒。"""
    t0 = time.time()
    out = []
    for i, s in enumerate(samples):
        s = tuple(s) + (None,) * (4 - len(s))
        snap = tracker.sample(s[0], s[1], s[2], s[3], now=t0 + i * 5.0)
        out.append(snap)
    return out


print("=" * 62)
print("1) 瞬时尖刺不应触发『重负载』（EWMA 过滤）")
tr = LoadTracker()
snaps = run(tr, [(3,)] * 6 + [(99,)] * 1 + [(3,)] * 6)
check("单点尖刺不误判", not any(s["heavy"] for s in snaps),
      "末态=%s" % snaps[-1]["state"])

print("=" * 62)
print("2) 持续重负载（编译/剪辑）应被识别")
tr = LoadTracker()
snaps = run(tr, [(8,)] * 3 + [(85,)] * 6)   # 连续 30 秒满载
check("持续重负载识别", snaps[-1]["heavy"], "末态=%s busy=%d" %
      (snaps[-1]["state"], snaps[-1]["busy"]))

print("=" * 62)
print("3) 重负载要持续够久才算（前 12 秒不触发）")
tr = LoadTracker()
snaps = run(tr, [(8,)] * 3 + [(85,)] * 2)   # 只有 10 秒
check("时长不足不触发", not snaps[-1]["heavy"], "state=%s" % snaps[-1]["state"])

print("=" * 62)
print("4) 重负载结束后需持续轻负载才解除（滞回）")
tr = LoadTracker()
snaps = run(tr, [(8,)] * 3 + [(85,)] * 8)
heavy_ok = snaps[-1]["heavy"]
snaps = run(tr, [(10,)] * 2)   # 只轻了 10 秒
check("退出需滞回", snaps[-1]["heavy"] and heavy_ok, "state=%s" % snaps[-1]["state"])
snaps = run(tr, [(5,)] * 6)    # 再轻 30 秒
check("持续轻负载后解除", not snaps[-1]["heavy"], "state=%s" % snaps[-1]["state"])

print("=" * 62)
print("5) 轻负载识别（读 PDF / 静态网页）")
tr = LoadTracker()
snaps = run(tr, [(12, 3, 2, 0)] * 5)
check("轻负载识别", snaps[-1]["state"] == "轻负载",
      "state=%s busy=%d" % (snaps[-1]["state"], snaps[-1]["busy"]))

print("=" * 62)
print("6) 大盘下载（高网络、低 CPU）也算重负载")
tr = LoadTracker()
snaps = run(tr, [(10, 5, 90, 0)] * 6)
check("网络重负载识别", snaps[-1]["heavy"],
      "net=%.0f heavy=%s" % (snaps[-1]["net"] or 0, snaps[-1]["heavy"]))

print("=" * 62)
print("7) 切换闸门：冷却期阻止频繁切换")
g = SwitchGovernor(cooldown=20.0)
t = 1000.0
ok1, _ = g.offer("office", 1, "balanced", now=t)
ok2, why2 = g.offer("battery", 1, "office", now=t + 5)     # 5 秒后又想切
ok3, _ = g.offer("battery", 1, "office", now=t + 25)       # 25 秒后
check("首次允许切换", ok1)
check("冷却期内阻止", not ok2, why2)
check("冷却期后放行", ok3)

print("=" * 62)
print("8) 切换闸门：游戏升档可抢占（不等冷却）")
g = SwitchGovernor(cooldown=20.0)
t = 2000.0
g.offer("office", 1, "balanced", now=t)
ok, why = g.offer("gaming", 1, "office", priority=True, now=t + 3)
check("游戏抢占式升档", ok, why)

print("=" * 62)
print("9) 切换闸门：需要连续命中 N 次")
g = SwitchGovernor(cooldown=0.0)
t = 3000.0
a, _ = g.offer("office", 3, "balanced", now=t)
b, _ = g.offer("office", 3, "balanced", now=t + 5)
c, _ = g.offer("office", 3, "balanced", now=t + 10)
check("第1、2次不切", not a and not b)
check("第3次切换", c)

print("=" * 62)
print("10) 趋势判据：爬升 vs 降温")
tr = LoadTracker()
run(tr, [(20,)] * 4)
run(tr, [(90,)])
up = tr.cpu.trend
run(tr, [(10,)] * 4)
down = tr.cpu.trend
check("爬升 trend>0", up > 0, "trend=%.2f" % up)
check("降温 trend<0", down < 0, "trend=%.2f" % down)

print("=" * 62)
if fails:
    print("结果：%d 项失败" % fails)
    sys.exit(1)
print("全部通过 ✔")
