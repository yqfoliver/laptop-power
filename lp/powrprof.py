# -*- coding: utf-8 -*-
r"""powrprof.dll 直调 —— 对标 G-Helper 的 PowerNative.cs

=============================================================================
为什么要它
=============================================================================
本程序原来所有电源计划读写都走 `powercfg.exe` 子进程：一次档位切换要几十次调用，
实测 20~40 秒，还每次都要 CREATE_NO_WINDOW 防幽灵终端窗。G-Helper 的做法是
**直接调 powrprof 的 C API**：进程内完成、微秒级、零子进程、零窗口。

更关键的是**能力**：`powercfg /q` 的文本输出解析不到的两个旋钮，用 API 一直都在——
  · PERFBOOSTMODE  睿频提升策略（0=禁用 1=启用 2=激进 3=高效启用 4=高效激进）
  · SYSCOOLPOL     系统散热方式（0=被动 1=主动）
⇒ 旧结论「本机电源计划只有 PROCTHROTTLEMIN/MAX 两个旋钮」是**解析限制，不是机器限制**，
  2026-10-05 用 API 逐条读出后翻案。同时读到的还有 处理器能源性能首选项策略(PERFEPP)、
  处理器性能核心放置最小核心数量 等共 95 项隐藏设置。

=============================================================================
安全性
=============================================================================
本模块只对**调用方传入的 scheme GUID** 生效；本程序只传自建计划
「笔记本自适应电源」，绝不碰系统自带「平衡」（硬性约束）。
"""
from __future__ import annotations

import ctypes
import uuid
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------- DLL 绑定

try:
    _pp = ctypes.WinDLL("powrprof", use_last_error=True)
except Exception:                                    # pragma: no cover
    _pp = None

try:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
except Exception:                                    # pragma: no cover
    _k32 = None


class GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                ("d4", ctypes.c_byte * 8)]

    def __str__(self) -> str:
        # 必须输出 Windows 规范形式 {d1-d2-d3-xx-yyyyyyyyyyyy}，
        # 否则与 config/注册表里的字符串比对会失败（第四段要再拆 2+6）
        d4 = bytes(self.d4)
        return "%08x-%04x-%04x-%s-%s" % (self.d1, self.d2, self.d3,
                                         d4[:2].hex(), d4[2:].hex())

    def __eq__(self, other) -> bool:
        return isinstance(other, GUID) and str(self) == str(other)

    def __hash__(self) -> int:
        return hash(str(self))


