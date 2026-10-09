# -*- coding: utf-8 -*-
"""Type-C/PD 供电下的游戏负载实测（只读，不写任何设置）。

用法：
    python tools/pd_game_test.py [秒数] [间隔]   # 默认 180s / 2s
    python tools/pd_game_test.py 180 2 out.csv

输出：
  * 逐样本一行（整机 W / 电池 rate / CPU W / 独显 W / 频率 / 温度 / 限频）
  * 结束给汇总：均值/峰值、累计放电 Wh、电源可持续功率估计
    电源估计 = 放电接近 0（电池不出力）时的整机功耗，即适配器能长期供的上限。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from lp import battery, hw, power, nvmlctl, powrprof, config  # noqa: E402


def _fmt(v, fmt="%.1f", na="-"):
    return na if v is None else (fmt % v)


def _proc_max(pw_mod):
    """读当前活动计划的 CPU 上限（AC），失败返回 None"""
    try:
        sc = pw_mod.active_scheme()
        if not sc:
            return None
        sub, setting = pw_mod.ATTR["PROCTHROTTLEMAX"]
        return pw_mod.read_value(sc, sub, setting, on_battery=False)
    except Exception:
        return None


def main():
    args = [a for a in sys.argv[1:]]
    dur = float(args[0]) if len(args) > 0 else 180.0
    step = float(args[1]) if len(args) > 1 else 2.0
    out_csv = args[2] if len(args) > 2 else os.path.join(HERE, "pd_game_log.csv")

    cfg = config.load()
    bat = battery.BatteryMonitor()
    pw = power.PowerMonitor(nvml=nvmlctl.Nvml())

    print("采样 %gs / 每 %gs（只读，不改设置）" % (dur, step))
    print("%-6s %8s %8s %8s %8s %7s %7s %7s %6s %6s %s"
          % ("t", "system", "batRate", "soc", "gpu", "cpuMHz", "gpuMHz",
             "cpuT", "gpuT", "gpuU", "备注"))
    rows = []
    t0 = time.time()
    n = 0
    while time.time() - t0 < dur:
        bs = bat.sample(force=True)
        sn = pw.sample(want_gpu=True)
        st = hw.power_status()
        n += 1
        t = time.time() - t0
        rate = bs.get("rate_w")
        dis = "放电" if (rate or 0) > 0.2 else ("充电" if (rate or 0) < -0.2 else "持平")
        thr = ",".join(str(x) for x in (sn.get("gpu_throttle") or []))[:18]
        note = "%s%s" % (dis, (" " + thr) if thr else "")
        print("%-6.0f %8s %8s %8s %8s %7s %7s %7s %6s %6s %s"
              % (t,
                 _fmt(sn.get("system_w")),
                 _fmt(rate, "%.2f"),
                 _fmt(sn.get("soc_w")),
                 _fmt(sn.get("gpu_w")),
                 _fmt(sn.get("cpu_mhz"), "%.0f"),
                 _fmt(sn.get("gpu_clock"), "%.0f"),
                 _fmt(sn.get("cpu_temp"), "%.0f"),
                 _fmt(sn.get("gpu_temp"), "%.0f"),
                 _fmt(sn.get("gpu_util"), "%.0f"),
                 note))
        rows.append({
            "t": round(t, 1), "system_w": sn.get("system_w"), "rate_w": rate,
            "soc_w": sn.get("soc_w"), "gpu_w": sn.get("gpu_w"),
            "cpu_mhz": sn.get("cpu_mhz"), "gpu_clock": sn.get("gpu_clock"),
            "cpu_temp": sn.get("cpu_temp"), "gpu_temp": sn.get("gpu_temp"),
            "gpu_util": sn.get("gpu_util"), "ac": st.get("ac"),
            "percent": bs.get("percent"),
            "proc_max_ac": _proc_max(powrprof),
        })
        time.sleep(step)

    # ---------------- 汇总 ----------------
    def agg(key):
        vs = [r[key] for r in rows if r.get(key) is not None]
        if not vs:
            return None, None, None
        return sum(vs) / len(vs), max(vs), min(vs)

    print()
    print("=" * 70)
    print("汇总（%d 样本）" % len(rows))
    print("=" * 70)
    for key, label in (("system_w", "整机 W"), ("rate_w", "电池 rate W"),
                       ("soc_w", "SoC W"), ("gpu_w", "独显 W"),
                       ("cpu_temp", "CPU ℃"), ("gpu_temp", "GPU ℃"),
                       ("cpu_mhz", "CPU MHz"), ("gpu_clock", "GPU MHz"),
                       ("gpu_util", "GPU 占用%")):
        a, mx, mn = agg(key)
        if a is None:
            print("  %-10s 无数据" % label)
        else:
            print("  %-10s 均值 %8.1f  峰值 %8.1f  最低 %8.1f" % (label, a, mx, mn))

    # 累计净放电（Wh，用 rate 对时间积分）
    wh = 0.0
    for i in range(1, len(rows)):
        r0, r1 = rows[i - 1], rows[i]
        if r0.get("rate_w") is None or r1.get("rate_w") is None:
            continue
        dt = (r1["t"] - r0["t"]) / 3600.0
        wh += dt * (r0["rate_w"] + r1["rate_w"]) / 2.0
    print("  本轮电池净变化：%+.3f Wh（正=净放电）" % wh)

    # 适配器可持续功率估计：找 |rate| 最小的样本（电池不出力）时的整机功耗
    cand = [r for r in rows if r.get("system_w") and r.get("rate_w") is not None
            and abs(r["rate_w"]) <= 1.5]
    if cand:
        vals = [r["system_w"] for r in cand]
        print("  电池不出力时的整机功耗：均值 %.1fW（%d 样本）→ 适配器可持续 ≈ %.0fW"
              % (sum(vals) / len(vals), len(vals), sum(vals) / len(vals)))
    else:
        print("  本轮没有「电池不出力」样本，无法直接估计适配器功率")
    dis_rows = [r for r in rows if (r.get("rate_w") or 0) > 1.5]
    if dis_rows:
        print("  ⚠ 有 %d/%d 样本在插电放电（电源供不上），均值 %.1fW"
              % (len(dis_rows), len(rows),
                 sum(r["rate_w"] for r in dis_rows) / len(dis_rows)))
    try:
        with open(out_csv, "w", encoding="utf-8") as f:
            keys = ["t", "system_w", "rate_w", "soc_w", "gpu_w", "cpu_mhz",
                    "gpu_clock", "cpu_temp", "gpu_temp", "gpu_util", "ac",
                    "percent", "proc_max_ac"]
            f.write(",".join(keys) + "\n")
            for r in rows:
                f.write(",".join("" if r.get(k) is None else str(r.get(k))
                                 for k in keys) + "\n")
        print("  明细已存：%s" % out_csv)
    except Exception as e:
        print("  写 CSV 失败: %r" % e)
    print("  当前配置：pd_guard_enabled=%s gpu_tgp_max_watts=%s"
          % (cfg.get("pd_guard_enabled"), cfg.get("gpu_tgp_max_watts")))


if __name__ == "__main__":
    main()
