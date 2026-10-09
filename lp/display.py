# -*- coding: utf-8 -*-
"""屏幕刷新率控制（纯 ctypes，零依赖）

为什么值得单独做一个模块
------------------------
屏幕是笔记本上**最大的单体耗电件**，而高刷面板在电池模式下纯属浪费：

  · 本机面板（TUF A14，2560x1600）实测支持的两档刷新率就是 **60 Hz / 165 Hz**
    —— 165 Hz 意味着 GPU/显示控制器每秒多做 165 次扫描输出，
    对静态办公/看文档的场景是纯粹的漏电。
  · Berkeley Lab 的实测口径里，同尺寸高刷面板在 120 Hz 下的面板功耗约 2.9 W，
    降到 60 Hz 普遍能省 0.5 ~ 1.5 W；本机 165→60 的跨度更大。
  · 这个收益不需要牺牲任何「体验」——反正省电档本来就不是打游戏用的，
    真正要帧率的时候（离电打游戏）我们会自动把刷新率还回去。

实现要点
--------
  * EnumDisplaySettingsW 枚举当前分辨率下所有可用刷新率（**只挑已验证存在的档**，
    绝不硬写一个面板不支持的频率，否则会得到黑屏）
  * ChangeDisplaySettingsExW 用 flags=0 做**易失性**切换：
    重启后自动回到系统登记的 165 Hz，不留任何持久痕迹（程序崩了也不会把机器锁在 60 Hz）
  * 只改刷新率，宽高/色深保持原样

本机实测（2026-10-04）：
  当前 2560x1600 @165 Hz 32bpp；同分辨率可用刷新率 = [60, 165]
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_long, c_short, sizeof, wintypes

_u = ctypes.WinDLL("user32", use_last_error=True)

ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
ENUM_REGISTRY_SETTINGS = 0xFFFFFFFE

DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000

DISP_CHANGE_SUCCESSFUL = 0
DISP_CHANGE_BADMODE = -2
DISP_CHANGE_FAILED = -1
DISP_CHANGE_RESTART = 1


class DEVMODE(Structure):
    """DEVMODEW 的完整布局（220 字节）。联合体两种解释都是 16 字节，直接按打印机口径写即可。"""
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
        ("dmOrientation", c_short), ("dmPaperSize", c_short),
        ("dmPaperLength", c_short), ("dmPaperWidth", c_short),
        ("dmScale", c_short), ("dmCopies", c_short),
        ("dmDefaultSource", c_short), ("dmPrintQuality", c_short),
        ("dmColor", c_short), ("dmDuplex", c_short),
        ("dmYResolution", c_short), ("dmTTOption", c_short),
        ("dmCollate", c_short), ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
    ]


_u.EnumDisplaySettingsW.restype = wintypes.BOOL
_u.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, POINTER(DEVMODE)]
_u.ChangeDisplaySettingsExW.restype = c_long
_u.ChangeDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, POINTER(DEVMODE),
                                        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p]


def _modal(hz: int) -> DEVMODE:
    dm = DEVMODE()
    dm.dmSize = sizeof(DEVMODE)
    dm.dmFields = (DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL | DM_DISPLAYFREQUENCY)
    dm.dmDisplayFrequency = int(hz)
    return dm


class DisplayCtl:
    """把刷新率当成一个可来回切的开关。切换失败一律静默，绝不阻塞主流程。"""

    COOLDOWN = 15.0          # 连续失败时不反复折腾显示子系统

    def __init__(self):
        self.changed = False
        self.original_hz = None
        self.available_hz = []
        self.err = ""
        self._last_try = 0.0

    # ------------------------------------------------------------ 查询
    def _current(self):
        dm = DEVMODE()
        dm.dmSize = sizeof(DEVMODE)
        if _u.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, byref(dm)):
            return dm
        return None

    def current_hz(self):
        dm = self._current()
        return int(dm.dmDisplayFrequency) if dm else None

    def current_mode_text(self):
        dm = self._current()
        if not dm:
            return ""
        return "%dx%d@%dHz" % (dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency)

    def available_hz_list(self, refresh: bool = False) -> list:
        """当前分辨率下所有可用刷新率（升序）。结果缓存，refresh=True 时重算。"""
        if self.available_hz and not refresh:
            return self.available_hz
        cur = self._current()
        if not cur:
            return []
        freqs = set()
        i = 0
        while True:
            dm = DEVMODE()
            dm.dmSize = sizeof(DEVMODE)
            if not _u.EnumDisplaySettingsW(None, i, byref(dm)):
                break
            if (dm.dmPelsWidth == cur.dmPelsWidth
                    and dm.dmPelsHeight == cur.dmPelsHeight
                    and dm.dmBitsPerPel == cur.dmBitsPerPel
                    and dm.dmDisplayFrequency):
                freqs.add(int(dm.dmDisplayFrequency))
            i += 1
        self.available_hz = sorted(freqs)
        return self.available_hz

    def lowest_hz(self):
        lst = self.available_hz_list()
        return lst[0] if lst else None

    # ------------------------------------------------------------ 切换
    def apply(self, hz) -> bool:
        """把刷新率切到 hz（易失性，重启自动回到系统设置）。"""
        try:
            hz = int(hz)
        except Exception:
            return False
        dm = self._current()
        if dm is None:
            self.err = "取不到当前显示模式"
            return False
        # 记录「未干预前的刷新率」必须放在**所有提前返回之前**。
        #
        # 旧实现把它放在真正切换的分支里，结果：若启动时屏幕已经是目标值
        # （上一次会话退出没还原成功、留下 60Hz），apply(60) 会命中下面的
        # 提前返回，original_hz 永远是 None、changed 永远是 False ⇒ 退出时
        # restore() 走空分支，屏幕再也回不到 165Hz，而且污染跨会话累积。
        # 2026-10-07 A-B-A-B 实测就是这样：基线段和续航档段都是 60Hz。
        if self.original_hz is None:
            self.original_hz = int(dm.dmDisplayFrequency)
        if int(dm.dmDisplayFrequency) == hz:
            return True
        if hz not in self.available_hz_list():
            self.available_hz_list(refresh=True)
            if hz not in self.available_hz_list():
                self.err = "面板不支持 %d Hz（可用：%s）" % (hz, self.available_hz or "?")
                return False

        tgt = _modal(hz)
        tgt.dmPelsWidth = dm.dmPelsWidth
        tgt.dmPelsHeight = dm.dmPelsHeight
        tgt.dmBitsPerPel = dm.dmBitsPerPel
        rc = _u.ChangeDisplaySettingsExW(None, byref(tgt), None, 0, None)
        if rc != DISP_CHANGE_SUCCESSFUL:
            self.err = "ChangeDisplaySettingsEx 返回 %s" % rc
            return False
        self.changed = True
        self.err = ""
        return True

    def restore(self, force_hz=None) -> bool:
        """还原到切换前的刷新率，并**回读校验**。

        目标值优先用记录的 original_hz；但本程序只会把刷新率往低了调，
        所以只要本次会话真的动过（changed=True），而记录值又不是面板
        最高档 —— 说明记录值可能是被上一次会话污染过的低值 —— 就用
        最高档兜底。最高档才是插电/出厂时的常态，这样能自愈污染链。
        """
        if force_hz is None and not self.changed:
            self.changed = False
            return True
        tgt = force_hz
        if tgt is None:
            tgt = self.original_hz
            try:
                top = (self.available_hz_list(refresh=True) or [None])[-1]
            except Exception:
                top = None
            if top and (tgt is None or int(tgt) < int(top)):
                tgt = top
        if tgt is None:
            self.changed = False
            return True
        ok = self.apply(tgt)
        # 回读校验：ChangeDisplaySettingsEx 返回成功 ≠ 真的生效了
        if ok:
            got = self.current_hz()
            if got is not None and got != int(tgt):
                self.err = "还原后回读 %dHz（期望 %dHz）" % (got, tgt)
                ok = False
            else:
                self.changed = False
        return ok

    def sample(self) -> dict:
        return {
            "current_hz": self.current_hz(),
            "original_hz": self.original_hz,
            "available_hz": list(self.available_hz_list()),
            "changed": self.changed,
            "mode": self.current_mode_text(),
            "err": self.err,
        }


def _selftest():  # pragma: no cover
    import time
    d = DisplayCtl()
    print("当前模式：", d.current_mode_text())
    print("可用刷新率：", d.available_hz_list())
    lo = d.lowest_hz()
    if lo and lo != d.current_hz():
        print("切到 %d Hz ->" % lo, d.apply(lo))
        time.sleep(2)
        print("  切换后：", d.current_mode_text())
        print("还原 ->", d.restore())
        time.sleep(2)
        print("  还原后：", d.current_mode_text())
    else:
        print("无需切换")
    print("状态：", d.sample())


if __name__ == "__main__":  # pragma: no cover
    _selftest()
