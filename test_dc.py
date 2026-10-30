# -*- coding: utf-8 -*-
"""离电续航策略回归测试（不写电源计划，只测判定与刷新率目标）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import config, hw
from lp.manager import Manager

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += (not ok)
    print("[%s] %-38s got=%s want=%s" % ("PASS" if ok else "FAIL", name, got, want))


def mk(pct, ac, fg="explorer.exe", heavy=False):
    """伪造电源/前台/负载，让 detect() 走纯逻辑分支"""
    m = Manager(config.load())
    orig_ps, orig_fg, orig_procs = hw.power_status, hw.foreground, hw.list_processes
    hw.power_status = lambda: {"ac": ac, "battery_percent": pct, "charging": False,
                               "has_battery": True}
    hw.foreground = lambda: {"process": fg, "title": "t", "fullscreen": False}
    hw.list_processes = lambda: {fg: 111}
    m.learner.rule_for = lambda exe, acs: None          # 屏蔽自学习干扰
    m.learner.confirm_needed = lambda x: 2
    t = m.detect({"heavy": heavy, "state": "轻负载", "busy": 10})
    hw.power_status, hw.foreground, hw.list_processes = orig_ps, orig_fg, orig_procs
    return t


print("---- detect() 电量梯度（2026-10-30 五并三：省电并入续航、平衡并入办公）----")
check("离电 10% + 办公 -> 续航（省电档已并入）", mk(10, False, "msedge.exe"), "battery")
check("离电 10% + 桌面 -> 续航", mk(10, False, "explorer.exe"), "battery")
check("离电 25% + 办公 -> 续航", mk(25, False, "msedge.exe"), "battery")
check("离电 25% + 重负载 -> 续航", mk(25, False, "msedge.exe", heavy=True), "battery")
check("离电 50% + 办公 -> 续航（离电铁律）", mk(50, False, "msedge.exe"), "battery")
check("离电 50% + 桌面 -> 续航", mk(50, False, "explorer.exe"), "battery")
check("离电 15% + 游戏 -> 游戏档(不分低电)", mk(15, False, "steam.exe"), "gaming")
check("插电 100% + 办公 -> 办公", mk(100, True, "msedge.exe"), "office")
check("插电 100% + 桌面 -> 办公（平衡已并入）", mk(100, True, "explorer.exe"), "office")

print("---- 刷新率目标 ----")
m = Manager(config.load())
st_ac = {"ac": True, "battery_percent": 100}
st_dc = {"ac": False, "battery_percent": 40}
m.current = "battery"
check("续航档 + 插电 -> 还原(不压)", m._refresh_target_hz(st_ac), None)
check("续航档 + 离电 -> 60Hz", m._refresh_target_hz(st_dc), 60)
m.current = "gaming"
check("游戏档 + 离电 -> 不动刷新率", m._refresh_target_hz(st_dc), None)
m.cfg["refresh_on_battery"] = False
check("功能关闭 -> 不动", m._refresh_target_hz(st_dc), None)

print("---- 电池遥测 ----")
b = m.bat.sample()
check("电池设备存在", b["present"], True)
check("健康度可读", 75.0 <= float(b["health_pct"]) <= 95.0, True)  # 真实值随老化漂移，只查区间
print("  状态=%s %s%% 放电=%sW 健康=%s%%" % (
    b["state_cn"], b["percent"], b["rate_w"], b["health_pct"]))

print("---- 档位表 ----")
from lp.profiles import PROFILES
# 2026-10-30 五并三：saver/balanced 从判定与 UI 退场，但 PROFILES 保留
# 这两个条目（老配置、学习数据、托盘「回到系统平衡」还原路径还在用）。
check("合并档位仍保留内部定义（还原路径用）",
      "saver" in PROFILES and "balanced" in PROFILES, True)
check("续航档离电 CPU 上限 30%", PROFILES["battery"]["dc"]["proc_max"], 30)
check("续航档离电强制核显", PROFILES["battery"]["dc"]["gpu_pref"], 0)
check("续航档离电含自适应亮度", "adapt_bright" in PROFILES["battery"]["dc"], True)

print("=" * 60)
print("结果: %d 项通过, %d 项失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
