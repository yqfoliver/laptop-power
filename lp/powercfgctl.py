# -*- coding: utf-8 -*-
"""
Windows 电源计划底层封装（只依赖 powercfg，无第三方库）

提供：
  - 枚举 / 复制 / 删除电源计划
  - 解析电源计划里【所有】可调项（含默认不显示的隐藏项），按别名索引
  - 按别名安全写入 AC / DC 值（机器上没有这个 Setting 就自动跳过）
"""
from __future__ import annotations

import re
import subprocess
import winreg
from typing import Dict, List, Optional

from . import powrprof as _pp

# powrprof 直调可用时，读/写/激活全部走进程内 API（快 400 倍、零子进程零窗口），
# powercfg 只作为兜底（比如 powrprof 被安全软件拦掉的情况）。
_PP_OK = False
try:
    _PP_OK = _pp.available()
except Exception:
    _PP_OK = False

# ---------------------------------------------------------------- 基础命令

def _run(args: List[str], timeout: int = 30) -> str:
    try:
        # CREATE_NO_WINDOW：pythonw 下不创建控制台窗口（否则每个 powercfg 调用都会
        # 在 Win11 默认终端(Windows Terminal)里泄漏一个空窗，越积越多）
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        raw = (p.stdout or b"") + (p.stderr or b"")
        for enc in ("utf-8", "gbk", "mbcs"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("gbk", "ignore")
    except Exception as e:  # pragma: no cover
        return ""


def run_powercfg(args: List[str]) -> str:
    return _run(["powercfg"] + args)


# ---------------------------------------------------------------- 查询解析

_SCHEME_RE = re.compile(r"电源方案\s*GUID:\s*([0-9a-fA-F\-]+)")
_GROUP_RE = re.compile(r"子组\s*GUID:\s*([0-9a-fA-F\-]+)")
_SETTING_RE = re.compile(r"电源设置\s*GUID:\s*([0-9a-fA-F\-]+)")
_ALIAS_RE = re.compile(r"GUID\s*别名:\s*(\S+)")
_CURRENT_RE = re.compile(r"当前(交流|直流)电源设置索引:\s*(0x[0-9a-fA-F]+)")
_MINMAX_RE = re.compile(r"(最小|最大)可能的设置:\s*(0x[0-9a-fA-F]+)")


class PowerSetting:
    """一个电源设置项"""

    def __init__(self, group_guid: str, group_alias: Optional[str],
                 guid: str, alias: Optional[str], name: str):
        self.group_guid = group_guid
        self.group_alias = (group_alias or "").upper()
        self.guid = guid
        self.alias = (alias or "").upper()
        self.name = name
        self.ac: Optional[int] = None
        self.dc: Optional[int] = None
        self.vmin: int = 0
        self.vmax: int = 0
        self.options: Dict[int, str] = {}   # 索引 -> 友好名

    @property
    def key(self) -> str:
        """设置项的唯一键（别名优先，其次是 GUID）"""
        return self.alias or self.guid

    def current(self, on_battery: bool) -> Optional[int]:
        return self.dc if on_battery else self.ac

    def __repr__(self) -> str:  # pragma: no cover
        return "<PowerSetting %s ac=%s dc=%s>" % (self.key, self.ac, self.dc)


class PowerPlan:
    """一个电源方案 + 它里面所有设置项"""

    def __init__(self, guid: str, name: str = ""):
        self.guid = guid
        self.name = name
        self.settings: Dict[str, PowerSetting] = {}

    def get(self, key: str) -> Optional[PowerSetting]:
        key = (key or "").upper()
        if not key:
            return None
        # 先按别名精确匹配，再兼容历史 GUID 键
        for s in self.settings.values():
            if s.alias == key:
                return s
            if s.guid.upper() == key:
                return s
            if s.key.upper() == key:
                return s
        return None

    def value(self, key: str, on_battery: bool) -> Optional[int]:
        s = self.get(key)
        return s.current(on_battery) if s else None

    def summary(self, on_battery: bool) -> Dict[str, Optional[int]]:
        return {k: s.current(on_battery) for k, s in self.settings.items()}


def parse_query(text: str) -> PowerPlan:
    """把 powercfg /query 的文本解析成 PowerPlan"""
    plan = PowerPlan("", "")
    m = _SCHEME_RE.search(text)
    if m:
        plan.guid = m.group(1)

    cur_group = ""
    cur_group_alias = ""
    cur = None                      # 当前 PowerSetting
    cur_name = ""

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("子组 GUID"):
            m = _GROUP_RE.search(line)
            if m:
                cur_group = m.group(1)
                cur_group_alias = ""
            m2 = _ALIAS_RE.search(line)
            if m2:
                cur_group_alias = m2.group(1)
            cur = None
            continue

        if line.startswith("电源设置 GUID"):
            m = _SETTING_RE.search(line)
            if not m:
                continue
            guid = m.group(1)
            name = line.split("(", 1)[1].rstrip(")").strip() if "(" in line else ""
            alias = None
            cur = PowerSetting(cur_group, cur_group_alias, guid, None, name)
            plan.settings[guid.upper()] = cur
            cur_name = name
            m2 = _ALIAS_RE.search(text[text.find(line):])  # 占位，后面统一扫
            continue

        # 别名紧跟在 GUID 行/子组行后面
        if line.startswith("GUID 别名") and cur is not None:
            m = _ALIAS_RE.search(line)
            if m:
                cur.alias = m.group(1).upper()
                plan.settings[cur.guid.upper()] = cur
            continue

        m = _CURRENT_RE.search(line)
        if m and cur is not None:
            val = int(m.group(2), 16)
            if m.group(1) == "交流":
                cur.ac = val
            else:
                cur.dc = val
            continue

        m = _MINMAX_RE.search(line)
        if m and cur is not None:
            v = int(m.group(2), 16)
            if m.group(1) == "最小":
                cur.vmin = v
            else:
                cur.vmax = v
            continue

        if line.startswith("可能的设置索引") and cur is not None:
            try:
                idx = int(line.split(":")[-1].strip())
            except ValueError:
                idx = -1
            nxt = ""
            for nxt in text.splitlines()[text.splitlines().index(raw) + 1:]:
                nxt = nxt.strip()
                if nxt.startswith("可能的设置友好名称"):
                    cur.options[idx] = nxt.split(":", 1)[-1].strip()
                    break
                if nxt.startswith("可能的设置索引") or not nxt:
                    break
            continue

    return plan


# powercfg /query 只暴露 30 项，CPU 睿频策略等在它眼里是"隐藏项"，
# 但 powrprof 进程内 API 读得到、写得动（GHelper 的 Turbo Boost 档就是这一项）。
# 这里按 GUID 补齐，让它们能和普通旋钮一样走档位写入（含回读校验与失败停用）。
HIDDEN_SETTINGS = {
    "PERFBOOSTMODE": ("be337238-0d82-4146-a960-4f3749d470c7",
                      "处理器性能提升模式", 0, 4),
    "SYSCOOLPOL":    ("94d3a615-a899-4ac5-ae2b-e4d8f634367f",
                      "系统散热策略", 0, 1),
}


def _fill_hidden(plan: PowerPlan) -> None:
    """把 powrprof 能读到、powercfg 查不到的隐藏项补进计划"""
    if not _PP_OK or not plan.guid:
        return
    for alias, (guid, name, vmin, vmax) in HIDDEN_SETTINGS.items():
        if plan.get(alias) or plan.get(guid):
            continue
        ac = _pp.read_value(plan.guid, _pp.SUB_PROCESSOR, guid, False)
        dc = _pp.read_value(plan.guid, _pp.SUB_PROCESSOR, guid, True)
        if ac is None and dc is None:
            continue                       # 本机不支持，别塞空项进去
        s = PowerSetting(_pp.SUB_PROCESSOR, "SUB_PROCESSOR", guid, alias, name)
        s.ac, s.dc, s.vmin, s.vmax = ac, dc, vmin, vmax
        plan.settings[alias] = s
        plan.settings[guid] = s


def load_plan(guid: str) -> PowerPlan:
    text = run_powercfg(["/query", guid])
    plan = parse_query(text)
    if not plan.guid:
        plan.guid = guid
    try:
        _fill_hidden(plan)
    except Exception:
        pass
    return plan


# ---------------------------------------------------------------- 计划管理

def active_scheme() -> str:
    if _PP_OK:
        g = _pp.active_scheme()
        if g:
            return g
    text = run_powercfg(["/getactivescheme"])
    m = _SCHEME_RE.search(text)
    return m.group(1) if m else ""


def live_value(scheme: str, group_guid: str, setting_guid: str,
               on_battery: bool) -> Optional[int]:
    """实时读某一项当前值（进程内 API；不可用时返回 None 由调用方走缓存）。"""
    if not _PP_OK:
        return None
    return _pp.read_value(scheme, group_guid, setting_guid, on_battery)


def list_schemes() -> Dict[str, str]:
    """返回 {guid: 名称}"""
    out: Dict[str, str] = {}
    text = run_powercfg(["/list"])
    g = None
    name = ""          # 上一轮抓到的计划名；首次循环 g 为 None，不会用到
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("电源方案 GUID"):
            m = _SCHEME_RE.search(line)
            if m and g:
                out[g] = name
            m2 = re.search(r"\((.+)\)", line)
            g = m.group(1) if m else None
            name = m2.group(1).strip() if m2 else ""
        if line.startswith("现有电源使用方案"):
            continue
    if g:
        out[g] = name
    return {k: v for k, v in out.items() if k}


def duplicate_scheme(src: str) -> Optional[str]:
    text = run_powercfg(["/duplicatescheme", src])
    return _SCHEME_RE.search(text).group(1) if _SCHEME_RE.search(text) else None


def delete_scheme(guid: str) -> bool:
    text = run_powercfg(["/deletescheme", guid])
    return "删除成功" in text or "成功" in text


def set_active(guid: str) -> bool:
    if _PP_OK and _pp.set_active(guid):
        return True
    text = run_powercfg(["/setactive", guid])
    return len(text.strip()) == 0 or "参数无效" not in text


def write_fast(scheme: str, alias: str, value: int, on_battery: bool = False) -> bool:
    """按别名直写（不走 load_plan 解析）。仅支持 powrprof ATTR 表里的项
    （PROCTHROTTLEMIN/MAX、PERFBOOSTMODE、PERFEPP、SYSCOOLPOL、亮度、合盖等）。
    给看门狗这类"绕过计划缓存、直接刷新内核参数"的场景用。"""
    if not _PP_OK:
        return False
    try:
        return _pp.write_attr(scheme, alias, int(value), on_battery)
    except Exception:
        return False


# 计划重命名（写注册表）
def rename_scheme(guid: str, name: str) -> bool:
    try:
        key = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as hk:
            with winreg.OpenKey(hk, guid, 0, winreg.KEY_SET_VALUE) as h:
                winreg.SetValueEx(h, "", 0, winreg.REG_SZ, name)
        return True
    except Exception:
        return False


def set_value(scheme: str, setting: PowerSetting, value: int, on_battery: bool) -> bool:
    """写入 AC 或 DC 值；成功返回 True

    优先走 powrprof 进程内 API（微秒级）；失败再退回 powercfg 子进程。
    注意：不用参数 `scheme` 去比对活动计划——调用方（manager）已经用
    `_scheme_active_ok()` 守过门，这里只负责"写进指定计划"。
    """
    if _PP_OK and _pp.write_value(scheme, setting.group_guid, setting.guid,
                                  int(value), on_battery):
        return True
    flag = "dc" if on_battery else "ac"
    text = run_powercfg([
        "/set%svalueindex" % flag, scheme, setting.group_guid, setting.guid, str(value)
    ])
    return "参数无效" not in text and "错误" not in text


def apply_values(scheme: str, values: Dict[str, int], on_battery: bool,
                 known: Optional[Dict[str, PowerSetting]] = None) -> List[str]:
    """按 {别名: 值} 写入。返回实际改动项的别名列表"""
    if known is None:
        known = load_plan(scheme).settings
    changed: List[str] = []
    for key, val in (values or {}).items():
        s = known.get(key)
        if s is None:
            continue
        if s.current(on_battery) == val:
            continue
        if s.vmax and val > s.vmax:
            val = s.vmax
        if s.vmin and val < s.vmin:
            val = s.vmin
        if set_value(scheme, s, val, on_battery):
            changed.append(key)
    return changed
