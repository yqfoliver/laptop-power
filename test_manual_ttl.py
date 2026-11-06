# -*- coding: utf-8 -*-
"""手动锁定自动过期（2026-11-06 B）+ 热键切档（C）的回归测试。

为什么值得单测：这两条都属于「静默生效型」逻辑 —— 出问题时界面一切正常，
只是档位永远不回落 / 热键按了没反应。而且第一版最可能踩的坑是：
  · 过期检查写在 `_loop` 的自动分支里（手动档下整段跳过 ⇒ 永不执行）
  · 起始时刻只在内存里（重启一次 = 续期一次）
  · WM_HOTKEY 不看 wparam（第二个热键静默变成"打开面板"）
"""
from __future__ import annotations

import inspect
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import config, tray
from lp.manager import Manager

PASS = FAIL = 0
_SAVES = []


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


def mk(manual="gaming", ttl=60, started_ago_min=0.0):
    """只装 TTL 相关字段的 Manager（不走 __init__：那会碰硬件/写盘）。"""
    m = Manager.__new__(Manager)
    m.cfg = dict(config.DEFAULT_CONFIG)
    if ttl is not None:
        m.cfg["manual_ttl_min"] = ttl
    m.manual = manual
    m._manual_at = (time.time() - started_ago_min * 60.0) if manual else 0.0
    m.cfg["manual_override"] = manual
    m.cfg["manual_since"] = m._manual_at
    m.cfg["auto_mode"] = False
    m._manual_dropped = ""
    m.current = "gaming"
    m.applied = []

    class G:
        def __init__(self):
            self.n = 0

        def reset(self):
            self.n += 1

    m.governor = G()
    m.detect = lambda *a, **k: "office"
    m.apply = lambda name, reason="", **k: m.applied.append((name, reason))
    return m


