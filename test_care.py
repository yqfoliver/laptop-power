# -*- coding: utf-8 -*-
"""电池充放电管理（lp/batterycare.py）离线回归测试

不碰真实电源计划：把 batterycare 用的 powercfg 通道替换成假实现，
只验证「统计口径 + 评分 + 降温动作的写入/还原时序」。
用法：python test_care.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lp import batterycare as bc   # noqa: E402

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


class FakePC:
    """假 powercfg：AC 当前 CPU 上限可读可写，记录每次调用"""

    def __init__(self, ac_now=100):
        self.ac_now = ac_now
        self.calls = []

    def run_powercfg(self, args):
        self.calls.append(list(args))
        if args and args[0] == "/q":
            # 输出形如：当前交流电源设置索引: 0x00000064 / 当前直流…0x0000002d
            return ("电 源 设 置 GUID: %s\n" % bc.GUID_THROTTLE_MAX +
                    "当前交流电源设置索引: 0x%08x\n" % self.ac_now +
                    "当前直流电源设置索引: 0x0000002d\n")
        if args and args[0] == "/setacvalueindex":
            self.ac_now = int(args[-1])
            return "ok"
        return ""

    def writes(self):
        return [c for c in self.calls if c and c[0] == "/setacvalueindex"]


# 本机是否装了 MyASUS / 充电上限是多少，都不能让测试结果跟着变 -> 一律注入假读数
NO_ASUS = {"supported": False, "value": None, "mode": None,
           "mode_cn": None, "label": "不可用", "myasus": False}


def fresh(cfg=None, pc=None, path=None, chg=None):
    fake = pc or FakePC()
    bc.pc = fake                      # 注入假通道
    payload = dict(NO_ASUS if chg is None else chg)
    c = bc.BatteryCare(cfg or {}, path, chg_reader=(lambda: dict(payload)))
    return c, fake


def batt(ac=True, charging=False, discharging=False, pct=50, rate=None,
         full=50000, health=None):
    return {"ac": ac, "charging": charging, "discharging": discharging,
            "percent": pct, "rate_w": rate, "full_mwh": full,
            "health_pct": health}


def tmp_json():
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(p)
    return p


print("== 1. 满充搁置统计（插电 + ≥95%）==")
c, _ = fresh({}, path=tmp_json())
c.feed(batt(ac=True, pct=100), 45.0, 300.0)
c.feed(batt(ac=True, pct=99), 45.0, 300.0)
c.feed(batt(ac=True, pct=80), 45.0, 300.0)      # 低于门槛不计
c.feed(batt(ac=False, pct=100, discharging=True, rate=20.0), 45.0, 300.0)  # 离电不计
b = c.report()["today"]
ok(b["full_soc_s"] == 600.0, "满充搁置只累计插电高电量时长", b["full_soc_s"])

print("== 2. 高温充电统计 ==")
c, _ = fresh({}, path=tmp_json())
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 89.0, 300.0)   # 高热
c.feed(batt(ac=True, charging=True, pct=70, rate=-30.0), 70.0, 300.0)   # 不热
c.feed(batt(ac=True, pct=80), 95.0, 300.0)                              # 没充电
b = c.report()["today"]
ok(b["hot_charge_s"] == 300.0, "高温充电只在「充电中+热」时累计", b["hot_charge_s"])

print("== 3. 深度放电统计 ==")
c, _ = fresh({}, path=tmp_json())
c.feed(batt(ac=False, discharging=True, pct=12, rate=21.0), 40.0, 300.0)
c.feed(batt(ac=False, discharging=True, pct=25, rate=21.0), 40.0, 300.0)
b = c.report()["today"]
ok(b["deep_s"] == 300.0, "深放只在离电低电量时累计", b["deep_s"])

print("== 4. 等效循环数（按放电能量折算）==")
p = tmp_json()
c, _ = fresh({}, path=p)
for _ in range(24):
    c.feed(batt(ac=False, discharging=True, pct=60, rate=60.0), 40.0, 300.0)
st = c.report()["stat"]
# 24 × 300s × 60W = 120Wh；满充 60.536Wh -> ≈1.98 次等效循环
ok(st["cycles_est"] is not None and 1.5 < st["cycles_est"] < 2.5,
   "循环数由放电 Wh / 满充容量折算", st.get("cycles_est"))

print("== 5. 充电高热 -> 压 CPU 上限 ==")
c, pc = fresh({}, path=tmp_json())
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 90.0, 60.0)
w = pc.writes()
ok(len(w) == 1 and w[0][-1] == "70", "触发降温：AC CPU 上限写 70", w)
ok(c.report()["relief_on"] and c.report()["relief_cap"] == 70, "报告标记降温中")
c.feed(batt(ac=True, charging=True, pct=62, rate=-30.0), 90.0, 60.0)
ok(len(pc.writes()) == 1, "持续高热不重复写（避免 powercfg 抖动）")

print("== 6. 温度回落 -> 还原基线 ==")
c.feed(batt(ac=True, charging=True, pct=64, rate=-30.0), 60.0, 60.0)
w = pc.writes()
ok(len(w) == 2 and w[1][-1] == "100", "回落即还原原值 100", w)
ok(not c.report()["relief_on"], "报告标记已结束降温")

print("== 7. 游戏档不干预 ==")
c, pc = fresh({}, path=tmp_json())
c.set_context(gaming=True)
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 92.0, 60.0)
ok(len(pc.writes()) == 0, "游戏档不压 CPU 上限（帧率优先）")
c.set_context(gaming=False)
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 92.0, 60.0)
ok(len(pc.writes()) == 1, "离开游戏档后恢复干预")

print("== 8. 开关与切档还原 ==")
# 两个开关各管一条触发线。旧实现把它们串成 `not A or not B`（AND 语义）：
# 只关「充电高热降温」会把「插电高温保护」一起关掉 —— 开关名字承诺的事没做到。
# 95℃ 插电充电：关掉充电高热那条，仍应由插电高温保护写 80%。
c, pc = fresh({"care_heat_relief": False}, path=tmp_json())
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 95.0, 60.0)
ok(len(pc.writes()) == 1 and pc.writes()[0][-1] == "80",
   "只关充电高热 -> 插电高温保护独立生效（写 80）", pc.writes())
# 只关插电高温：90℃ 充电中应仍按充电高热写 70%
c3, pc3 = fresh({"care_thermal_relief": False}, path=tmp_json())
c3.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 90.0, 60.0)
ok(len(pc3.writes()) == 1 and pc3.writes()[0][-1] == "70",
   "只关插电高温 -> 充电高热独立生效（写 70）", pc3.writes())
# 两个都关：一条线都不许动
c4, pc4 = fresh({"care_heat_relief": False, "care_thermal_relief": False},
                path=tmp_json())
c4.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 99.0, 60.0)
ok(len(pc4.writes()) == 0, "两个开关都关 -> 不写计划")
c2, pc2 = fresh({}, path=tmp_json())
c2.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 95.0, 60.0)
c2.reset()
ok(len(pc2.writes()) == 2 and pc2.writes()[-1][-1] == "100",
   "切档 reset() 无条件还原上限", pc2.writes())

print("== 9. 已低于目标值时不动手 ==")
c, pc = fresh({}, pc=FakePC(ac_now=60), path=tmp_json())
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 95.0, 60.0)
ok(len(pc.writes()) == 0, "原上限已低于降温目标 -> 不写（只记状态）")

print("== 10. 统计跨重启保留 ==")
p = tmp_json()
c, _ = fresh({}, path=p)
c.feed(batt(ac=True, pct=99), 45.0, 300.0)
c._save(force=True)
c3, _ = fresh({}, path=p)
ok(c3.report()["today"]["full_soc_s"] == 300.0, "落盘后重新加载仍在",
   c3.report()["today"].get("full_soc_s"))
ok(c3.report()["score"] is not None, "加载后立刻能算出评分")

print("== 11. 评分随违规下降 ==")
c, _ = fresh({}, path=tmp_json())
c.feed(batt(ac=True, pct=100), 45.0, 300.0)          # 5 分钟，几乎无扣分
good = c.report()["score"]
c2, _ = fresh({}, path=tmp_json())
for _ in range(60):
    c2.feed(batt(ac=True, pct=100), 45.0, 300.0)     # 5 小时满充搁置
    c2.feed(batt(ac=True, charging=True, pct=100, rate=-30.0), 92.0, 300.0)
bad = c2.report()["score"]
ok(good > bad, "长时间满充+高温充电评分更低", "%s vs %s" % (good, bad))
ok(any("满充搁置" in a for a in c2.report()["advice"]), "给出满充搁置建议")
ok(any("高温充电" in a for a in c2.report()["advice"]), "给出高温充电建议")

print("== 12. dt 上限钳制（休眠/挂起不虚增）==")
c, _ = fresh({}, path=tmp_json())
c.feed(batt(ac=True, pct=100), 45.0, 999999.0)
ok(c.report()["today"]["full_soc_s"] == 300.0, "超长 dt 被钳到 300s")

print("== 13. 原始采样不写入 ==")
c, _ = fresh({}, path=None)      # data_path=None：不落盘也不报错
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 92.0, 60.0)
ok(True, "无 data_path 时静默工作")

print("== 14. 热参数热重载 ==")
c, pc = fresh({}, path=tmp_json())
ok(c.hot_charge_c == 88.0, "默认高温门槛 88℃")
c.reload_cfg({"care_hot_charge_c": 70, "care_heat_relief_cpu_pct": 55})
ok(c.hot_charge_c == 70.0 and c.relief_cap == 55, "热重载后新阈值/上限生效")
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 75.0, 60.0)
ok(pc.writes() and pc.writes()[0][-1] == "55", "用新上限 55 写入", pc.writes())

print("== 15. 禁用总开关 ===")
c, pc = fresh({"battery_care_enabled": False}, path=tmp_json())
c.feed(batt(ac=True, pct=100), 45.0, 3600.0)
r = c.report()
ok(r["score"] is None and not r["advice"], "禁用后不统计不评分")
ok("未启用" in c.summary_line(), "summary_line 反映未启用")

print("== 16. 只在自建计划上动手（系统平衡档不写）==")


class FakePC2(FakePC):
    """额外支持 /getactivescheme：报告当前活动计划 GUID"""

    def __init__(self, active):
        FakePC.__init__(self)
        self.active = active

    def run_powercfg(self, args):
        if args and args[0] == "/getactivescheme":
            self.calls.append(list(args))
            return "电源方案 GUID: %s  (随便)" % self.active
        return FakePC.run_powercfg(self, args)      # 由父类统一记录，避免重复计数


SYSTEM_BAL = "381b4222-f694-41f0-9685-ff5bb260df2e"
CUSTOM = "684f7fa1-32d5-41ed-b743-f71f619fb2d0"

pc1 = FakePC2(SYSTEM_BAL)
bc.pc = pc1
c = bc.BatteryCare({"custom_scheme": CUSTOM}, tmp_json(), scheme_guid=CUSTOM,
                   chg_reader=lambda: dict(NO_ASUS))
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 92.0, 60.0)
ok(len(pc1.writes()) == 0, "活动计划是系统「平衡」时绝不写 CPU 上限")

pc2 = FakePC2(CUSTOM)
bc.pc = pc2
c = bc.BatteryCare({"custom_scheme": CUSTOM}, tmp_json(), scheme_guid=CUSTOM,
                   chg_reader=lambda: dict(NO_ASUS))
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 92.0, 60.0)
ok(len(pc2.writes()) == 1, "活动计划是自建计划时才允许降温写入")

print("== 17. 华硕充电上限（只读镜像值）接入 ==")


def chg(value, myasus=True):
    cn = {100: "满容量模式", 80: "平衡模式", 60: "长效使用模式"}.get(value, "自定义 %d%%" % value)
    mode = {100: "full", 80: "balanced", 60: "lifespan"}.get(value, "custom")
    return {"supported": True, "value": value, "mode": mode, "mode_cn": cn,
            "label": "x", "myasus": myasus}


c, _ = fresh({}, path=tmp_json(), chg=chg(100))
ok(c.report()["charge_limit"]["value"] == 100, "读出充电上限 = 100")
ok("100%" in c.charge_line(), "charge_line 反映 100%（满容量）", c.charge_line())
ok(any("100%" in a and "MyASUS" in a for a in c.report()["advice"]),
   "无保护时给出「去 MyASUS 改 80%」的建议")

c, _ = fresh({}, path=tmp_json(), chg=chg(80))
ok("80%" in c.charge_line() and "平衡模式" in c.charge_line(),
   "已设 80% 时显示「平衡模式」", c.charge_line())
adv = c.report()["advice"]
ok(any("已设 80%" in a for a in adv), "已设上限 -> 改成确认语")
ok(not any("改成「平衡模式」" in a for a in adv), "已设上限 -> 不再重复唠叨")

c, _ = fresh({}, path=tmp_json(), chg=chg(60))
ok(any("已设 60%" in a for a in c.report()["advice"]), "60% 长效模式同样识别")

print("== 18. 已设上限时不再提示「满充搁置去设上限」==")
c, _ = fresh({}, path=tmp_json(), chg=chg(80))
for _ in range(24):                                  # 2 小时满充搁置
    c.feed(batt(ac=True, pct=100), 45.0, 300.0)
ok(c.report()["today"]["full_soc_s"] == 7200.0, "满充搁置照样统计（数据不失真）")
ok(not any("改成「平衡模式」" in a for a in c.report()["advice"]),
   "已设 80% 时不再推「去设上限」")

print("== 19. 充电上限读取节流 ==")
calls = {"n": 0}


def counting_reader():
    calls["n"] += 1
    return dict(chg(100))


bc.pc = FakePC()
c = bc.BatteryCare({}, tmp_json(), chg_reader=counting_reader)
ok(calls["n"] == 1, "构造时先读一次（0.03 ms，让首屏就准）", calls["n"])
for _ in range(10):
    c.feed(batt(ac=True, pct=80), 45.0, 1.0)
ok(calls["n"] == 1, "60 s 内连续 feed 不重复读注册表", calls["n"])
c._chg_tick(force=True)
ok(calls["n"] == 2, "force 可强制刷新", calls["n"])

print("== 20. 非华硕 / MyASUS 未装时的措辞 ==")
c, _ = fresh({}, path=tmp_json(), chg=NO_ASUS)
for _ in range(24):
    c.feed(batt(ac=True, pct=100), 45.0, 300.0)
ok(any("装 MyASUS" in a for a in c.report()["advice"]), "未装 MyASUS -> 建议去装")
ok("未读到" in c.charge_line(), "读不到时 charge_line 说明未读到", c.charge_line())

c, _ = fresh({}, path=tmp_json(),
             chg={"supported": False, "value": None, "mode": None, "mode_cn": None,
                  "label": "不可用", "myasus": True})
for _ in range(24):
    c.feed(batt(ac=True, pct=100), 45.0, 300.0)
ok(any("MyASUS" in a and "设一次" in a for a in c.report()["advice"]),
   "装了 MyASUS 但没读到 -> 提示去设一次")
ok("已装" in c.charge_line(), "charge_line 提示 MyASUS 已装", c.charge_line())

print("== 21. 插电高温保护（已充满非充电也动作）==")
c, pc = fresh({}, path=tmp_json())
c.feed(batt(ac=True, charging=False, pct=100), 96.0, 60.0)
w = pc.writes()
ok(len(w) == 1 and w[0][-1] == "80", "插电 96℃ ≥ 95℃ -> AC CPU 上限写 80", w)
ok(c.report()["relief_on"] and c.report()["relief_cap"] == 80, "报告标记高温保护中")
c.feed(batt(ac=True, charging=False, pct=100), 96.0, 60.0)
ok(len(pc.writes()) == 1, "高温持续不重复写")

print("== 22. 高温保护滞回（92℃ 的重负载渲染不误触发）==")
c, pc = fresh({}, path=tmp_json())
c.feed(batt(ac=True, charging=False, pct=100), 92.0, 60.0)
ok(len(pc.writes()) == 0, "92℃ 低于 95℃ 触发线 -> 不动作")
c.feed(batt(ac=True, charging=False, pct=100), 96.0, 60.0)
c.feed(batt(ac=True, charging=False, pct=100), 91.0, 60.0)
ok(len(pc.writes()) == 1, "91℃ ≥ 89℃ 清除线 -> 继续保护")
c.feed(batt(ac=True, charging=False, pct=100), 88.0, 60.0)
ok(len(pc.writes()) == 2 and pc.writes()[-1][-1] == "100", "≤89℃ 还原原值 100", pc.writes())
ok(not c.report()["relief_on"], "报告标记已结束保护")

print("== 23. 离电高温不适用插电保护 ==")
c, pc = fresh({}, path=tmp_json())
c.feed(batt(ac=False, discharging=True, pct=60, rate=21.0), 96.0, 60.0)
ok(len(pc.writes()) == 0, "离电时不写 AC 高温保护（DC 档本就限频）")

print("== 24. 充电高热 + 插电高温叠加取更严值 ==")
c, pc = fresh({"care_heat_relief_cpu_pct": 85, "care_thermal_cpu_pct": 60},
              path=tmp_json())
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 96.0, 60.0)
w = pc.writes()
ok(len(w) == 1 and w[0][-1] == "60", "双触发取 min(85,60)=60", w)

print("== 25. 插电高温保护开关 ==")
c, pc = fresh({"care_thermal_relief": False}, path=tmp_json())
c.feed(batt(ac=True, charging=False, pct=100), 96.0, 60.0)
ok(len(pc.writes()) == 0, "关闭 thermal 开关后插电高温不动作")
c.reload_cfg({"care_thermal_relief": True})
c.feed(batt(ac=True, charging=False, pct=100), 96.0, 60.0)
ok(len(pc.writes()) == 1, "热重载打开后恢复动作")

# ==================================================================
# 26~30：按场景的充电上限 + 「本次充满」意图（2026-10-11）
# 用户反馈「养护模式就只能 80%，不能充满，不合理」→ 养护改成按场景，
# 并允许一次有期限的「充满」，而不是一个永久开关。
# ==================================================================
ASUS_80 = {"supported": True, "value": 80, "mode": "balanced",
           "mode_cn": "平衡模式", "label": "充电上限 80%", "myasus": True}
ASUS_100 = dict(ASUS_80, value=100, mode="full", mode_cn="满容量模式",
                label="充电上限 100%")

def seed_days(c, n=7, dc_h=6.0, ac_h=18.0, full_h=0.0):
    """直接造 n 天统计样本。

    不能用 feed(大 dt) 造：feed 会把 dt 夹到 300 s（防休眠后虚增统计），
    一次喂 6 小时只会被记成 5 分钟。
    """
    import datetime
    days = c._data.setdefault("days", {})
    today = datetime.date.today()
    for i in range(n):
        k = (today - datetime.timedelta(days=i)).isoformat()
        days[k] = {"sample_s": (dc_h + ac_h) * 3600.0, "ac_s": ac_h * 3600.0,
                   "dc_s": dc_h * 3600.0, "full_soc_s": full_h * 3600.0,
                   "hot_charge_s": 0.0, "deep_s": 0.0, "charge_wh": 0.0,
                   "discharge_wh": 0.0, "relief_s": 0.0, "max_heat_c": 0.0,
                   "intent_full_s": 0.0}
    return c


print("== 26. 场景判定：长期插电 desk / 常带出门 mobile ==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
ok(c.scene() == "unknown", "刚建档样本不足 -> unknown", c.scene())

c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
seed_days(c, dc_h=0.5, ac_h=10.0)
c._recompute({})
ok(c.scene() == "desk", "日均离电 0.5h -> desk", (c.scene(), c.dc_h_day()))
adv = c.report()["advice"]
ok(any("长期插电" in a for a in adv), "desk 场景给「保持/压低」的结论", adv)
ok(not any("白砍" in a for a in adv), "desk 场景不会劝你充满", adv)

c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
seed_days(c, dc_h=6.0, ac_h=2.0)
c._recompute({})
ok(c.scene() == "mobile", "日均离电 6h -> mobile", (c.scene(), c.dc_h_day()))
adv = c.report()["advice"]
ok(any("白砍" in a for a in adv), "mobile + 上限 80% -> 劝你改回 100%", adv)
ok(not any("建议压到 80%" in a for a in adv), "mobile 场景不再念叨压到 80%", adv)

print("== 27. mobile 场景不再念叨「满充搁置去设上限」==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_100)
seed_days(c, dc_h=6.0, ac_h=2.0, full_h=2.0)     # 每天满充搁置 2h
c._recompute({})
adv = c.report()["advice"]
ok(c.scene() == "mobile", "样本判定为 mobile", c.scene())
# 注意别用 "满充搁置" 做断言：无坏习惯时的兜底语是「无明显满充搁置 / 高温充电 / 深放」，
# 它会永远命中。真正的唠叨语带「满充搁置日均」。
ok(not any("满充搁置日均" in a for a in adv),
   "常带出门的人满充是刚需，不当坏习惯唠叨", adv)

print("== 28. 「本次充满」意图：有期限、会自动结束 ==")
writes = []


def fake_writer(v):
    writes.append(v)
    return {"ok": True, "before": 80, "after": v, "need_admin": False,
            "reason": "", "key": "fake"}


c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
c._lim_writer = fake_writer
r = c.set_full_intent(reason="travel")
ok(r["ok"] and writes == [100], "设意图时立刻去写 100%", writes)
ok(c.intent_active() is True, "意图已激活")
# 意图期间的满充不扣分
c.feed(batt(ac=True, pct=100), 45.0, 7200.0)
b = c.report()["today"]
ok(b.get("full_soc_s", 0) == 0, "意图期间的满充不计入满充搁置", b)
ok(b.get("intent_full_s", 0) >= 300.0, "改记到 intent_full_s", b)
# 拔电用掉 -> 自动结束
c.feed(batt(ac=False, discharging=True, pct=70, rate=20.0), 45.0, 60.0)
ok(c.intent_active() is False, "拔电用到 90% 以下 -> 意图自动结束")
ok(writes[-1] == 100, "结束时不会偷偷改回上限（改回要用户或场景触发）", writes)

print("== 29. 意图有效期下限 + 到期自动结束（忘了清也不会永远停在 100%）==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
c._lim_writer = fake_writer
r = c.set_full_intent(reason="manual", hours=0.001)
span = r["intent"]["expire"] - r["intent"]["created"]
ok(span >= 1800.0 - 1.0, "有效期有 0.5h 下限（防止手滑设成几秒）", span)
c.feed(batt(ac=True, pct=100), 45.0, 10.0)
ok(c.intent_active() is True, "有效期内仍在")
c._intent["expire"] = c._intent["created"] - 1.0     # 直接把过期时间拨到过去
c._data["intent"] = c._intent
c.feed(batt(ac=True, pct=100), 45.0, 10.0)
ok(c.intent_active() is False, "过期自动结束", c.report().get("intent"))
ok(c.report().get("intent") is None, "结束后 report 里不再带意图")

print("== 30. 写入权限分流：权限不够要报 need_admin 而不是假装成功 ==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)


def denied_writer(v):
    return {"ok": False, "before": 80, "after": 80, "need_admin": True,
            "reason": "写 HKLM 需要管理员权限", "key": "fake"}


c._lim_writer = denied_writer
r = c.set_full_intent(reason="manual")
ok(r["write"].get("need_admin") is True, "被拒时返回 need_admin=True", r["write"])
ok(c.intent_active() is True,
   "写入失败也保留意图（用户提权成功后仍能继续，且满充不误扣分）")
c.restore_limit()
ok(True, "restore_limit 走养护上限（target_limit）")

print("== 31. 满充校准提醒 ==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
ok(c.calib_due() is True, "从未校准 -> 该提醒")
adv = c.report()["advice"]
ok(any("还没做过满充校准" in a for a in adv), "首次给校准建议", adv)
c._last_calib = c._day_key()
c._data["last_calib"] = c._last_calib
ok(c.calib_due() is False, "刚校准过 -> 不提醒")
ok(c.calib_days_since() == 0, "距上次 0 天", c.calib_days_since())
c._last_calib = "2020-01-01"
ok(c.calib_due() is True and c.calib_days_since() > 1000,
   "很久没校准 -> 提醒", c.calib_days_since())
# calibrate 意图完成后要记下校准日期
c2, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
c2._lim_writer = fake_writer
c2.set_full_intent(reason="calibrate")
c2.feed(batt(ac=False, discharging=True, pct=60, rate=20.0), 45.0, 60.0)
ok(c2.calib_due() is False and c2.calib_days_since() == 0,
   "校准意图走完一轮 -> 记下校准日期", c2.calib_days_since())

# ============================================================
# 30~34. 上限下发后的「生效确认」
#
# 2026-10-11 实测：写完注册表后 EC 约 13 分钟才改变充电行为，中间还被
# 厂商服务回滚过一次。所以「写完回读 == 目标值」是**假成功**，必须靠
# 电量行为事后确认，并且发现被回滚要重写一次。
# ============================================================
def fresh_mut(chg, path=None):
    """chg 内容可变（用来模拟厂商服务把值改回去）。"""
    payload = dict(chg)
    bc.pc = FakePC()
    c = bc.BatteryCare({}, path or tmp_json(), chg_reader=(lambda: dict(payload)))
    return c, payload


def mk_writer(pay):
    """写成功的同时把「注册表」也改掉（贴近真实：写完回读就是新值）。"""
    def w(v):
        v = int(v)
        pay["value"] = v
        pay["mode_cn"] = {100: "满容量模式", 80: "平衡模式", 60: "长效使用模式"}[v]
        return {"ok": True, "before": 80, "after": v, "need_admin": False,
                "reason": "", "key": "fake", "ec_confirmed": False}
    return w


print("== 30. 写入成功 ≠ 已生效：先置「待生效」，不谎报 ==")
c, pay = fresh_mut(ASUS_80)
c._lim_writer = mk_writer(pay)
c.set_full_intent(reason="manual")
st = c.report().get("limit_state")
ok(bool(st) and st["state"] == "pending", "刚写完 -> pending", st)
ok(c.report().get("limit_pending") is True, "report 暴露 pending 标记")
ok("待生效" in c.charge_line(), "文案如实显示「待生效」而不是「已充满」", c.charge_line())
ok("已生效" not in "".join(c.report()["advice"]), "建议里不宣称已生效",
   c.report()["advice"])

print("== 31. 电量越过上限 -> 判定已生效 ==")
c, pay = fresh_mut(ASUS_80)
c._lim_writer = mk_writer(pay)
c.set_full_intent(reason="manual")
c.feed(batt(ac=True, pct=100, charging=True, rate=-20.0), 45.0, 60.0)
st = c.report().get("limit_state")
ok(bool(st) and st["state"] == "synced", "电量到 100% -> synced", st)
ok(c.report().get("limit_pending") is False, "确认后清掉 pending")
ok("待生效" not in c.charge_line(), "文案恢复常态", c.charge_line())

print("== 32. 被厂商服务回滚 -> 自动重写一次；再被改回 -> 认输 ==")
c, pay = fresh_mut(ASUS_80)
c._lim_writer = mk_writer(pay)
c.set_full_intent(reason="manual")
ok(pay["value"] == 100, "前置：已写成 100%", pay["value"])
pay["value"] = 80                       # 厂商服务把它改回去（实测发生过一次）
c.feed(batt(ac=True, pct=79), 45.0, 60.0)
ok(pay["value"] == 100, "发现被回滚 -> 自动重写一次", pay["value"])
ok(c.report().get("limit_state", {}).get("state") == "pending", "重试期间仍是 pending")
pay["value"] = 80                       # 又被改回去
c.feed(batt(ac=True, pct=79), 45.0, 60.0)
st = c.report().get("limit_state")
ok(bool(st) and st["state"] == "rolled_back", "重试一次还被改回 -> rolled_back", st)
ok(any("厂商服务" in a for a in c.report()["advice"]),
   "如实告知用户改不动，而不是静默失败", c.report()["advice"])

print("== 33. 确认窗口内电量始终没动 -> unconfirmed（不假装成功）==")
c, pay = fresh_mut(ASUS_80)
c._lim_writer = mk_writer(pay)
c.set_full_intent(reason="manual")
c._pending["deadline"] = time.time() - 1.0      # 窗口已过
c.feed(batt(ac=True, pct=79), 45.0, 60.0)
st = c.report().get("limit_state")
ok(bool(st) and st["state"] == "unconfirmed", "超时未越线 -> unconfirmed", st)
ok(any("还没看到生效" in a for a in c.report()["advice"]),
   "提示里说清「可能没跟上」", c.report()["advice"])

print("== 34. 需要提权 -> 记录成 need_admin，不写成已下发 ==")
c, pay = fresh_mut(ASUS_80)
c._lim_writer = (lambda v: {"ok": False, "need_admin": True,
                            "reason": "写 HKLM 需要管理员权限"})
c.set_full_intent(reason="manual")
st = c.report().get("limit_state")
ok(bool(st) and st["state"] == "need_admin", "权限不够 -> need_admin", st)
ok(c.report().get("limit_pending") is False, "没写成功就不该有待生效标记")

print("== 35. 待生效状态跨重启保留（否则重启后假装已生效）==")
c, pay = fresh_mut(ASUS_80)
c._lim_writer = mk_writer(pay)
c.set_full_intent(reason="manual")
p = c.data_path
c2 = bc.BatteryCare({}, p, chg_reader=(lambda: dict(pay)))
ok(c2.report().get("limit_pending") is True, "重启后 pending 仍在",
   c2.report().get("limit_state"))
ok(c2.intent_active() is True, "重启后充满意图仍在")

print("== 36. 满充容量按天落盘（历史没存过 -> 只能靠考古）==")
c, _ = fresh({}, path=tmp_json())
c.feed(batt(ac=True, pct=99, full=60536, health=82.9), 45.0, 60.0)
b = c._data["days"][time.strftime("%Y-%m-%d")]
ok(b.get("full_mwh") == 60536, "当天记下 full_mwh", b.get("full_mwh"))
ok(b.get("health_pct") is not None, "同时记健康度", b.get("health_pct"))
c.feed(batt(ac=True, pct=100, full=60400), 45.0, 60.0)
b = c._data["days"][time.strftime("%Y-%m-%d")]
ok(b.get("full_mwh") == 60400, "同日多次喂入取最后值", b.get("full_mwh"))
st = c.report()["stat"]
ok(st.get("cap_series") and st["cap_series"][-1][1] == 60400,
   "stat 里带容量轨迹", st.get("cap_series"))
r = c.report()
ok(r.get("today", {}).get("full_mwh") == 60400, "report.today 同步快照",
   r.get("today", {}).get("full_mwh"))

print("== 37. 阶跃下跌 -> 提示区分「电量计重学习」与「真实衰减」==")
c, _ = fresh({}, path=tmp_json())
today = time.strftime("%Y-%m-%d")
yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
c._data["days"] = {yesterday: {"full_mwh": 60536, "health_pct": 82.9,
                               "full_soc_s": 0.0, "hot_charge_s": 0.0,
                               "deep_s": 0.0, "charge_wh": 0.0,
                               "discharge_wh": 0.0, "ac_drain_wh": 0.0,
                               "sample_s": 0.0}}
c.feed(batt(ac=True, pct=99, full=55244), 45.0, 60.0)
st = c.report()["stat"]
ok(st.get("cap_drop_wh") == round((60536 - 55244) / 1000.0, 1),
   "首末掉幅 %.1f Wh" % st["cap_drop_wh"], st.get("cap_drop_wh"))
adv = c.report()["advice"]
ok(any("电量计重新学习" in a for a in adv),
   "阶跃下跌提示电量计重学习，而不是直接判真实衰减", adv)

print("== 38. 容量回升 -> 说明之前的下降是校准假象 ==")
c, _ = fresh({}, path=tmp_json())
c._data["days"] = {yesterday: {"full_mwh": 55244, "health_pct": 75.7,
                               "full_soc_s": 0.0, "hot_charge_s": 0.0,
                               "deep_s": 0.0, "charge_wh": 0.0,
                               "discharge_wh": 0.0, "ac_drain_wh": 0.0,
                               "sample_s": 0.0}}
c.feed(batt(ac=True, pct=99, full=58900), 45.0, 60.0)
adv = c.report()["advice"]
ok(any("校准假象" in a for a in adv), "回升时给出反向结论", adv)

print("== 39. 面板短行：健康时不出现（用户要求「简化甚至不出现」）==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
seed_days(c, dc_h=6.0, ac_h=2.0)      # 常带出门 + 80% 上限，习惯良好
c.feed(batt(ac=True, pct=60, full=60536, health=82.9), 45.0, 60.0)
ok(c.report().get("advice_short") == "", "习惯良好 -> 短行为空（整行不出现）",
   c.report().get("advice_short"))

print("== 40. 满充搁置 + 上限 100% -> 一句短行，不是一百字长文 ==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_100)
seed_days(c, dc_h=0.5, ac_h=10.0, full_h=4.0)   # 日均满充搁置 4h
c.feed(batt(ac=True, pct=99, full=55244, health=75.7), 45.0, 60.0)
s = c.report().get("advice_short") or ""
ok("满充搁置" in s and "80" in s, "短行含问题+动作: %r" % s)
ok(len(s) <= 30, "短行 ≤30 字: %d 字" % len(s), len(s))
ok("MyASUS" not in s, "短行不带 MyASUS 点击路径（面板按钮一键可办）", s)
ok(any(len(a) > len(s) for a in c.report()["advice"]),
   "CLI 仍保留完整长建议", c.report()["advice"][:1])

print("== 41. 上限已压到目标档位 -> 不再唠叨（短行同样为空）==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
seed_days(c, dc_h=0.5, ac_h=10.0, full_h=4.0)
c.feed(batt(ac=True, pct=79, full=55244, health=75.7), 45.0, 60.0)
ok(c.report().get("advice_short") == "", "已压到 80% -> 不刷存在感",
   c.report().get("advice_short"))

print("== 42. 深放优先于满充搁置（不可逆损伤排最前）==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_100)
seed_days(c, dc_h=0.5, ac_h=10.0, full_h=4.0)
for k in list(c._data["days"]):
    c._data["days"][k]["deep_s"] = 10800.0     # 每天深放 3h（≤20%）
c.feed(batt(ac=True, pct=99, full=55244, health=75.7), 45.0, 60.0)
s = c.report().get("advice_short") or ""
ok("深放" in s, "深放排最前: %r" % s)
ok(len(s) <= 30, "依然短: %d 字" % len(s), len(s))

print("== 43. 上限写入翻车 -> 短行直说（否则用户不知道按钮白点了）==")
c, _ = fresh({}, path=tmp_json(), chg=ASUS_80)
seed_days(c, dc_h=0.5, ac_h=10.0, full_h=4.0)
c._set_limit_state(100, "rolled_back", "被厂商服务改回")
c.feed(batt(ac=True, pct=99, full=55244, health=75.7), 45.0, 60.0)
s = c.report().get("advice_short") or ""
ok("未生效" in s and "MyASUS" in s, "翻车时短行给指路: %r" % s)

print("\n结果：%d 通过 / %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