def g(s: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


def _bind(name: str, argtypes, restype=wintypes.DWORD):
    if _pp is None:
        return None
    try:
        f = getattr(_pp, name)
    except AttributeError:
        return None
    f.argtypes = argtypes
    f.restype = restype
    return f


_PG = ctypes.POINTER(GUID)
_GV = ctypes.c_void_p
_DW = ctypes.POINTER(wintypes.DWORD)
_BY = ctypes.POINTER(ctypes.c_byte)

_GetActiveScheme = _bind("PowerGetActiveScheme", [_GV, ctypes.POINTER(_GV)])
_SetActiveScheme = _bind("PowerSetActiveScheme", [_GV, _PG])
_ReadAC = _bind("PowerReadACValueIndex", [_GV, _PG, _PG, _PG, _DW])
_ReadDC = _bind("PowerReadDCValueIndex", [_GV, _PG, _PG, _PG, _DW])
_WriteAC = _bind("PowerWriteACValueIndex", [_GV, _PG, _PG, _PG, wintypes.DWORD])
_WriteDC = _bind("PowerWriteDCValueIndex", [_GV, _PG, _PG, _PG, wintypes.DWORD])
_Enum = _bind("PowerEnumerate", [_GV, _PG, _PG, wintypes.DWORD, wintypes.DWORD, _BY, _DW])
_Friendly = _bind("PowerReadFriendlyName", [_GV, _PG, _PG, _PG, _BY, _DW])
_GetEffOverlay = _bind("PowerGetEffectiveOverlayScheme", [_PG])
_GetActOverlay = _bind("PowerGetActualOverlayScheme", [_PG])
_SetOverlay = _bind("PowerSetActiveOverlayScheme", [_PG])
_LocalFree = None
if _k32 is not None:
    try:
        _LocalFree = _k32.LocalFree
        _LocalFree.argtypes = [_GV]
        _LocalFree.restype = _GV
    except AttributeError:                            # pragma: no cover
        _LocalFree = None

if _GetActiveScheme is not None and _LocalFree is None:   # pragma: no cover
    # 拿不到 LocalFree 就不启用（防止长期驻留进程内存泄漏）
    _GetActiveScheme = None


def available() -> bool:
    return _pp is not None and _GetActiveScheme is not None and _WriteAC is not None


# ---------------------------------------------------------------- 子组 / 设置项 GUID

SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
SUB_PCIEXPRESS = "501a4d13-42af-4429-9fd1-a8218c268e20"
SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
SUB_SYSTEM_BUTTON = "4f971e89-eebd-4455-a8de-9e59040e7347"
SUB_VIDEO = "7516b95f-f776-4464-8c53-06167f40cc99"
SUB_NONE = "fea3413e-7e05-4911-9a71-700331f1c294"

# 处理器电源管理里的关键旋钮（GUID → 别名）
ATTR: Dict[str, Tuple[str, str]] = {
    "PROCTHROTTLEMIN":  (SUB_PROCESSOR, "893dee8e-2bef-41e0-89c6-b55d0929964c"),
    "PROCTHROTTLEMAX":  (SUB_PROCESSOR, "bc5038f7-23e0-4960-96da-33abaf5935ec"),
    "PERFBOOSTMODE":    (SUB_PROCESSOR, "be337238-0d82-4146-a960-4f3749d470c7"),
    "PERFEPP":          (SUB_PROCESSOR, "36687f9e-e3a5-4dbf-b1dc-15eb381c6863"),
    "SYSCOOLPOL":       (SUB_PROCESSOR, "94d3a615-a899-4ac5-ae2b-e4d8f634367f"),
    "CPMINCORES":       (SUB_PROCESSOR, "0cc5b647-c1df-4637-891a-dec35c318583"),
    # 显示（GHelper 走 WmiMonitorBrightnessMethods；本机实测**改计划索引同样即时生效**，
    # 且能复用已打通的 powrprof 进程内 API —— 1.3ms vs powercfg 子进程 ~150ms）
    "VIDEONORMALLEVEL": (SUB_VIDEO, "aded5e82-b909-4619-9949-f5d71dac0bcb"),
    "VIDEOIDLE":        (SUB_VIDEO, "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e"),
    # 其它常用项
    "LIDACTION":        (SUB_SYSTEM_BUTTON, "5ca83367-6e45-459f-a27b-476b1d01c936"),
    "PCIE_ASPM":        (SUB_PCIEXPRESS, "ee12f906-d277-404b-b6da-e5fa1a576df5"),
    "CONNECTIVITY_STANDBY": (SUB_NONE, "f15576e8-98b7-4186-b944-eafa664402d9"),
}

# 语义映射（写值用）
BOOST_LABEL = {0: "禁用", 1: "启用", 2: "激进", 3: "高效启用", 4: "高效激进"}
COOL_LABEL = {0: "被动(先降频再提速风扇)", 1: "主动(先提速风扇)"}
# 合盖动作（LIDACTION，GHelper ClamshellModeControl.cs 语义）
LID_LABEL = {0: "不采取任何操作", 1: "睡眠", 2: "休眠", 3: "关机"}
LID_NOTHING, LID_SLEEP, LID_HIBERNATE, LID_SHUTDOWN = 0, 1, 2, 3

# Windows「电源模式」滑块（overlay）
OVERLAY_SILENT = "961cc777-2547-4f9d-8174-7d86181b8a7a"
OVERLAY_BALANCED = "00000000-0000-0000-0000-000000000000"
OVERLAY_TURBO = "ded574b5-45a0-4f42-8737-46345c09c238"
OVERLAY_LABEL = {
    OVERLAY_SILENT: "最佳能效",
    OVERLAY_BALANCED: "平衡",
    OVERLAY_TURBO: "最佳性能",
}


# ---------------------------------------------------------------- 计划读写

def active_scheme() -> Optional[str]:
    """当前活动电源计划 GUID（进程内，无子进程）。"""
    if not available():
        return None
    p = _GV()
    try:
        if _GetActiveScheme(None, ctypes.byref(p)) != 0 or not p.value:
            return None
        gg = ctypes.cast(p, _PG).contents
        return str(gg)
    except Exception:
        return None
    finally:
        if p.value:
            try:
                _LocalFree(p)
            except Exception:
                pass


def set_active(scheme: str) -> bool:
    if not available():
        return False
    try:
        return _SetActiveScheme(None, ctypes.byref(g(scheme))) == 0
    except Exception:
        return False


def read_value(scheme: str, sub: str, setting: str, on_battery: bool = False
               ) -> Optional[int]:
    if not available():
        return None
    sg, st = g(sub), g(setting)
    sc = g(scheme)
    v = wintypes.DWORD(0)
    try:
        f = _ReadDC if on_battery else _ReadAC
        if f(None, ctypes.byref(sc), ctypes.byref(sg), ctypes.byref(st), ctypes.byref(v)) != 0:
            return None
        return int(v.value)
    except Exception:
        return None


def write_value(scheme: str, sub: str, setting: str, value: int,
                on_battery: bool = False) -> bool:
    """只写索引，不激活计划（调用方随后 set_active 一次即可生效）。"""
    if not available():
        return False
    sg, st, sc = g(sub), g(setting), g(scheme)
    try:
        f = _WriteDC if on_battery else _WriteAC
        return f(None, ctypes.byref(sc), ctypes.byref(sg), ctypes.byref(st),
                 wintypes.DWORD(int(value) & 0xFFFFFFFF)) == 0
    except Exception:
        return False


def read_attr(scheme: str, attr: str, on_battery: bool = False) -> Optional[int]:
    ent = ATTR.get(attr)
    if not ent:
        return None
    return read_value(scheme, ent[0], ent[1], on_battery)


def write_attr(scheme: str, attr: str, value: int, on_battery: bool = False) -> bool:
    ent = ATTR.get(attr)
    if not ent:
        return False
    return write_value(scheme, ent[0], ent[1], value, on_battery)


_RANGE: Dict[str, Tuple[int, int]] = {
    "PROCTHROTTLEMIN": (0, 100),
    "PROCTHROTTLEMAX": (0, 100),
    "PERFBOOSTMODE": (0, 4),
    "PERFEPP": (0, 100),
    "SYSCOOLPOL": (0, 1),
    "VIDEONORMALLEVEL": (0, 100),
    "VIDEOIDLE": (0, 3600),
    "LIDACTION": (0, 3),
}


def clamp_attr(attr: str, value: int):
    """按已知范围夹取；未知旋钮原样返回。→ 夹取后的值（越界拒绝时返回 None 由调用方决定）"""
    rng = _RANGE.get(attr)
    if not rng:
        return int(value)
    return max(rng[0], min(rng[1], int(value)))


def write_range(scheme: str, attr: str, value: int,
                on_battery: bool = False) -> bool:
    """越界即拒绝（仅对已知范围的旋钮做保护），返回是否真的写过。"""
    rng = _RANGE.get(attr)
    if rng and not (rng[0] <= int(value) <= rng[1]):
        return False
    return write_attr(scheme, attr, value, on_battery)


def write_verified(scheme: str, attr: str, value: int,
                   on_battery: bool = False) -> bool:
    """写入并**回读校验**（GHelper 的 DeviceSet 也是靠回读判定被服务托管）。

    ATKACPI 的教训：写返回"成功"不代表真的生效（风扇曲线/充电阈值/PPT 全是
    写返回 1、回读原值）。凡是有替代通道的关键旋钮，都用这个函数确认。
    """
    v = clamp_attr(attr, value)
    if not write_attr(scheme, attr, v, on_battery):
        return False
    got = read_attr(scheme, attr, on_battery)
    return got is not None and int(got) == int(v)


# ---------------------------------------------------------------- 枚举

ACCESS_SUBGROUP = 17
ACCESS_SETTING = 18


def _enumerate(scheme: str, sub: Optional[str], access: int) -> List[str]:
    if not available() or _Enum is None:
        return []
    sc = g(scheme)
    sg = ctypes.byref(g(sub)) if sub else None
    out: List[str] = []
    i = 0
    while i < 512:
        size = wintypes.DWORD(16)
        buf = (ctypes.c_byte * 16)()
        try:
            rc = _Enum(None, ctypes.byref(sc), sg, access, i, buf, ctypes.byref(size))
        except Exception:
            break
        if rc != 0:
            break
        out.append(str(ctypes.cast(buf, _PG).contents))
        i += 1
    return out


def _friendly(scheme: str, sub: Optional[str], setting: Optional[str] = None) -> str:
    if not available() or _Friendly is None:
        return ""
    sc = g(scheme)
    sg = ctypes.byref(g(sub)) if sub else None
    st = ctypes.byref(g(setting)) if setting else None
    size = wintypes.DWORD(0)
    try:
        _Friendly(None, ctypes.byref(sc), sg, st, None, ctypes.byref(size))
        n = max(int(size.value), 4)
        buf = (ctypes.c_byte * n)()
        if _Friendly(None, ctypes.byref(sc), sg, st, buf, ctypes.byref(size)) != 0:
            return ""
        return bytes(buf)[:size.value].decode("utf-16le", "ignore").rstrip("\x00")
    except Exception:
        return ""


def subgroups(scheme: str) -> List[Tuple[str, str]]:
    return [(x, _friendly(scheme, x)) for x in _enumerate(scheme, None, ACCESS_SUBGROUP)]


def settings(scheme: str, sub) -> List[Tuple[str, str, Optional[int], Optional[int]]]:
    if isinstance(sub, (tuple, list)):      # 容忍 subgroups() 的返回值直接喂进来
        sub = sub[0]
    out = []
    for st in _enumerate(scheme, sub, ACCESS_SETTING):
        out.append((st, _friendly(scheme, sub, st),
                    read_value(scheme, sub, st, False),
                    read_value(scheme, sub, st, True)))
    return out


def probe_processor(scheme: str) -> Dict[str, Optional[int]]:
    """一次性读出自建计划里的关键 CPU 旋钮（给状态/面板用）。"""
    out: Dict[str, Optional[int]] = {}
    for k in ("PROCTHROTTLEMIN", "PROCTHROTTLEMAX", "PERFBOOSTMODE", "PERFEPP",
              "SYSCOOLPOL"):
        out[k] = read_attr(scheme, k, False)
    return out


# ---------------------------------------------------------------- 亮度 / 合盖

def brightness(scheme: str, on_battery: bool = False) -> Optional[int]:
    """计划里的显示器亮度索引（0~100）。"""
    return read_attr(scheme, "VIDEONORMALLEVEL", on_battery)


def set_brightness(scheme: str, value: int, on_battery: bool = False) -> bool:
    return write_range(scheme, "VIDEONORMALLEVEL", value, on_battery)


def lid_action(scheme: str, on_battery: bool = False) -> Optional[int]:
    """合盖动作：0=不动作 1=睡眠 2=休眠 3=关机（GHelper ClamshellModeControl）"""
    return read_attr(scheme, "LIDACTION", on_battery)


def set_lid_action(scheme: str, action: int, on_battery: bool = False) -> bool:
    return write_verified(scheme, "LIDACTION", action, on_battery)


# ---------------------------------------------------------------- 电源模式叠加层

def overlay() -> Optional[str]:
    """Windows「电源模式」滑块当前值（最佳能效 / 平衡 / 最佳性能）。"""
    if not available() or _GetEffOverlay is None:
        return None
    try:
        gg = GUID()
        if _GetEffOverlay(ctypes.byref(gg)) != 0:
            return None
        return str(gg)
    except Exception:
        return None


def overlay_label() -> str:
    v = overlay()
    return OVERLAY_LABEL.get(v or "", "未知")


def set_overlay(name: str) -> bool:
    """name: silent / balanced / turbo"""
    if not available() or _SetOverlay is None:
        return False
    guid = {"silent": OVERLAY_SILENT, "balanced": OVERLAY_BALANCED,
            "turbo": OVERLAY_TURBO}.get(name)
    if not guid:
        return False
    try:
        return _SetOverlay(ctypes.byref(g(guid))) == 0
    except Exception:
        return False


if __name__ == "__main__":      # pragma: no cover
    print("available:", available())
    act = active_scheme()
    print("active:", act)
    print("overlay:", overlay(), overlay_label())
    if act:
        for k, v in probe_processor(act).items():
            print("  %-18s = %s" % (k, v))
