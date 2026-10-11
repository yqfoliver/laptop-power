# -*- coding: utf-8 -*-
"""后台盯充电状态变化：记录「充到上限停充」与「改上限后恢复充电」的时刻。

用法：python tools/_watch_charge.py [秒数] [输出文件]
默认 900 秒，每 20 秒采一次，只在状态**发生变化**时写一行（省日志）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lp import battery  # noqa: E402
from lp import hw  # noqa: E402

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 900.0
OUT = sys.argv[2] if len(sys.argv) > 2 else "charge_watch.log"

b = battery.BatteryMonitor()
last = None
t0 = time.time()
with open(OUT, "a", encoding="utf-8") as f:
    f.write("=== watch start %s ===\n" % time.strftime("%H:%M:%S"))
    while time.time() - t0 < DUR:
        try:
            s = b.sample()
            cl = hw.asus_charge_limit()
            key = (s.get("percent"), s.get("ac"), s.get("charging"),
                   cl.get("value"))
            if key != last:
                line = ("%s 电量=%s%% AC=%s 充电中=%s 功率=%sW 上限=%s%%"
                        % (time.strftime("%H:%M:%S"), s.get("percent"),
                           s.get("ac"), s.get("charging"), s.get("rate_w"),
                           cl.get("value")))
                print(line, flush=True)
                f.write(line + "\n")
                f.flush()
                last = key
        except Exception as e:
            f.write("ERR %r\n" % (e,))
            f.flush()
        time.sleep(20.0)
print("=== watch done ===")
