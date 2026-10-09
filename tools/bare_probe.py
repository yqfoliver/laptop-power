# -*- coding: utf-8 -*-
"""裸机基线直测：直接量「降刷 + 独显断电」这两层各值多少瓦（2026-10-07）

为什么不用 A-B-A-B
------------------
A-B-A-B（tools/battery_soak.py --abab）跑了 50 分钟，结论却是"基线无效"：
退出程序后刷新率仍是 60Hz、独显 Eco 仍是 1，两段状态几乎一样。根因是
退出还原失效（lp/display.py 的 original_hz 记录被提前返回跳过）。

与其等修好再花 45 分钟重跑，不如**直接把状态拨到裸机**量一次跳变：
    状态 X：60Hz + 独显断电（程序离电策略生效后）
    状态 Y：165Hz + 独显上电（真裸机）
两次各采样 N 秒，差值就是「降刷 + 独显断电」两层的合计收益。
比 A/B 快 20 倍，而且没有"基线被污染"的问题 —— 状态是**写死**的。

用法（离电、程序已退出、别动鼠标）：
    python tools/bare_probe.py --seconds 90
    python tools/bare_probe.py --seconds 90 --no-restore     # 测完停在裸机态
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from lp import display, gpueco, hw, power  # noqa: E402


def sampler():
    pm = power.PowerMonitor()
    for _ in range(3):                 # PDH 速率计数器前两次采样无效，先预热
        pm.sample(want_cores=False, want_gpu=False)
    return pm


def measure(pm, seconds, interval=2.0):
    """采样 seconds 秒，丢掉前 1/3（切换后的瞬态），返回整数功耗均值。"""
    vals, socs, n = [], [], max(3, int(seconds / interval))
    for _ in range(n):
        s = pm.sample(want_cores=False, want_gpu=False) or {}
        if s.get("system_w"):
            vals.append(s["system_w"])
        if s.get("soc_w"):
            socs.append(s["soc_w"])
        time.sleep(interval)
    keep = vals[len(vals) // 3:] or vals

    def avg(x):
        return round(sum(x) / len(x), 2) if x else None

    return {"system_w": avg(keep), "soc_w": avg(socs[len(socs) // 3:] or socs),
            "n": len(vals)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--settle", type=float, default=12.0)
    ap.add_argument("--no-restore", action="store_true",
                    help="测完不还原（停在裸机态），便于手动观察")
    ap.add_argument("--full-stack", action="store_true",
                    help="量「完整栈」：状态X = 自建计划+60Hz+断电，"
                         "状态Y = **系统平衡计划**+165Hz+独显上电（真裸机）。"
                         "不加这个参数时只有刷新率和独显两层会被拨动，"
                         "而自建计划里的 CPU 上限/熄屏/待机等设置是**持久写在计划里**的，"
                         "程序退出也不会消失 —— 那样测出来的「裸机」其实还带着我们的设置")
    ap.add_argument("--keep-screen", action="store_true",
                    help="测量期间强制亮屏。熄屏的话屏幕那一层的收益会被吃掉，"
                         "165Hz vs 60Hz 的差值根本测不出来 —— 要量「屏幕值多少瓦」"
                         "必须开着屏测")
    args = ap.parse_args()

    if args.keep_screen:
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
            print("亮屏保持：已开启（屏幕不会被熄屏策略关掉）")
        except Exception as e:
            print("亮屏保持开启失败：%r" % e)

    st = hw.power_status()
    print("=" * 66)
    print("裸机基线直测")
    print("=" * 66)
    print("电源：%s  电量 %s%%" % ("插电" if st.get("ac") else "离电",
                                   st.get("battery_percent")))
    if st.get("ac"):
        print("!! 插电状态：独显 Eco 与降刷策略本就不生效，测不出差值")

    import subprocess
    CUSTOM = "684f7fa1-32d5-41ed-b743-f71f619fb2d0"    # 笔记本自适应电源
    BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"  # 系统「平衡」

    def set_plan(guid):
        # 必须走 Python subprocess：Git Bash 会把 powercfg 的参数改写掉
        subprocess.run(["powercfg", "/setactive", guid],
                       capture_output=True, timeout=30)

    d = display.DisplayCtl()
    g = gpueco.GpuEco()
    print("起始状态：%sHz  eco=%s" % (d.current_hz(), g.read()))
    if args.full_stack:
        set_plan(CUSTOM)
        time.sleep(3)

    pm = sampler()

    # ---------- 状态 X：当前（多为 60Hz + 断电） ----------
    print("\n【状态 X】当前：%sHz / eco=%s —— 采样 %.0fs"
          % (d.current_hz(), g.read(), args.seconds))
    x = measure(pm, args.seconds, args.interval)
    print("   整机 %sW（SoC %sW，n=%d）" % (x["system_w"], x["soc_w"], x["n"]))

    # ---------- 拨到裸机 ----------
    print("\n→ 拨到裸机：刷新率还原 + 独显上电")
    if args.full_stack:
        set_plan(BALANCED)          # CPU 上限/熄屏/待机一起回到系统默认
        print("   活动计划 → 系统「平衡」（真裸机）")
        time.sleep(3)
    ok_hz = d.restore(force_hz=165)
    time.sleep(args.settle)
    ok_eco = g.set(0)
    time.sleep(args.settle)
    print("   刷新率还原 %s（当前 %sHz） 独显上电 %s（eco=%s）"
          % ("成功" if ok_hz else "失败", d.current_hz(),
             "成功" if ok_eco else "失败", g.read()))

    print("\n【状态 Y】裸机：%sHz / eco=%s —— 采样 %.0fs"
          % (d.current_hz(), g.read(), args.seconds))
    y = measure(pm, args.seconds, args.interval)
    print("   整机 %sW（SoC %sW，n=%d）" % (y["system_w"], y["soc_w"], y["n"]))

    print("\n" + "=" * 66)
    if x["system_w"] and y["system_w"]:
        d1 = y["system_w"] - x["system_w"]
        print("裸机 %.2fW  →  省电态 %.2fW    差 %+.2f W（%+.1f%%）"
              % (y["system_w"], x["system_w"], -d1,
                 100.0 * d1 / y["system_w"] if y["system_w"] else 0))
        print("这就是「降刷 + 独显 ACPI 断电」两层的合计收益。")
    else:
        print("采样失败，未得出差值")

    if not args.no_restore:
        print("\n→ 还原到省电态（自建计划 + 60Hz + 独显断电）")
        if args.full_stack:
            set_plan(CUSTOM)
            time.sleep(3)
        print("   降刷 %s  断电 %s"
              % ("成功" if d.apply(60) else "失败", "成功" if g.set(1) else "失败"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
