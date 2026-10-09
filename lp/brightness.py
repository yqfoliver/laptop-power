# -*- coding: utf-8 -*-
"""屏幕亮度控制（离电续航优化）。

本机标定（2026-10-05 实测）：
- dxva2 DDC/CI 对 eDP 内屏不可用（GetMonitorBrightness 失败）；
- WMI root/wmi 的 WmiMonitorBrightness 类存在（读数与电源计划值一致），
  但设置走 COM ExecMethod 复杂度高；
- **改活动电源计划 VIDEONORMALLEVEL 的索引即时生效**（实测 WMI 实读跟随），
  且完全复用项目已验证的 powercfg 静默通道（子进程 CREATE_NO_WINDOW，无窗口）。

文献依据（2026-10 检索多源交叉）：
- 屏幕是离电第一耗电大户（占整机 20~50%），亮度 100%→50% 可延长续航 20~30%；
- 续航最佳亮度区间 30~50%（可视性与功耗平衡点）；
- 人眼感知是对数的（Weber 定律）：高亮度段大幅下调几乎无感；
- 空闲渐进调暗（adaptive/idle dimming）可再省 10~20%。

模块职责：
- read()：读电源计划里当前 AC/DC 亮度索引；
- apply()：写 AC/DC 索引并立即生效（SCHEME_CURRENT 重应用）；
- idle_seconds()：用户输入空闲时长（GetLastInputInfo，0.001ms 级）。
控制策略（亮度封顶/空闲调暗/尊重用户手动调整）在 manager 里。
"""
import ctypes
import re
import time
from ctypes import wintypes

if __package__:
    from . import powercfgctl as pc
    from . import powrprof as pp
else:                                    # 直接运行 lp/brightness.py 自测
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lp import powercfgctl as pc
    from lp import powrprof as pp

GUID_VIDEO = "7516b95f-f776-4464-8c53-06167f40cc99"   # SUB_VIDEO
GUID_LEVEL = "aded5e82-b909-4619-9949-f5d71dac0bcb"   # VIDEONORMALLEVEL 显示器亮度

_IDX_RE = re.compile(r"0x([0-9a-fA-F]+)")
# 精确匹配「当前交流/直流电源设置索引」——powercfg /q 里还有最小/最大可能的设置等
# 0x 数值，靠「取最后两个」是脆弱假设（同项目 PROCTHROTTLEMAX 就因此翻过车）
_CUR_RE = re.compile(r"当前(交流|直流)电源设置索引:\s*(0x[0-9a-fA-F]+)")


class BrightnessCtl:
    """亮度读写。所有方法失败均返回 None/False，绝不抛异常。

    2026-10-06 提速（对标 GHelper PowerNative.cs / ScreenBrightness.cs）：
      · 主路径改为 **powrprof 进程内 API**：读/写各 ~1.3ms，零子进程零窗口；
      · powercfg 子进程保留为兜底（API 不可用时自动回退），语义完全等价
        （原实现走 SCHEME_CURRENT，即「当前活动计划」——API 里就是
         active_scheme() 读出 GUID 再写它、再 set_active 一次）。
      · 调用方仍须先过 `_scheme_active_ok()` 守门（处于系统「平衡」档时
        活动计划就是系统计划，写进去等于改了系统计划 —— 硬性禁止）。
    """

    def __init__(self) -> None:
        self.available = True
        self._last_ok = 0.0
        self._via = ""            # 最近一次成功的通道：powrprof / powercfg

    # ------------------------------------------------------------ 读
    def read(self):
        """返回 (ac, dc) 亮度索引（0~100），失败返回 (None, None)"""
        # ① 快路径：进程内 API
        if pp.available():
            try:
                sch = pp.active_scheme()
                if sch:
                    ac = pp.brightness(sch, False)
                    dc = pp.brightness(sch, True)
                    if ac is not None or dc is not None:
                        self._via = "powrprof"
                        self._last_ok = time.time()
                        return (ac, dc)
            except Exception:
                pass
        # ② 兜底：powercfg
        try:
            out = pc.run_powercfg(["/q", "SCHEME_CURRENT", GUID_VIDEO, GUID_LEVEL])
            if not out:
                return (None, None)
            vals = {}
            for kind, hexv in _CUR_RE.findall(out):
                v = int(hexv, 16)
                if 0 <= v <= 100:
                    vals[kind] = v
            if "交流" in vals or "直流" in vals:
                self._via = "powercfg"
                self._last_ok = time.time()
                return (vals.get("交流"), vals.get("直流"))
        except Exception:
            pass
        return (None, None)

    # ------------------------------------------------------------ 写
    def apply(self, ac=None, dc=None) -> bool:
        """写亮度索引并立即生效。只写提供的一侧。
        快路径 ≈ 2~3 ms（3 次进程内 API），兜底 powercfg ≈ 150 ms。"""
        # ① 快路径：进程内 API（读活动计划 → 写 → 重新激活一次）
        if pp.available():
            try:
                sch = pp.active_scheme()
                if sch:
                    ok = False
                    if ac is not None:
                        v = max(0, min(100, int(ac)))
                        ok = pp.set_brightness(sch, v, False) or ok
                    if dc is not None:
                        v = max(0, min(100, int(dc)))
                        ok = pp.set_brightness(sch, v, True) or ok
                    if ok:
                        pp.set_active(sch)      # 写索引后激活一次才生效
                        self._via = "powrprof"
                        self._last_ok = time.time()
                        return True
            except Exception:
                pass
        # ② 兜底：powercfg
        ok = False
        try:
            if ac is not None:
                v = max(0, min(100, int(ac)))
                pc.run_powercfg(["/setacvalueindex", "SCHEME_CURRENT",
                                 GUID_VIDEO, GUID_LEVEL, str(v)])
                ok = True
            if dc is not None:
                v = max(0, min(100, int(dc)))
                pc.run_powercfg(["/setdcvalueindex", "SCHEME_CURRENT",
                                 GUID_VIDEO, GUID_LEVEL, str(v)])
                ok = True
            if ok:
                pc.run_powercfg(["/setactive", "SCHEME_CURRENT"])
                self._via = "powercfg"
                self._last_ok = time.time()
        except Exception:
            return False
        return ok

    # ------------------------------------------------------------ 用户空闲
    @staticmethod
    def idle_seconds() -> float:
        """用户无键鼠输入的秒数（屏保/熄屏/视频暂停判定也用它）"""
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        try:
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                now = ctypes.windll.kernel32.GetTickCount()
                dw = lii.dwTime
                if now < dw:                      # GetTickCount 回绕
                    now += 0x100000000
                return max(0.0, (now - dw) / 1000.0)
        except Exception:
            pass
        return 0.0


if __name__ == "__main__":
    b = BrightnessCtl()
    ac, dc = b.read()
    print("当前亮度 AC=%s DC=%s" % (ac, dc))
    print("用户空闲 %.1f 秒" % b.idle_seconds())
    # 不在这里做写入演示，避免打扰用户屏幕
