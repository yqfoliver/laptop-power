# -*- coding: utf-8 -*-
"""自学习 + 开机自启 自测（不改动系统电源计划：只用 dry 与纯数据方法）"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import config, learner as lrn

print("=" * 60)
print("1) 开机自启注册项")
print("   python_exe :", config.python_exe())
print("   launcher   :", config.launcher())
print("   cmd        :", config.autostart_cmd())
print("   当前已开启 :", config.autostart_enabled())

before = config.autostart_enabled()
ok = config.set_autostart(True)
now = config.autostart_enabled()
print("   开启测试   : set=%s 读回=%s  (vbs=%s)" % (
    ok, now, os.path.isfile(os.path.join(config.APP_ROOT, "静默启动.vbs"))))

config.set_autostart(False)
print("   关闭测试   : 读回=%s  (期望 False, vbs=%s)" % (
    config.autostart_enabled(),
    os.path.isfile(os.path.join(config.APP_ROOT, "静默启动.vbs"))))

config.set_autostart(before)
print("   还原为     :", config.autostart_enabled(), "-> 保持原样:", config.autostart_enabled() == before)

print("=" * 60)
print("2) 自学习：进程画像 -> 规则")
L = lrn.Learner()
L.reset()

for i in range(3):
    L.vote("mycustomgame.exe", "gaming", True)
    L.observe("mycustomgame.exe", "gaming", True)
r = L.rule_for("mycustomgame.exe", True)
print("   3 票后规则  :", r, "(期望 gaming)")
print("   同一程序但对电池:", L.rule_for("mycustomgame.exe", False), "(期望 None)")

L.vote("mysim.exe", "battery", False)
print("   1 票还不成规则 :", L.rule_for("mysim.exe", False), "(期望 None)")
L.vote("mysim.exe", "battery", False)
L.vote("mysim.exe", "battery", False)
print("   3 票后         :", L.rule_for("mysim.exe", False), "(期望 battery)")

print("=" * 60)
print("3) 自学习：抖动抑制")
for i in range(6):
    L.feed("gaming", 8)          # 平均只有 8 秒 -> 判定太急
print("   短停留后 confirm:", L.confirm_needed("gaming"), "(应被调高)")
for i in range(10):
    L.feed("office", 7200)       # 平均 2 小时 -> 可以更快响应
print("   长停留后 confirm:", L.confirm_needed("office"), "(应降回 1)")

print("=" * 60)
print("4) 自学习：参数偏好 + 失败自愈")
L.set_pref("battery", "bright", 75, 40)
print("   battery.bright =", L.knob_value("battery", "bright", 40), "(期望 75)")
L.observe("x.exe", "battery", False)
for i in range(4):
    L.note_fail("battery", "proc_max")
print("   失败 4 次后 is_disabled(battery,proc_max):", L.is_disabled("battery", "proc_max"), "(期望 True)")
print("   未失败的项仍可用:", L.is_disabled("battery", "bright"))

rep = L.report()
print("=" * 60)
print("5) 报告摘要")
print("   观察小时:", rep["hours"], " 切档:", rep["switches"],
      " 识别程序:", rep["known_procs"], " 规则:", len(rep["rules"]))
print("   dwell:", rep["dwell"])
print("   pref:", rep["pref"], " disabled:", rep["disabled"])

print("=" * 60)
print("6) Manager 接入（dry 预览，不写真实电源计划）")
from lp.manager import Manager
from lp import profiles
mgr = Manager(config.load())
for key in ("gaming", "battery", "office"):
    res = mgr.apply(key, reason="自测", dry=True)
    print("   [%s] 待写入 %d 项 %s" % (key, len(res["changed"]),
                                     res.get("failed") or ""))
print("   detect() =", mgr.detect())
st = mgr.status()
print("   learn_on=%s learn_rules=%s learn_hours=%s autostart=%s"
      % (st["learn_on"], st["learn_rules"], st["learn_hours"], st.get("autostart")))

print("=" * 60)
print("7) 清空学习数据")
mgr.learn_reset()
print("   规则数:", len(mgr.learner.data.get("rules", {})), "(期望 0)")
print("   learn.json 已重建:", os.path.isfile(lrn.LEARN_PATH))
print("=" * 60)
print("全部通过")
