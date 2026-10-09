# -*- coding: utf-8 -*-
"""
能效曲线落地前的标定探测：本机独显的功耗上限到底是多少？

为什么需要它：
    分配器(alloc)里"独显已到顶就停止让瓦"的判据用的是 gpu_tgp_max，
    而该值默认 100W（文档注释写 75+25）。若本机真实上限是 55+20=75W，
    那么 0.97*100=97W 这个阈值永远达不到 —— 分配器会一直压 CPU 让瓦，
    可 GPU 早就封顶，多让的瓦全浪费（帧数不涨、CPU 性能白掉）。

本脚本只读，不写任何设置。
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from lp import atkacpi, hw, nvmlctl  # noqa: E402


def main():
    st = hw.power_status()
    print("电源：%s  电量 %s%%" % ("插电" if st.get("ac") else "离电",
                                  st.get("battery_percent")))
    if not st.get("ac"):
        print("离电状态：按设计不唤醒独显，改为只读 ATKACPI")
    print()

    # ---------- 1) ATKACPI：固件里的 TGP 基准 / 附加 ----------
    print("=" * 64)
    print("1) ATKACPI（固件侧 TGP 标定）")
    print("=" * 64)
    try:
        a = atkacpi.AtkAcpi()
        if not a.ok:
            print("  ATKACPI 不可用")
        else:
            for key, label in (("gpu_power_base", "TGP 基准"),
                               ("gpu_power_var", "TGP 附加(Dynamic Boost)")):
                try:
                    v = a.read_val(key)
                    print("  %-22s = %s" % (label, v))
                except Exception as e:
                    print("  %-22s 读取失败: %r" % (label, e))
            try:
                snap = a.snapshot()
                b = snap.get("gpu_base_w")
                v = snap.get("gpu_var_w")
                print("  快照 base=%s var=%s  -> 合计=%s W"
                      % (b, v, (b + v) if (b is not None and v is not None) else "?"))
            except Exception as e:
                print("  快照失败: %r" % e)
    except Exception as e:
        print("  ATKACPI 初始化失败: %r" % e)

    # ---------- 2) NVML：vBIOS 报的功耗上限 ----------
    print()
    print("=" * 64)
    print("2) NVML（vBIOS 侧功耗上限，会唤醒独显，故仅插电时读）")
    print("=" * 64)
    if not st.get("ac"):
        print("  跳过（离电不唤醒独显）")
        return
    try:
        n = nvmlctl.Nvml()
        if not n.ready:
            print("  NVML 不可用（独显可能处于 Eco 断电）")
            return
        lo, hi = n.power_limit_range()
        print("  可设功耗范围      = %s ~ %s W" % (lo, hi))
        print("  当前功耗上限      = %s mW" % n.get_power_limit())
        print("  实时功耗          = %s W" % n.power_usage())
        print("  SM 时钟           = %s / 最大 %s MHz"
              % (n.clock_sm(), n.max_clock_sm()))
        print("  温度              = %s ℃（降频阈值 %s ℃）"
              % (n.temperature(), n.temp_threshold()))
        print("  利用率            = %s %%" % n.utilization())
    except Exception as e:
        print("  NVML 探测失败: %r" % e)

    # ---------- 3) 结论提示 ----------
    print()
    print("=" * 64)
    print("3) 与 alloc 的 gpu_tgp_max 默认值对照")
    print("=" * 64)
    try:
        from lp import config
        c = config.load()
        print("  config.gpu_tgp_max_watts = %s（默认 100）"
              % c.get("gpu_tgp_max_watts", "(未设置→)100"))
        print("  -> 若真实上限低于该值，「已到顶停止让渡」判据将永不触发")
    except Exception as e:
        print("  读配置失败: %r" % e)


if __name__ == "__main__":
    main()
