# -*- coding: utf-8 -*-
"""探测 GHelper「Fans + Power」里那几个 Windows 侧旋钮在本机是否真能写：
   PERFBOOSTMODE（处理器性能提升模式 = Turbo Boost 档）
   PERFEPP（EPP 能效偏好）、SYSCOOLPOL（散热策略 主动/被动）
只读 + 试写 + 立即还原，绝不留下改动。
"""
import sys
import time

sys.path.insert(0, ".")

from lp import powrprof as pp            # noqa: E402

SUB = pp.SUB_PROCESSOR
KNOBS = {
    "PERFBOOSTMODE": "be337238-0d82-4146-a960-4f3749d470c7",
    "PERFEPP":       "36687f9e-e3a5-4dbf-b1dc-15eb381c686b",
    "SYSCOOLPOL":    "94d3a615-a899-4ac5-ae2b-e4d8f634367f",
    "PROCTHROTTLEMAX": "bc5038f7-23e0-4960-96da-33abaf5935ec",
}
BOOST_CN = {0: "关闭睿频", 1: "启用", 2: "激进", 3: "高效启用", 4: "高效激进"}


def main():
    if not pp.available():
        print("powrprof 直调不可用")
        return 1
    scheme = pp.active_scheme()
    print("活动计划: %s" % scheme)
    if not scheme:
        return 1

    for name, guid in KNOBS.items():
        ac = pp.read_value(scheme, SUB, guid, False)
        dc = pp.read_value(scheme, SUB, guid, True)
        extra = ""
        if name == "PERFBOOSTMODE" and ac is not None:
            extra = " (%s)" % BOOST_CN.get(ac, "?")
        print("%-16s AC=%-8s DC=%-8s%s" % (name, ac, dc, extra))

    # ---- 试写 PERFBOOSTMODE ----
    g = KNOBS["PERFBOOSTMODE"]
    orig_ac = pp.read_value(scheme, SUB, g, False)
    orig_dc = pp.read_value(scheme, SUB, g, True)
    print("\n--- 试写 PERFBOOSTMODE ---  原值 AC=%s DC=%s" % (orig_ac, orig_dc))
    for trial in (2, 0):
        ok = pp.write_value(scheme, SUB, g, trial, False)
        time.sleep(0.2)
        back = pp.read_value(scheme, SUB, g, False)
        print("  写 %d -> 回读 %s  %s" % (trial, back, "✔ 可写" if back == trial else "✘ 未生效"))
    if orig_ac is not None:
        pp.write_value(scheme, SUB, g, orig_ac, False)
        time.sleep(0.2)
        now = pp.read_value(scheme, SUB, g, False)
        print("  还原 -> %s %s" % (now, "✔" if now == orig_ac else "✘ 还原失败！请手动改回 %s" % orig_ac))
    if orig_dc is not None:
        pp.write_value(scheme, SUB, g, orig_dc, True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
