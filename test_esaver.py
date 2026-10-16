# -*- coding: utf-8 -*-
"""Windows 节能模式让位回归测试（2026-10-17）

覆盖：
1) _energy_saver 判定（插电不让位 / 离电+最佳能效才让位 / ov=None 不让位）
2) _esaver_tick 让位与回收的写值（进入=100；退出=按 AC/DC 两侧分别补写）
3) esaver_defer=false 时不切换让位
4) apply(dry) 预览期间让位 -> proc_max 报告为 100
5) _ppm_watchdog 让位期间解除武装（不自愈）
6) status() 暴露 energy_saver / overlay 两个字段
全部不碰真实电源计划：pc.* 与 set_proc_max 均以记录型替身替换。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import config, powercfgctl as pc
from lp import powrprof as pp
from lp.manager import Manager

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += (not ok)
    print("[%s] %-42s got=%s want=%s" % ("PASS" if ok else "FAIL", name, got, want))


SILENT = pp.OVERLAY_SILENT           # 961cc777-...（最佳能效）
BALANCED = pp.OVERLAY_BALANCED       # 全零（平衡）


class Rec:
    """记录型替身：把所有调用按顺序记下来，供断言"""

    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self


def fresh(esaver_on_ac=True, ov=SILENT, defer=True):
    """造一个 Manager，并把电源/叠加层读取全部替换成可控替身"""
    m = Manager(config.load())
    m.cfg["esaver_defer"] = defer
    m.cfg["learn_enabled"] = False           # 屏蔽自学习覆盖，值只剩档位表
    m.current = "battery"                    # 续航档：AC=100 / DC=30
    m._emit = lambda: None
    m._ov = ov
    m._ov_at = 1e18                          # 缓存永新鲜：_overlay_guid 不再翻 API
    rec = Rec()
    m.set_proc_max = rec                     # 记录 set_proc_max(v)
    return m, rec


def st(ac):
    return {"ac": ac}


print("---- 1) _energy_saver 判定 ----")
m, _ = fresh()
check("离电+最佳能效 -> 让位", m._energy_saver(st(False)), True)
check("插电+最佳能效 -> 不让位（用户主动）", m._energy_saver(st(True)), False)
m2, _ = fresh(ov=BALANCED)
check("离电+平衡 -> 不让位", m2._energy_saver(st(False)), False)
m3, _ = fresh(ov=None)
check("离电+读不到叠加层 -> 不让位（宁保守）", m3._energy_saver(st(False)), False)

print("---- 2) 让位进入：放开到 100 ----")
m, rec = fresh()
m._esaver_on = False
m._esaver_tick(st(False))
check("_esaver_on", m._esaver_on, True)
check("写入值（两侧同值放开）", [c[0][0] for c in rec.calls], [100])
# 幂等：第二次进来不再写
m._esaver_tick(st(False))
check("重复 tick 不重复写", [c[0][0] for c in rec.calls], [100])
check("原因文本", "Windows 节能模式接管" in m.last_reason, True)

print("---- 3) 退出让位：按 AC/DC 两侧分别补写 ----")
m, rec = fresh()
m._esaver_on = True                          # 预先处于让位中
m._ov = BALANCED                             # Windows 退出节能：叠加层变了
m._ov_at = 1e18
m._esaver_tick(st(False))                    # 仍在离电 -> 检测到退出 -> 补写
check("_esaver_on 复位", m._esaver_on, False)

# _esaver_restore 需要真实 plan 结构：以最小假 setting 验证两侧取值分化
m.cfg["custom_scheme"] = "G"                  # 生产里非 balanced 档必然已建计划


class FakeSet:
    def __init__(self, dc=0, ac=0):
        self.dc, self.ac = dc, ac
        self.vmax = self.vmin = None

    def current(self, on_bat):
        return self.dc if on_bat else self.ac


writes = []
fake = FakeSet(dc=50, ac=50)               # 让位前两侧都是错的（证明两侧独立取不同值）
m._plan = lambda s: types.SimpleNamespace(
    get=lambda k=None: fake if "PROCTHROTTLEMAX" in (k or "PROCTHROTTLEMAX") else None)
pc.set_value = lambda sch, s, v, on_bat: (writes.append((on_bat, v)), True)[1]
pc.list_schemes = lambda: {"G": object()}
m._esaver_restore()
check("补写两侧（DC=30, AC=100）", sorted(writes), sorted([(True, 30), (False, 100)]))
check("假 setting 同步刷新", (fake.dc, fake.ac), (30, 100))

# 值已正确时不写（避免每轮白写）
writes.clear()
m._esaver_restore()
check("值正确则不重写", writes, [])

print("---- 4) esaver_defer=false：不切换让位 ----")
m, rec = fresh(defer=False)
m._esaver_on = False
m._esaver_tick(st(False))
check("不让位", m._esaver_on, False)
check("不写任何值", [c[0][0] for c in rec.calls], [])

print("---- 5) apply(dry) 预览：让位期 proc_max 报 100 ----")
m, _ = fresh(defer=True)
m._esaver_on = True

class FakeSet2(FakeSet):
    def __init__(self):
        FakeSet.__init__(self, dc=50, ac=50)


m.ensure_scheme = lambda: "G"
m._plan = lambda s: types.SimpleNamespace(get=lambda k=None: FakeSet2())
m.learner.knob_value = lambda n, k, v: v        # 双保险：屏蔽自学习
m.thermal.apply_to_knobs = lambda *a: None
r = m.apply("battery", dry=True)                # 续航档 DC=30/AC=100
line = [c for c in r["changed"] if "CPU 最高频率" in c]
check("预览报告 proc_max=100（两侧）", len(line), 2)
check("另一侧也应为 100", all("→100" in c for c in line), True)

print("---- 6) 看门狗让位期解除武装 ----")
m, _ = fresh()
m._esaver_on = True
m._power_snap = lambda **k: {"cpu_util": 90.0, "cpu_perf_pct": 30.0}  # 双低=像卡死
heal = Rec()
m._ppm_heal = heal
m._ppm_watchdog(st(False))
check("不自愈", len(heal.calls), 0)
check("stall 计数清零", m._stall_n, 0)

print("---- 7) status() 新字段 ----")
from lp import hw


def stub_status_env(m):
    """让 status() 在离电 + 最佳能效下跑通（假 plan/假前台）"""
    hw.power_status, hw.foreground = (lambda: {"ac": False,
                                               "battery_percent": 40,
                                               "charging": False,
                                               "has_battery": True},
                                      lambda: {"process": "msedge.exe",
                                               "title": "t",
                                               "fullscreen": False})
    m.hw_report = lambda: {}
    m.alloc_report = lambda: {}
    m.battery_report = lambda: {}
    m.pd_report = lambda: {}
    m.gpupick_report = lambda: {}
    m.clamshell_report = lambda: {}

    class FakeKnobSet:
        options = {}

        def current(self, on_bat):
            return 100

    m._plan = lambda s: types.SimpleNamespace(get=lambda k=None: FakeKnobSet())


m, _ = fresh()
stub_status_env(m)
m._ov = SILENT
stt = m.status()
check("energy_saver=系统状态（直读）", stt["energy_saver"], True)
check("overlay 标签", stt["overlay"], "最佳能效")
m2, _ = fresh(ov=BALANCED)
stub_status_env(m2)
stt2 = m2.status()
check("平衡档时 energy_saver=False", stt2["energy_saver"], False)
check("平衡档 overlay 标签", stt2["overlay"], "平衡")

print("\n==== ESaver: %d passed, %d failed ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
