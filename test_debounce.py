# -*- coding: utf-8 -*-
"""lp/manager.py 电源事件防抖（_stable_ac，借鉴 GHelper charger_delay）单元测试。

不依赖真实硬件：直接构造 Manager 后只调用纯函数式的 _stable_ac。
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lp.manager import Manager
from lp import config

PASS = 0


def ok(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1
    print("  ok -", msg)


def mk() -> Manager:
    cfg = config.load()
    m = Manager(cfg)
    m._ac_stable = None
    m._ac_candidate = None
    m._ac_changed_at = 0.0
    m._ac_unstable = False
    m._ac_settle_s = 5.0
    return m


def test_first_read_sets_stable():
    print("== 首次读数即稳定值 ==")
    m = mk()
    ok(m._stable_ac(True) is True, "首次 raw=True → 返回 True 并记为稳定")
    ok(m._stable_ac(True) is True, "再次 True 仍 True")
    ok(m._ac_unstable is False, "无跳变 → 不稳定标志为 False")


def test_flicker_filtered():
    print("== 单次抖动被滤掉 ==")
    m = mk()
    m._stable_ac(True)                 # 基线：插电
    v1 = m._stable_ac(False)           # 抖动到离电（候选，未确认）
    ok(v1 is True, "抖动瞬间仍返回旧稳定值（不承认跳变）")
    ok(m._ac_unstable is True, "跳变待确认 → 标 unstable")
    v2 = m._stable_ac(True)            # 抖回插电（settle 前变回 → 清零候选）
    ok(v2 is True, "抖动消失 → 不承认任何跳变")


def test_real_change_committed():
    print("== 持续变化在 settle 后承认 ==")
    m = mk()
    m._ac_settle_s = 0.0               # 立即确认（测试用）
    ok(m._stable_ac(False) is False, "settle=0 时新值立即生效")
    ok(m._stable_ac(False) is False, "保持 False")
    ok(m._stable_ac(True) is True, "再切回 True 立即生效")


def test_settle_window():
    print("== settle 时间窗 ==")
    m = mk()
    m._stable_ac(True)
    m._ac_settle_s = 1.0
    t0 = 1000.0
    ok(m._stable_ac(False, now=t0) is True, "t0：候选出现，未达窗口 → 维持旧值")
    ok(m._ac_unstable is True, "t0：不稳定标志置位")
    ok(m._stable_ac(False, now=t0 + 0.5) is True, "t0+0.5：仍未达窗口")
    ok(m._stable_ac(False, now=t0 + 1.5) is False, "t0+1.5：达窗口 → 承认跳变")
    ok(m._ac_unstable is False, "承认后不稳定标志清除")
    # 稳定后继续返回新值
    ok(m._stable_ac(False, now=t0 + 2.0) is False, "跳变后持续返回新稳定值")


def test_sustained_flap_keeps_old():
    print("== 持续抖动（来回切）不承认 ==")
    m = mk()
    m._stable_ac(True)
    m._ac_settle_s = 5.0
    # 每 1 秒在 离电/插电 间翻转（真实接触抖动）：回到稳定值时清空候选，
    # 候选永达不到 5s 同值 → 始终维持旧值 True，不承认任何抖动。
    for i in range(20):
        raw = (i % 2 == 1)            # 偶数拍离电（候选），奇数拍插电（回到稳定值→清空候选）
        v = m._stable_ac(bool(raw), now=2000.0 + i * 1.0)
        ok(v is True, "第 %d 拍翻转仍维持旧值" % (i + 1))
    # 中途任意候选待确认步都应标 unstable
    m2 = mk()
    m2._stable_ac(True)
    m2._ac_settle_s = 5.0
    m2._stable_ac(False, now=3000.0)
    ok(m2._ac_unstable is True, "候选待确认期间 unstable=True")


def test_pd_waits_out_settle_window():
    print("== 防抖窗口内 PD 控制器不动手（否则每次拔电都闪一下屏） ==")
    m = mk()
    m._stable_ac(True)                  # 基线：插电
    m._stable_ac(False)                 # 刚拔：候选，尚未确认 → ac 仍是 True
    ok(m._ac_stable is True and m._ac_unstable is True, "处于待确认窗口")

    class FakePd:
        enabled = True
        active = False
        predict_armed = False
        cpu_cap = 100

        def __init__(self):
            self.calls = []

        def feed(self, *a, **kw):
            self.calls.append((a, kw))
            return None

    old_pd, old_src = m.pd, m.pdsrc
    m.pd = FakePd()
    m.pdsrc = None
    try:
        m._pd_tick({"ac": True}, {})
        ok(m.pd.calls == [],
           "窗口内不调 pd.feed（否则「拔电」会被当成「PD 供不上」→ 降刷）")
        # 窗口结束、确认离电后：走还原路径，不进压制
        applied = []
        m._pd_apply = lambda req: applied.append(req)     # 别真去写电源计划
        m.pd.active = True
        m._stable_ac(False, now=time.monotonic() + 10)
        m._pd_tick({"ac": False}, {})
        ok([c[0][0] for c in m.pd.calls] == [False],
           "确认离电后只做还原（feed(False)），不进压制: %r" % (m.pd.calls,))
        ok(applied == [{"reset": True}], "并执行一次性还原: %r" % (applied,))
    finally:
        m.pd, m.pdsrc = old_pd, old_src


if __name__ == "__main__":
    test_first_read_sets_stable()
    test_flicker_filtered()
    test_real_change_committed()
    test_settle_window()
    test_sustained_flap_keeps_old()
    test_pd_waits_out_settle_window()
    print("\n全部通过：%d 项" % PASS)
