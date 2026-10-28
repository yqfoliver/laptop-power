# -*- coding: utf-8 -*-
"""GUI 层回归：GDI 面板绘制 / 托盘电源广播 / WebView2 就绪判定。

这一层的问题症状和功耗逻辑不同源 —— 表现是「界面有黑边」「hover 没反应」
「点了没反应」，没有异常、没有日志，只能靠断言钉住。
"""
import ctypes
import inspect
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main as M  # noqa: E402
from lp import native_panel as NP  # noqa: E402
from lp import tray as TR  # noqa: E402
from lp import webview2panel as WV  # noqa: E402

N = [0]
FAILS = []


def ok(cond, msg, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(msg)
        print("  FAIL: %s  %s" % (msg, extra))
    return cond


# ------------------------------------------------------------ 1. GDI 填充
def _memdc(w, h):
    scr = NP._u.GetDC(None)
    mem = NP._g.CreateCompatibleDC(scr)
    bmp = NP._g.CreateCompatibleBitmap(scr, w, h)
    NP._g.SelectObject(mem, bmp)
    NP._u.ReleaseDC(None, scr)
    NP._g.GetPixel.restype = ctypes.c_uint
    NP._g.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    return mem


def _bare_panel():
    """绕开 __init__（不需要 Manager）：只给绘制用到的缓存和状态。"""
    p = NP.NativePanel.__new__(NP.NativePanel)
    p._brushes = {}
    p._pens = {}
    p._btns = []
    p._hover = None
    p._scale = 1.0
    p._snap = {}
    return p


def test_gdi_fill_no_border():
    print("== 1. GDI 填充不许带黑边（画笔槽要真选中 PS_NULL） ==")
    p = _bare_panel()
    fill = 0x00303030
    mem = _memdc(140, 140)
    p._fill(mem, fill, 10, 10, 130, 130, radius=16)
    top = NP._g.GetPixel(mem, 70, 10)          # 上边缘中点
    left = NP._g.GetPixel(mem, 10, 70)         # 左边缘中点
    inside = NP._g.GetPixel(mem, 70, 70)
    # 老实现用画刷占画笔槽 → 画笔仍是 DC 默认的黑 1px 笔 → 边缘被描成 0x000000
    ok(top == fill, "圆角矩形上边缘=填充色（不是默认黑笔）", hex(top))
    ok(left == fill, "圆角矩形左边缘=填充色", hex(left))
    ok(inside == fill, "内部=填充色", hex(inside))

    mem2 = _memdc(60, 60)
    p._fill(mem2, fill, 5, 5, 55, 55)          # 直角矩形（进度条/电池条走这条）
    edge = NP._g.GetPixel(mem2, 30, 5)
    ok(edge == fill, "直角矩形上边缘=填充色", hex(edge))

    # 画笔必须被缓存复用，不能每次 _fill 都新建（每秒重绘 ~20 次）
    ok(p._pen(None) == p._pen(None), "无边框笔缓存复用")
    ok(len(p._pens) == 1, "只建了一支笔", len(p._pens))


# ------------------------------------------------------------ 2. hover 反馈
def test_panel_hover_feedback():
    print("== 2. 按钮 hover 必须真的变色 ==")

    def fills_for(hover, snap=None):
        p = _bare_panel()
        p._hover = hover
        p._snap = snap or {}
        got = []
        p._fill = lambda hdc, c, l, t, r, b, radius=0: got.append((c, l, t, r, b))
        p._text = lambda *a, **k: None
        p._bar = lambda *a, **k: None
        p._line_kv_bar = lambda *a, **k: None
        p._draw_ui(0, NP.TOTAL_W, NP.TOTAL_H)
        return got

    base = fills_for(None)
    # 散热三档：曾经三元表达式两边都写 C_CARD_HI，hover 永远没变化
    for bid in ("t:quiet", "t:perf", "p:gaming", "auto", "web"):
        hot = fills_for(bid)
        diff = sum(1 for a, b in zip(base, hot) if a[0] != b[0])
        ok(diff >= 1, "hover=%s 至少一个区域变色" % bid, diff)
    # 当前激活的那颗按钮不吃 hover（active 颜色优先），要换一个档位再测
    snap = {"thermal": {"mode": "perf"}}
    hot = fills_for("t:auto", snap)
    base2 = fills_for(None, snap)
    diff = sum(1 for a, b in zip(base2, hot) if a[0] != b[0])
    ok(diff >= 1, "非激活档（t:auto）也有 hover 反馈", diff)
    # 但一次 hover 不该把整屏都换色（说明没写成"全体反色"）
    hot = fills_for("t:quiet")
    diff = sum(1 for a, b in zip(base, hot) if a[0] != b[0])
    ok(diff <= 3, "hover 只影响该按钮那几笔", diff)


# ------------------------------------------------------------ 3. 电源广播
def test_power_broadcast_routed():
    print("== 3. 系统电源广播要送到 Manager（不能只挂在按需创建的面板上） ==")
    t = TR.Tray()
    got = []
    t.power_handler = lambda w: got.append(w)
    rc = t._wndproc(0, TR.WM_POWERBROADCAST, TR.PBT_APMSUSPEND, 0)
    ok(rc == 0 and got == [TR.PBT_APMSUSPEND], "挂起事件送达回调", (rc, got))
    t._wndproc(0, TR.WM_POWERBROADCAST, TR.PBT_APMRESUMEAUTOMATIC, 0)
    ok(got[-1] == TR.PBT_APMRESUMEAUTOMATIC, "唤醒事件送达回调", got)

    class Boom:
        def __call__(self, w):
            raise RuntimeError("handler boom")

    t.power_handler = Boom()
    ok(t._wndproc(0, TR.WM_POWERBROADCAST, 4, 0) == 0,
       "回调抛异常不许外泄（否则消息泵一起死）")
    t.power_handler = None
    ok(t._wndproc(0, TR.WM_POWERBROADCAST, 4, 0) == 0, "未挂回调时也安全")

    # 分派语义：挂起→on_suspend；两种唤醒→on_resume；其余不理
    class FakeMgr:
        def __init__(self):
            self.calls = []

        def on_suspend(self):
            self.calls.append("suspend")

        def on_resume(self):
            self.calls.append("resume")

    m = FakeMgr()
    M._power_event(m, M.PBT_APMSUSPEND)
    ok(m.calls == ["suspend"], "4 → on_suspend", m.calls)
    M._power_event(m, M.PBT_APMRESUMESUSPEND)
    M._power_event(m, M.PBT_APMRESUMEAUTOMATIC)
    ok(m.calls == ["suspend", "resume", "resume"], "7/0x12 → on_resume", m.calls)
    M._power_event(m, 0x0000)          # PBT_APMQUERYSUSPEND：只询问，不该动手
    ok(len(m.calls) == 3, "询问型事件不触发动作", m.calls)
    M._power_event(None, M.PBT_APMSUSPEND)
    ok(True, "mgr 为 None 不炸")


# ------------------------------------------------------------ 4. WebView2
def test_webview2_not_permanently_failed():
    print("== 4. WebView2 首帧慢不许把整轮会话判死 ==")
    src = inspect.getsource(WV.WebView2Panel.show)
    body = src.split("def show", 1)[1]
    # 就绪超时那一支不许再写 failed = True（会把后续每次打开都变成"点了没反应"）
    ok("self.failed = True" not in body,
       "show() 里不得把 failed 钉死（超时只是这次没赶上）")
    ok("self.ready" in body, "仍以 ready 为准")

    # 重复 ensure 不得并发建第二套环境（第二个 CreateController 必失败，
    # 会反过来把本来能成功的那次判死）
    src2 = inspect.getsource(WV.WebView2Panel.ensure)
    ok("_ensuring" in src2, "ensure() 有在途保护")

    p = WV.WebView2Panel("http://127.0.0.1:1/")
    p._ensuring = True
    ok(p.ensure() is False, "在途时 ensure() 直接返回，不重复建环境")
    ok(p.failed is False, "在途不算失败")


def main():
    test_gdi_fill_no_border()
    test_panel_hover_feedback()
    test_power_broadcast_routed()
    test_webview2_not_permanently_failed()


if __name__ == "__main__":
    main()
    print()
    print("%d 项断言，失败 %d" % (N[0], len(FAILS)))
    if FAILS:
        print("失败项：%s" % FAILS)
        sys.exit(1)
    print("全部通过")
