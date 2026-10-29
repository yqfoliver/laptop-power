# -*- coding: utf-8 -*-
"""散热策略 / ATKACPI 封装的离线回归测试（全部用假通道，绝不碰真电源计划）"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import thermal as T                      # noqa: E402
from lp import atkacpi as A                      # noqa: E402

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK ] %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


# --------------------------------------------------------------- 假 powrprof
class FakePP:
    """记录写入，返回可控的当前值"""
    def __init__(self, init):
        self.cur = dict(init)
        self.saved = {}
        self.written = []
        self.restored = []
        self.active_calls = 0
        self.fail = set()          # 这些属性写入返回失败（模拟通道忙/被拒）

    def read_attr(self, scheme, attr, on_battery=False):
        return self.cur.get(attr)

    def write_range(self, scheme, attr, value, on_battery=False):
        if attr in self.fail:
            return False
        self.written.append((attr, value))
        self.cur[attr] = value
        return True

    def write_attr(self, scheme, attr, value, on_battery=False):
        return self.write_range(scheme, attr, value, on_battery)

    def set_active(self, scheme):
        self.active_calls += 1
        return True

    def available(self):
        return True


SCHEME = "684f7fa1-32d5-41ed-b743-f71f619fb2d0"
ORIG = {"SYSCOOLPOL": 1, "PERFBOOSTMODE": 2, "PERFEPP": 20}


def make(mode, init=None):
    cfg = {"thermal_mode": mode}
    t = T.ThermalPolicy(cfg)
    t.scheme = SCHEME
    fake = FakePP(init or ORIG)
    t.pp = fake                     # 注入假通道
    t._pp_ok = True
    return t, fake


print("== 1) 模式与参数 ==")
for m in T.ORDER:
    t, _ = make(m)
    ok("模式 %s 合法" % m, t.mode == m and t.label != "")
t, _ = make("不存在的档位")
ok("非法模式回退 auto", t.mode == "auto")
q, _ = make("quiet")
ok("静音档封顶 CPU 上限", q.cpu_cap_ac == 75)
ok("静音档有 3 个硬件旋钮", set(q.hw_values) == {"SYSCOOLPOL", "PERFBOOSTMODE", "PERFEPP"})
ok("静音档散热=被动", q.hw_values.get("SYSCOOLPOL") == 0)
ok("静音档睿频收敛", q.hw_values.get("PERFBOOSTMODE") in (0, 1, 3, 4))
p, _ = make("perf")
ok("性能档散热=主动", p.hw_values.get("SYSCOOLPOL") == 1)
ok("性能档不封顶 CPU", p.cpu_cap_ac is None)
a, _ = make("auto")
ok("自动档不接管硬件", a.hw_values == {} and a.atk_mode is None)

print("== 2) hw_apply 写入与记忆 ==")
q, fake = make("quiet")
note = q.hw_apply(SCHEME)
ok("静音档写入 3 项", len(fake.written) == 3, str(fake.written))
ok("记录了接管前原值", q._hw_saved == ORIG, str(q._hw_saved))
ok("已应用集合正确", q._hw_applied == q.hw_values)
ok("返回说明提到策略", "静音" in (note or ""))
n2 = q.hw_apply(SCHEME)
ok("幂等：第二次不再写", len(fake.written) == 3 and n2 == "")

print("== 3) hw_restore 还原 ==")
q.hw_restore()
ok("还原写了 3 项", len(fake.written) == 6, str(fake.written))
ok("值回到原值", all(fake.cur[k] == v for k, v in ORIG.items()))
ok("原值表清空", q._hw_saved == {})
n3 = q.hw_restore()
ok("重复还原不写", len(fake.written) == 6)

print("== 4) auto 模式 ==")
a, fake = make("auto")
fake.cur = dict(ORIG)
n = a.hw_apply(SCHEME)
ok("自动档不写任何值", fake.written == [] and n == "")
# 先用 quiet 接管再切 auto -> 应还原
q2, fake2 = make("quiet")
q2.hw_apply(SCHEME)
q2.set_mode("auto")
q2._load()
n = q2.hw_apply(SCHEME)
ok("切回 auto 后还原原值", all(fake2.cur[k] == v for k, v in ORIG.items()), str(fake2.cur))

print("== 5) 部分接管（只 2 项可用） ==")
q3, fake3 = make("quiet")
fake3.cur = {"SYSCOOLPOL": 1, "PERFBOOSTMODE": 2}     # 缺 PERFEPP
q3.hw_apply(SCHEME)
ok("只写存在的项", all(a != "PERFEPP" for a, _ in fake3.written), str(fake3.written))
ok("原值只记存在的项", "PERFEPP" not in q3._hw_saved)
q3.hw_restore()
ok("还原不碰不存在的项", "PERFEPP" not in fake3.cur)

print("== 6) apply_to_knobs 封顶钩子 ==")
q4, _ = make("quiet")
ok("静音+插电+非游戏档封顶", q4.apply_to_knobs("office", True, "proc_max", 100) == 75)
ok("游戏档不封顶", q4.apply_to_knobs("gaming", True, "proc_max", 100) == 100)
ok("平衡档不封顶", q4.apply_to_knobs("balanced", True, "proc_max", 100) == 100)
ok("离电不封顶", q4.apply_to_knobs("office", False, "proc_max", 100) == 100)
ok("其它旋钮不封顶", q4.apply_to_knobs("office", True, "proc_min", 100) == 100)
ok("低于上限不抬高", q4.apply_to_knobs("office", True, "proc_max", 50) == 50)
p4, _ = make("perf")
ok("性能档不封顶", p4.apply_to_knobs("office", True, "proc_max", 100) == 100)

print("== 7) set_mode / reload ==")
q5, fake5 = make("quiet")
q5.hw_apply(SCHEME)
ok("set_mode 校验非法值", q5.set_mode("xxx") is False)
ok("set_mode 接受合法值", q5.set_mode("perf") is True and q5.mode == "perf")
ok("cfg 同步", q5.cfg.get("thermal_mode") == "perf")
q5.hw_apply(SCHEME)
ok("切换后写的是新目标", (fake5.cur.get("SYSCOOLPOL") == 1), str(fake5.cur))
ok("下次还原回到 auto 原值", q5.hw_restore() and fake5.cur["SYSCOOLPOL"] == 1)

print("== 8) ATKACPI 纯逻辑 ==")
c = bytes([35, 50, 55, 60, 65, 70, 75, 99] + [7, 16, 28, 37, 48, 55, 64, 78])
temps, fans = A.AtkAcpi.curve_points(c)
ok("曲线拆包", temps[0] == 35 and fans[0] == 7 and len(temps) == 8)
ok("曲线文本", "35℃→7%" in A.AtkAcpi.curve_text(c))
ok("空曲线文本", A.AtkAcpi.curve_text(None) == "—")
ok("DEV 表关键项", A.DEV["perf_mode"] == 0x00120075 and A.DEV["cpu_temp"] == 0x00120094)
ok("性能模式枚举", A.PERF_LABEL[2] == "静音" and A.PERF_LABEL[1] == "增强")

print("== 9) 还原失败必须能重试（第六轮审查） ==")
# 现场：静音档接管后切回 auto，其中一项写失败（通道忙）—— 那一项的原值留在
# _hw_saved，而 _hw_applied 被清空。旧代码用「目标 == 已应用」提前返回，
# 两个空集合相等 ⇒ 永久挡住重试，旋钮停在静音档的值上，界面却显示「自动」。
q6, fake6 = make("quiet")
q6.hw_apply(SCHEME)
q6.set_mode("auto")
fake6.fail = {"PERFEPP"}                 # 这一项先写不进去
q6.hw_apply(SCHEME)
ok("写失败的那项仍留在原值表", q6._hw_saved == {"PERFEPP": ORIG["PERFEPP"]},
   str(q6._hw_saved))
ok("失败时确实没还原", fake6.cur["PERFEPP"] != ORIG["PERFEPP"])
fake6.fail = set()                       # 通道恢复
q6.hw_apply(SCHEME)                      # 下一轮巡检（不会再有人手动点一次）
ok("通道恢复后自动补还原", fake6.cur["PERFEPP"] == ORIG["PERFEPP"],
   str(fake6.cur))
ok("补还原后原值表清空", q6._hw_saved == {})
ok("auto 且无可还原项时返回空串", q6.hw_apply(SCHEME) == "")

print("== 10) 原值落盘：强杀后不能把静音值当原值 ==")
# 现场：静音档接管 → 进程被强杀 → 计划里 knobs 还是静音值。重启后再接管，
# 旧代码 read_attr 读到的是我们自己写的值，于是把它当「用户原值」记下来，
# 用户真正的设置永久丢失（且全程无声）。
_p1 = tempfile.mktemp(suffix=".json", prefix="lp_th_")
q7 = T.ThermalPolicy({"thermal_mode": "quiet"}, data_path=_p1)
fake7 = FakePP(ORIG)
q7.pp = fake7
q7.scheme = SCHEME
q7.hw_apply(SCHEME)
ok("接管时把原值落盘", os.path.isfile(_p1))
q8 = T.ThermalPolicy({"thermal_mode": "quiet"}, data_path=_p1)
fake8 = FakePP(fake7.cur)                 # 此时计划里全是静音档的值
q8.pp = fake8
q8.scheme = SCHEME
q8.hw_apply(SCHEME)
ok("新进程认领回真正的原值（不是静音值）", q8._hw_saved == ORIG, str(q8._hw_saved))
q8.hw_restore()
ok("还原到真正的原值", fake8.cur == ORIG, str(fake8.cur))
ok("还原后原值表清空", q8._hw_saved == {})

_p2 = tempfile.mktemp(suffix=".json", prefix="lp_th_")
q9 = T.ThermalPolicy({"thermal_mode": "quiet"}, data_path=_p2)
q9.pp = FakePP(ORIG)
q9.scheme = SCHEME
q9.hw_apply(SCHEME)
q10 = T.ThermalPolicy({"thermal_mode": "auto"}, data_path=_p2)
fake10 = FakePP(ORIG)
q10.pp = fake10
q10.hw_apply("99999999-0000-0000-0000-000000000000")   # 换过计划
ok("计划 GUID 对不上就不认领旧原值（不拿别的计划的数去覆盖）",
   fake10.written == [] and q10._hw_saved == {}, str(fake10.written))

q11 = T.ThermalPolicy({"thermal_mode": "quiet"})       # 不给 data_path
fake11 = FakePP(ORIG)
q11.pp = fake11
q11.scheme = SCHEME
q11.hw_apply(SCHEME)
ok("不传 data_path 时纯内存，不落盘",
   q11._hw_saved == ORIG and fake11.cur != ORIG)
for _p in (_p1, _p2):
    try:
        os.unlink(_p)
    except Exception:
        pass

print("== 11) 硬件旋钮范围保护（若 powrprof 可用） ==")
if T.pp.available():
    ok("越界 PROCTHROTTLEMAX 拒绝", T.pp.write_range(SCHEME, "PROCTHROTTLEMAX", 300) is False)
    ok("越界 PERFBOOSTMODE 拒绝", T.pp.write_range(SCHEME, "PERFBOOSTMODE", 9) is False)
    ok("越界 SYSCOOLPOL 拒绝", T.pp.write_range(SCHEME, "SYSCOOLPOL", 5) is False)
    ok("越界 PERFEPP 拒绝", T.pp.write_range(SCHEME, "PERFEPP", 200) is False)
else:
    print("  (powrprof 不可用，跳过真机范围校验)")

print()
print("结果: %d 通过, %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
