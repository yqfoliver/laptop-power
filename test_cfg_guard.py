# -*- coding: utf-8 -*-
"""
配置护栏回归测试（2026-10-30 第七轮审查）
==========================================
这一轮的核心发现：**护栏只覆盖「声明过的」键**。

`config.sanitize()` 遍历 DEFAULT_CONFIG。有一整批「隐藏旋钮」只以
`cfg.get(key, 默认值)` 的形式活在各模块里，从没进过 DEFAULT_CONFIG：

  thermal_mode / thermal_quiet_* / thermal_perf_* / thermal_atk_enabled
  care_thermal_relief / care_hot_cpu_c / care_thermal_clear_c / care_thermal_cpu_pct

而 `/api/settings` 又能写这些键。于是 `{"care_hot_cpu_c": "abc"}` 落盘之后，
下次启动 BatteryCare/ThermalPolicy 在构造期 `float("abc")` 抛 ValueError，
Manager 建不起来 —— 进程静默退出，只剩 startup_err.log 一行 traceback。
（与 poll_seconds:"abc" 同一类，但更狠：那次是循环退出，这次是根本不启动。）

  1. sanitize 现在能纠正这批键（声明 + 范围）
  2. 读点自身也不会被坏值炸掉（两道护栏）
  3. _NUM_LIMITS 里的每个键都必须在 DEFAULT_CONFIG 里，否则范围永远不生效
  4. 纯死键（声明了却没人读）已删除，_migrate 会把旧缓存里的它们清掉
  5. hotkey 从「写死的 0x50」变成真配置；托盘窗口没建成时不注册
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import batterycare, config, thermal

PASS = 0
FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


GUARDED = ("care_hot_cpu_c", "care_thermal_clear_c", "care_thermal_cpu_pct",
           "care_thermal_relief", "thermal_quiet_relief_c", "thermal_perf_relief_c",
           "thermal_quiet_cpu_pct", "thermal_perf_relief_pct",
           "thermal_quiet_gpu_temp_c", "thermal_mode", "thermal_atk_enabled",
           "error_log", "self_eco_on_battery", "auto_game_gpu_pct")

print("== 1) 这批隐藏旋钮必须进 DEFAULT_CONFIG（否则护栏看不见它们）==")
missing = [k for k in GUARDED if k not in config.DEFAULT_CONFIG]
ok(not missing, "全部已声明", missing)

print("== 2) sanitize 把坏值纠正回默认 ==")
c = dict(config.DEFAULT_CONFIG)
for k in GUARDED:
    c[k] = "abc"
fixed = config.sanitize(c)
for k in GUARDED:
    v = c.get(k)
    want = config.DEFAULT_CONFIG[k]
    if isinstance(want, bool):
        ok(v is want, "%s 回到 %r" % (k, want), repr(v))
    else:
        ok(v == want, "%s 回到 %r" % (k, want), repr(v))
ok("care_hot_cpu_c" in fixed, "坏值被记进修正清单（可观测）", fixed)

print("== 3) 范围夹紧（越界不是坏字符串，是合法但危险的数字）==")
c = dict(config.DEFAULT_CONFIG)
c["care_hot_cpu_c"] = 999
c["care_thermal_cpu_pct"] = 1000
c["thermal_quiet_cpu_pct"] = -50
config.sanitize(c)
ok(c["care_hot_cpu_c"] == 115.0, "温度线上限夹到 115℃", c["care_hot_cpu_c"])
ok(c["care_thermal_cpu_pct"] == 100, "CPU 上限夹到 100%", c["care_thermal_cpu_pct"])
ok(c["thermal_quiet_cpu_pct"] == 10, "下限夹到 10%（0 会让 CPU 直接卡死）",
   c["thermal_quiet_cpu_pct"])

print("== 4) 读点自身不能被坏值炸掉（第二道护栏）==")
for mode in ("quiet", "perf"):
    cc = dict(config.DEFAULT_CONFIG)
    cc["thermal_mode"] = mode
    for k in ("thermal_quiet_relief_c", "thermal_perf_relief_c",
              "thermal_quiet_cpu_pct", "thermal_perf_relief_pct",
              "thermal_quiet_gpu_cpu_temp_c", "thermal_perf_gpu_temp_c"):
        cc[k] = "abc"
    try:
        t = thermal.ThermalPolicy(cc)
        ok(True, "%s：坏值不再抛异常" % mode)
        ok(t.relief_trigger in (88.0, 98.0), "%s：温度线回默认" % mode, t.relief_trigger)
        ok(t.cpu_cap_ac is None or 30 <= t.cpu_cap_ac <= 100,
           "%s：CPU 上限在安全区" % mode, t.cpu_cap_ac)
    except Exception as e:
        ok(False, "%s：坏值不再抛异常" % mode, repr(e))

try:
    b = batterycare.BatteryCare({"care_hot_cpu_c": "abc",
                                 "care_thermal_clear_c": "abc",
                                 "care_thermal_cpu_pct": "abc",
                                 "care_full_intent_h": "abc"}, None)
    ok(b.hot_cpu_c == 95.0 and b.thermal_clear_c == 89.0, "BatteryCare 构造不炸",
       (b.hot_cpu_c, b.thermal_clear_c))
    ok(b.thermal_cap == 80, "BatteryCare 的 CPU 上限回默认", b.thermal_cap)
    ok(b.intent_h == 24.0, "BatteryCare 的意图有效期回默认", b.intent_h)
except Exception as e:
    ok(False, "BatteryCare 构造不炸", repr(e))

print("== 5) _NUM_LIMITS 的键必须都声明过（否则范围永远不生效）==")
undeclared = sorted(set(config._NUM_LIMITS) - set(config.DEFAULT_CONFIG))
ok(not undeclared, "范围表里没有『孤儿键』", undeclared)

print("== 6) 死键已退场，旧 config.json 里的残留会被清掉 ==")
dead = ("low_battery_percent", "minimize_to_tray", "battery_eco_percent")
for k in dead:
    ok(k not in config.DEFAULT_CONFIG, "%s 已从默认配置移除" % k)
cfg = dict(config.DEFAULT_CONFIG)
for k in dead:
    cfg[k] = 20
config._migrate(cfg)
left = [k for k in dead if k in cfg]
ok(not left, "旧缓存里的死键被 _migrate 清掉", left)
ok("open_panel_on_start" in config.DEFAULT_CONFIG,
   "open_panel_on_start 补上了声明（main.py 一直在读它）")

print("== 7) hotkey 是真配置，不是写死的 0x50 ==")
import main as M                       # noqa: E402  （要 config 先就绪）

ok(M.parse_hotkey("Ctrl+Alt+P") == (0x0002 | 0x0001, 0x50), "默认 Ctrl+Alt+P")
ok(M.parse_hotkey("alt+shift+g") == (0x0001 | 0x0004, ord("G")), "alt+shift+g")
ok(M.parse_hotkey("Ctrl+Q") == (0x0002, ord("Q")), "只有 Ctrl 时修饰键不丢")
ok(M.parse_hotkey("Win+F5") == (0x0008, 0x74), "Win+F5")
ok(M.parse_hotkey("f11") == (0x0002 | 0x0001, 0x50),
   "认不出来（缺修饰键）回默认，绝不注册一个猜的组合")
ok(M.parse_hotkey("Ctrl+Alt+") == (0x0002 | 0x0001, 0x50), "尾随 + 不崩")
ok(M.parse_hotkey(None) == (0x0002 | 0x0001, 0x50), "None 不崩")
ok(M.parse_hotkey("Ctrl+Alt+Ö") == (0x0002 | 0x0001, 0x50), "非 ASCII 键名回默认")
ok(M.register_hotkey(0, "Ctrl+Alt+P") is False,
   "托盘窗口没建成时不注册（否则白白吞掉别人的全局快捷键）")

print("\n通过 %d，失败 %d" % (PASS, FAIL))
raise SystemExit(1 if FAIL else 0)