def main() -> int:
    real_save = config.save
    config.save = lambda c: _SAVES.append(dict(c))
    try:
        print("manual_ttl_s：配置不可信时的兜底")
        ok(mk(ttl=60).manual_ttl_s() == 3600.0, "60 分钟 → 3600 秒")
        ok(mk(ttl="abc").manual_ttl_s() == 3600.0, "字符串 → 回默认 60 分钟")
        ok(mk(ttl=None).manual_ttl_s() == 3600.0, "None → 回默认")
        ok(mk(ttl=0).manual_ttl_s() == 0.0, "0 = 关闭自动解除")
        ok(mk(ttl=-5).manual_ttl_s() == 0.0, "负数 = 关闭（不当成过期）")
        ok(mk(ttl=99999).manual_ttl_s() == 1440 * 60.0, "超上限被钳到 24 小时")

        print("manual_left_min：剩余时间")
        ok(mk(started_ago_min=30).manual_left_min() == 30, "锁了 30 分钟 → 还剩 30")
        ok(mk(started_ago_min=59.5).manual_left_min() == 1, "向上取整（别显示 0）")
        ok(mk(started_ago_min=61).manual_left_min() == 0, "已过期 → 0")
        ok(mk(manual="battery", started_ago_min=999).manual_left_min() is None,
           "续航档不解锁 → 没有倒计时")
        ok(mk(manual=None).manual_left_min() is None, "没锁定 → None")
        ok(mk(ttl=0).manual_left_min() is None, "关掉 TTL → None")

        print("_manual_tick：未到点")
        m = mk(started_ago_min=10)
        m._manual_tick()
        ok(m.manual == "gaming", "未到期不动档位")
        ok(m.applied == [], "未到期不切档（不打扰）")

        print("_manual_tick：到点交回自动")
        m = mk(started_ago_min=61)
        m._manual_tick()
        ok(m.manual is None, "到期后手动锁定被清")
        ok(m.cfg["manual_override"] is None, "落盘的 manual_override 同步清空")
        ok(m.cfg["manual_since"] == 0, "起始时刻同步归零（配置不许撒谎）")
        ok(m.cfg["auto_mode"] is True, "自动判定开关跟着亮回去")
        ok(m.governor.n == 1, "交回自动前重置切换闸门")
        ok(len(m.applied) == 1 and m.applied[0][0] == "office",
           "按自动判定立刻套用一次", m.applied)
        ok(m.applied and "交回自动" in m.applied[0][1], "原因说清为什么变档",
           m.applied)

        print("_manual_tick：续航档 / 关闭 TTL")
        m = mk(manual="battery", started_ago_min=999)
        m._manual_tick()
        ok(m.manual == "battery" and m.applied == [], "锁在续航档永不自动解除")
        m = mk(started_ago_min=999, ttl=0)
        m._manual_tick()
        ok(m.manual == "gaming", "TTL=0 时到点也不动（用户关掉了）")

        print("_manual_tick：起始时刻缺失 / 时钟错乱")
        m = mk(started_ago_min=0)
        m._manual_at = 0.0
        m.cfg["manual_since"] = 0
        m._manual_tick()
        ok(m.manual == "gaming" and m.applied == [], "没有起始时刻 → 不判过期")
        ok(m._manual_at > 0 and m.cfg["manual_since"] > 0, "顺手补记起始时刻并落盘")
        m._manual_at = time.time() + 3600
        m._manual_tick()
        ok(m.manual == "gaming", "时钟回拨（起始时刻在未来）→ 不判过期")

        print("_drop_manual_on_boot：不跨重启")
        m = mk(manual="gaming", started_ago_min=1)
        m._drop_manual_on_boot()
        ok(m.manual is None and m._manual_dropped == "gaming",
           "重启后耗电档锁定被清，并记下原来锁的是哪档")
        ok(m.cfg["auto_mode"] is True and m.cfg["manual_since"] == 0,
           "配置同步：自动开、时间戳归零")
        m = mk(manual="battery", started_ago_min=1)
        m._drop_manual_on_boot()
        ok(m.manual == "battery" and m._manual_dropped == "",
           "续航档不受影响（保守选择留着没代价）")

        print("release_manual：唯一的清锁入口")
        m = mk(started_ago_min=5)
        m.release_manual()
        ok(m.manual is None and m._manual_at == 0.0
           and m.cfg["manual_override"] is None and m.cfg["manual_since"] == 0,
           "release_manual 一次清干净四个字段")
        ok(m.applied == [], "release_manual 本身不切档（调用方决定）")

        print("接线：status 必须暴露倒计时 / _loop 必须调用过期检查")
        src_status = inspect.getsource(Manager.status)
        ok("manual_left_min" in src_status, "status() 带上 manual_left_min（面板要用）")
        src_loop = inspect.getsource(Manager._loop)
        ok("_manual_tick" in src_loop, "过期检查真的挂在巡检里（不是死代码）")
        # 关键：必须在「自动分支」之前 —— 手动档下那段整段跳过
        i_tick = src_loop.find("_manual_tick")
        i_auto = src_loop.find("auto_mode")
        ok(0 <= i_tick < i_auto, "过期检查在自动分支之前（否则手动档下永不执行）",
           (i_tick, i_auto))
        ok("auto_full_tick" in src_loop, "离电前充满也挂在巡检里")

        print("热键：两个组合必须用不同 id")
        got = []
        t = tray.Tray(on_command=lambda mid: got.append(mid))
        t._wndproc(0, tray.WM_HOTKEY, 1, 0)          # id=1 → 开面板
        t._wndproc(0, tray.WM_HOTKEY, 2, 0)          # id=2 → 切档
        ok(got == [tray.MENU_OPEN, tray.MENU_CYCLE],
           "WM_HOTKEY 按 wparam 分流（否则切档键会静默变成开面板）", got)
        ok(tray.MENU_CYCLE not in (tray.MENU_OPEN, tray.MENU_QUIT),
           "MENU_CYCLE 是独立命令 id")
    finally:
        config.save = real_save

    print("\n%d 项通过，%d 项失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
