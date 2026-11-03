# -*- coding: utf-8 -*-
"""真机短接管验证：启用 → 两拍 → 停用（finally 保证释放）。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lp.fancurve import FanCurve

fc = FanCurve(cfg={"fan_curve_enabled": False, "fan_curve_min_pct": 30},
              data_path=None, watchdog=False)
print("初始状态:", {k: fc.state()[k] for k in ("enabled", "manual", "fan_now", "cpu_temp")})
try:
    fc.set_enabled(True)
    for i in range(2):
        r = fc.tick()
        print("tick%d: %s" % (i, r))
        time.sleep(1.5)
    print("接管中状态:", {k: fc.state()[k] for k in ("manual", "fan_now", "fan_tag",
                                                    "last_pct", "note")})
finally:
    out = fc.release("验证结束，交回自动")
    print("release:", out)
time.sleep(1.0)
print("最终:", {k: fc.state()[k] for k in ("manual", "fan_now", "fan_tag", "note")})
