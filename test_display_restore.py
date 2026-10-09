# -*- coding: utf-8 -*-
"""刷新率还原回归测试（2026-10-07）——用假 user32 钉住「屏幕卡 60Hz」这条链

背景
----
A-B-A-B 实测（tools/battery_soak.py --abab）抓到一个假基线：
裸机段和续航档段的日志里**全都是 60Hz / eco=1**，退出程序根本没把刷新率
和独显还原回去，于是两段功耗几乎一样，"前后对比"测不出来。

根因在 lp/display.py 的 apply()：记录 original_hz 的代码被放在了「真正
执行切换」的分支之后。只要启动时屏幕已经是目标值（上一次会话没还原成功
留下的 60Hz），apply(60) 就会命中提前返回 ⇒ original_hz 永远是 None、
changed 永远是 False ⇒ 退出时 restore() 走空分支 ⇒ 屏幕再也回不到
165Hz，而且这个污染会跨会话累积。

这里把三条规则钉死，防止以后改回去：
  1. 提前返回之前也必须记录 original_hz
  2. 真的动过（changed=True）而记录值又不是面板最高档时，还原到最高档（自愈）
  3. 没动过就别碰（用户自己设的 60Hz 不该被我们改回 165）
  4. 回读校验：ChangeDisplaySettingsEx 返回成功 ≠ 真的生效
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lp import display  # noqa: E402

PASS = 0
FAIL = 0

# 假显示子系统的状态
CUR = {"hz": 165}
CALLS = []
MODES = [60, 165]


class FakeUser32:
    """只实现 DisplayCtl 用到的两个 user32 调用。"""

    def EnumDisplaySettingsW(self, name, i, dm):
        # 真实 user32 有 argtypes 会做转换；假对象收到的是 byref() 的
        # CArgObject 包壳，真正的 DEVMODE 在它的 _obj 里。
        m = getattr(dm, "_obj", dm)
        if i == display.ENUM_CURRENT_SETTINGS:
            hz = CUR["hz"]
        else:
            if i < 0 or i >= len(MODES):
                return False
            hz = MODES[i]
        m.dmPelsWidth = 2560
        m.dmPelsHeight = 1600
        m.dmBitsPerPel = 32
        m.dmDisplayFrequency = hz
        return True

    def ChangeDisplaySettingsExW(self, name, dm, hwnd, flags, lparam):
        m = getattr(dm, "_obj", dm)
        # effective=False 时：假装成功但其实没生效（用来验证回读校验）
        if not FakeUser32.effective:
            return display.DISP_CHANGE_SUCCESSFUL
        CUR["hz"] = int(m.dmDisplayFrequency)
        CALLS.append(int(m.dmDisplayFrequency))
        return display.DISP_CHANGE_SUCCESSFUL


FakeUser32.effective = True


def reset(hz=165, effective=True):
    CUR["hz"] = hz
    CALLS.clear()
    FakeUser32.effective = effective


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK ] %s" % msg)
    else:
        FAIL += 1
        print("  [ERR] %s" % msg)


def main():
    display._u = FakeUser32()

    print("\n== 1) 正常路径：165 → 60 → 还原 165 ==")
    reset(165)
    d = display.DisplayCtl()
    ok(d.apply(60), "apply(60) 成功")
    ok(CUR["hz"] == 60, "当前已是 60Hz")
    ok(d.original_hz == 165, "记录了原始 165Hz（got=%s）" % d.original_hz)
    ok(d.changed is True, "changed=True")
    ok(d.restore(), "restore 成功")
    ok(CUR["hz"] == 165, "已还原到 165Hz（got=%s）" % CUR["hz"])
    ok(d.changed is False, "还原后 changed 复位")

    print("\n== 2) 污染场景：启动时屏幕已经是 60Hz ==")
    # 旧实现在这里 original_hz 会是 None —— 那条提前返回把记录跳过了
    reset(60)
    d2 = display.DisplayCtl()
    ok(d2.apply(60), "apply(60) 幂等返回 True")
    ok(d2.original_hz == 60, "**提前返回也记录了原始值**（got=%s，旧实现是 None）"
       % d2.original_hz)
    ok(CUR["hz"] == 60, "幂等路径不写显示子系统")

    print("\n== 3) 自愈：记录值是被污染的低档，而本次确实动过 ==")
    reset(165)
    d3 = display.DisplayCtl()
    d3.apply(60)                      # 正常降刷，original_hz=165
    d3.original_hz = 60               # 模拟记录值被上一次会话污染
    ok(d3.restore(), "restore 成功")
    ok(CUR["hz"] == 165, "还原到面板最高档 165（got=%s，若回 60 说明自愈失效）"
       % CUR["hz"])

    print("\n== 4) 没动过就别碰（用户自己设的 60Hz 不该被改） ==")
    reset(60)
    d4 = display.DisplayCtl()
    ok(d4.restore(), "restore 空操作返回 True")
    ok(not CALLS, "没有产生任何 ChangeDisplaySettingsEx 调用")

    print("\n== 5) 回读校验：API 说成功但没真的生效 ==")
    reset(60, effective=False)        # 切了但不生效
    d5 = display.DisplayCtl()
    d5.original_hz = 165
    d5.changed = True
    ok(not d5.restore(), "restore 判定失败（不能只看返回码）")
    ok("回读" in (d5.err or ""), "错误里写明回读不符（got=%r）" % d5.err)

    print("\n" + "=" * 50)
    print("结果: %d 项通过, %d 项失败" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
