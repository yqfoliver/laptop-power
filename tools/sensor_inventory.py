# -*- coding: utf-8 -*-
"""只读：把我们程序现在能读到的所有传感器打一张清单，用来和第三方工具对照。

回答的问题：既然借不到别人的接口，那我们自己漏了什么？清单见真章。
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from lp import hw, power            # noqa: E402
from lp import battery as _battery  # noqa: E402


def main():
    st = hw.power_status()
    print("电源: %s" % st)
    print()

    print("=" * 70)
    print("A) PDH 采样（pdmon.sample）—— 本机全部可用项")
    print("=" * 70)
    pm = power.PowerMonitor()
    snap = pm.sample(want_gpu=bool(st.get("ac")))
    order = ["soc_w", "cpu_w", "igpu_w", "system_w", "gpu_w",
             "cpu_util", "cpu_freq_mhz", "cpu_perf", "per_core",
             "temp_c", "tz_c", "gpu_temp_c", "gpu_util"]
    for k in order:
        if k in snap:
            v = snap[k]
            if isinstance(v, (list, tuple)):
                v = "%s… 共 %d 项" % (str(v[:6]), len(v))
            print("  %-14s %s" % (k, v))
    extra = [k for k in snap if k not in order]
    if extra:
        print("  其它字段: %s" % ", ".join(sorted(extra)))
    print()

    print("=" * 70)
    print("B) 一次随时间推移的第二次采样（PDH 前两次采样无效，这是已知坑）")
    print("=" * 70)
    import time
    time.sleep(1.1)
    snap2 = pm.sample(want_gpu=bool(st.get("ac")))
    for k in ("soc_w", "system_w", "cpu_util", "cpu_perf", "temp_c"):
        if k in snap2:
            print("  %-14s %s  (第一次是 %s)"
                  % (k, snap2[k], snap.get(k)))
    print()

    print("=" * 70)
    print("C) 电池（ACPI IOCTL）")
    print("=" * 70)
    try:
        b = _battery.BatteryMonitor().sample()
        for k in ("percent", "rate_w", "rate_avg_w", "full_mwh",
                  "design_mwh", "health_pct", "eta_min"):
            if k in b:
                print("  %-14s %s" % (k, b[k]))
    except Exception as e:
        print("  读取失败:", repr(e))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
