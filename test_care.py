# -*- coding: utf-8 -*-
"""电池充放电管理（lp/batterycare.py）离线回归测试

不碰真实电源计划：把 batterycare 用的 powercfg 通道替换成假实现，
只验证「统计口径 + 评分 + 降温动作的写入/还原时序」。
用法：python test_care.py
"""
import json
import os
import sys
import tempfile

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
         full=60536):
    return {"ac": ac, "charging": charging, "discharging": discharging,
            "percent": pct, "rate_w": rate, "full_mwh": full}


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
c, pc = fresh({"care_heat_relief": False}, path=tmp_json())
c.feed(batt(ac=True, charging=True, pct=60, rate=-30.0), 95.0, 60.0)
ok(len(pc.writes()) == 0, "关闭降温开关后不写计划")
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

print("\n结果：%d 通过 / %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
