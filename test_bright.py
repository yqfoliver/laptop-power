# -*- coding: utf-8 -*-
"""亮度优化策略测试：封顶/空闲调暗/用户覆盖/插电还原/游戏不干预，全部用假控制器。"""
import sys

sys.path.insert(0, ".")
from lp import config, hw  # noqa: E402
from lp.manager import Manager  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, got, want):
    global PASSED, FAILED
    ok = got == want
    PASSED, FAILED = PASSED + (1 if ok else 0), FAILED + (0 if ok else 1)
    print("[%s] %s got=%s want=%s" % ("PASS" if ok else "FAIL", name, got, want))


class FakeBright:
    def __init__(self, dc=60):
        self.available = True
        self.dc = dc
        self.ac = 90
        self.applied = []          # 记录 apply 调用
        self.idle = 0.0

    def read(self):
        return (self.ac, self.dc)

    def apply(self, ac=None, dc=None):
        if ac is not None:
            self.ac = ac
        if dc is not None:
            self.dc = dc
        self.applied.append((ac, dc))
        return True

    def idle_seconds(self):
        return self.idle


def mk(br, current="office", cfg_over=None, fullscreen=False):
    m = Manager(config.load())
    m.bright = br
    m.current = current
    m._br_was_ac = None
    m._br_applied = None
    m._br_user_cap = None
    m._br_dimmed = False
    m._br_base = None
    m._br_n = 0
    m._br_fail_at = 0.0
    # 亮度 action 现在要求「活动计划 = 自建计划」才动手：测试里直接放行，
    # 免得结果跟着本机当前电源计划变
    import time as _t
    m._sch_ok_at = _t.time()
    m._sch_ok_val = True
    m.cfg.update(cfg_over or {})
    hw.foreground = lambda: {"process": "x.exe", "title": "t", "fullscreen": fullscreen}
    return m


print("---- 离电封顶 ----")
br = FakeBright(dc=60)
m = mk(br)
st = {"ac": False, "battery_percent": 50}
m._brightness_tick(dict(st))    # 基线：60 > 45 -> 压到 45
check("离电首查：60 封顶到 45", br.dc, 45)
check("记账 applied", m._br_applied, 45)
m._brightness_tick(dict(st))
check("已封顶不重复写", len(br.applied), 1)

print("---- 用户手动改亮度 -> 尊重 ----")
br.dc = 80                       # 用户在系统里改亮
m._br_n = 11                     # 下一次 tick 计数到 12 -> 触发低频轮询
m._brightness_tick(dict(st))
check("采纳用户值 80", m._br_user_cap, 80)
m._brightness_tick(dict(st))
check("不再往回压", br.dc, 80)

print("---- 空闲调暗与恢复 ----")
br2 = FakeBright(dc=40)
m2 = mk(br2)
st2 = {"ac": False, "battery_percent": 50}
m2._brightness_tick(dict(st2))   # 基线 40 <= 45 只记账
check("基线记账 40", m2._br_applied, 40)
br2.idle = 120                   # 空闲 2 分钟
m2._brightness_tick(dict(st2))
check("空闲 90s+ 调暗到 20", br2.dc, 20)
check("调暗标记", m2._br_dimmed, True)
br2.idle = 1.0                   # 用户回来
m2._brightness_tick(dict(st2))
check("输入恢复到基准 40", br2.dc, 40)
check("调暗标记清除", m2._br_dimmed, False)

print("---- 游戏档/全屏不调暗 ----")
br3 = FakeBright(dc=45)
m3 = mk(br3, current="gaming")
st3 = {"ac": False, "battery_percent": 50}
m3._brightness_tick(dict(st3))
br3.idle = 300
m3._brightness_tick(dict(st3))
check("游戏档空闲不调暗", br3.dc, 45)

br4 = FakeBright(dc=45)
m4 = mk(br4, fullscreen=True)
st4 = {"ac": False, "battery_percent": 50}
m4._brightness_tick(dict(st4))
br4.idle = 300
m4._brightness_tick(dict(st4))
check("全屏（看视频）不调暗", br4.dc, 45)

print("---- 插电还原 ----")
br5 = FakeBright(dc=45)
m5 = mk(br5, cfg_over={"dc_brightness_cap": 45})
st5 = {"ac": False, "battery_percent": 50}
m5._brightness_tick(dict(st5))       # 建立基线
m5.current = "office"
st6 = {"ac": True, "battery_percent": 100}
m5._br_was_ac = False
m5._brightness_tick(dict(st6))       # 回插电
check("插电还原 AC 亮度 90", br5.ac, 90)
check("状态清零", (m5._br_applied, m5._br_dimmed), (None, False))

print("---- 切档重新基线 ----")
br6 = FakeBright(dc=60)
m6 = mk(br6)
m6._brightness_tick(dict(st))
m6.apply("battery", dry=True)        # dry 只预览，不提交，状态应保持
check("dry 不动状态", m6._br_applied, 45)
m6._br_reset()                        # 真实切档会走这里（apply 提交路径）
check("切档后 applied 清零", m6._br_applied, None)

print("=" * 50)
print("结果: %d 项通过, %d 项失败" % (PASSED, FAILED))
sys.exit(1 if FAILED else 0)
