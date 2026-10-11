# -*- coding: utf-8 -*-
"""打印电池当前状态（只读）：用于判断充电上限是否真的卡住了充电。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lp import battery  # noqa: E402
from lp import hw  # noqa: E402

cl = hw.asus_charge_limit()
print("充电上限(注册表): %s%%  %s" % (cl.get("value"), cl.get("mode_cn")))

b = battery.BatteryMonitor()
for i in range(3):
    s = b.sample()
    print("  #%d 电量=%s%%  AC=%s  充电中=%s  放电中=%s  功率=%s W"
          % (i, s.get("percent"), s.get("ac"), s.get("charging"),
             s.get("discharging"), s.get("rate_w")))
    if i < 2:
        time.sleep(2.0)
