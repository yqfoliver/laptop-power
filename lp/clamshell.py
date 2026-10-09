# -*- coding: utf-8 -*-
r"""合盖不休眠（Clamshell / 蛤壳模式）—— 对标 G-Helper 的 ClamshellModeControl.cs

=============================================================================
为什么做
=============================================================================
笔记本接外接显示器当台式机用时（合盖 + 外接屏 + 插电），Windows 默认合盖
动作是「睡眠」⇒ 一合盖主机就睡，外接屏黑。G-Helper 的做法是把**当前电源计划**的
LIDACTION（合盖动作）临时改成 0（不采取任何操作），条件是「有外接显示器且已接电源」，
条件不再满足就还原成用户原本的设定。

G-Helper 原文（PowerNative.cs / ClamshellModeControl.cs）：
    EnableClamshellMode()  -> PowerNative.SetLidAction(0, ac=true)
    DisableClamshellMode() -> PowerNative.SetLidAction(保存的默认值, ac=true)
    IsClamshellReady()     -> 有外接屏 && (接电源 || 允许电池)
    默认值只认 2(休眠)/3(关机)，其它一律按 1(睡眠) —— 防止把「不动作」当成用户默认值

=============================================================================
本机实测（2026-10-06）
=============================================================================
  · LIDACTION GUID = 5ca83367-6e45-459f-a27b-476b1d01c936（SUB_SYSTEM_BUTTON）
  · 走 lp.powrprof 进程内 API 读写，**普通权限可写可回读**：
      写 0 -> 回读 0 -> 还原 1 -> 回读 1  ✓（实测）
  · 本机默认 = 1（睡眠）

=============================================================================
安全边界（严格遵守项目硬约束）
=============================================================================
  1. 只写**自建计划**（scheme_guid），绝不碰系统自带「平衡」；
  2. 只在自建计划**处于活动状态**时才动手（否则写了也不生效、还可能误改活动计划）；
  3. 进入前把原值持久化到 clamshell.json，退出/关闭开关/切到平衡档时逐项还原；
  4. 默认**关闭**（合盖行为是用户强预期，改成不动作可能让人以为坏了）。
"""
from __future__ import annotations

import ctypes
import json
import os
from typing import Dict, Optional

from . import powrprof as pp

# 合盖动作语义（与 powrprof.LID_LABEL 一致）
LID_NOTHING, LID_SLEEP, LID_HIBERNATE, LID_SHUTDOWN = 0, 1, 2, 3

SM_CMONITORS = 80


def _monitor_count() -> int:
    try:
        return int(ctypes.windll.user32.GetSystemMetrics(SM_CMONITORS))
    except Exception:
        return 1


class ClamshellCtl:
    """合盖不休眠控制。所有方法失败均不抛异常，只记录 note。"""

    def __init__(self, cfg: dict, scheme_guid: Optional[str] = None,
                 data_path: Optional[str] = None) -> None:
        self.cfg = cfg
        self.scheme = (scheme_guid or "").strip()
        self.data_path = data_path
        self._saved: Optional[Dict[str, int]] = None     # {"ac": n, "dc": n}
        self._engaged = False
        self._note = ""
        self._mon = 1
        self._load()

    # ------------------------------------------------------------ 配置 / 持久化
    def reload_cfg(self, cfg: dict) -> None:
        self.cfg = cfg
        self.scheme = (str(cfg.get("custom_scheme") or "")).strip()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("clamshell_enabled", False))

    @property
    def allow_battery(self) -> bool:
        return bool(self.cfg.get("clamshell_on_battery", False))

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def note(self) -> str:
        return self._note

    def _load(self) -> None:
        if not self.data_path or not os.path.isfile(self.data_path):
            return
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("saved"), dict):
                self._saved = {k: int(v) for k, v in d["saved"].items()
                               if k in ("ac", "dc")}
        except Exception:
            pass

    def _persist(self) -> None:
        if not self.data_path:
            return
        try:
            tmp = self.data_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"saved": self._saved or {},
                           "updated": __import__("time").strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.data_path)
        except Exception:
            pass

    # ------------------------------------------------------------ 探测
    @staticmethod
    def monitor_count() -> int:
        return _monitor_count()

    def external_display(self) -> bool:
        return self.monitor_count() > 1

    def ready(self, on_ac: bool) -> bool:
        """当前是否满足「该进蛤壳模式」的条件（对标 IsClamshellReady）"""
        if not self.external_display():
            return False
        return bool(on_ac or self.allow_battery)

    # ------------------------------------------------------------ 原值
    @staticmethod
    def _default_action() -> int:
        """G-Helper 口径：只把 2(休眠)/3(关机) 当作用户默认值，
        读到 0(不动作) 说明上次没还原干净 ⇒ 退回 1(睡眠)。

        本机实测默认就是 1(睡眠)，与之一致。
        """
        return LID_SLEEP

    def _save_origin(self) -> bool:
        if self._saved is not None:
            return True
        if not self.scheme:
            return False
        try:
            ac = pp.lid_action(self.scheme, False)
            dc = pp.lid_action(self.scheme, True)
        except Exception:
            ac = dc = None
        if ac is None and dc is None:
            return False
        self._saved = {}
        for k, v in (("ac", ac), ("dc", dc)):
            if v is None:
                continue
            # 0 = 上次没还原干净，按默认睡眠处理，避免把「不动作」固化成默认
            self._saved[k] = v if v in (LID_HIBERNATE, LID_SHUTDOWN, LID_SLEEP) \
                else self._default_action()
        if not self._saved:
            return False
        self._persist()
        return True

    def saved_action(self) -> Optional[Dict[str, int]]:
        return dict(self._saved) if self._saved else None

    # ------------------------------------------------------------ 主循环
    def tick(self, on_ac: bool, scheme_active: bool) -> Optional[str]:
        """返回本次发生的动作描述（None = 什么都没做）。幂等。"""
        if not pp.available():
            return None

        # 关闭开关 / 自建计划没生效 -> 一定要还原（否则用户会莫名其妙合不上盖）
        if not self.enabled or not scheme_active or not self.scheme:
            if self._engaged:
                self.reset(reason="条件不再满足，已还原合盖动作")
                return self._note
            return None

        self._mon = self.monitor_count()
        if self.ready(on_ac):
            if self._engaged:
                return None
            # ---- 进入 ----
            if not self._save_origin():
                self._note = "读不到原合盖动作，未启用蛤壳模式"
                return self._note
            ok_ac = pp.set_lid_action(self.scheme, LID_NOTHING, False)
            ok_dc = pp.set_lid_action(self.scheme, LID_NOTHING, True)
            if not (ok_ac or ok_dc):
                self._note = "写入合盖动作失败（计划只读？），未启用蛤壳模式"
                return self._note
            try:
                pp.set_active(self.scheme)      # 写索引后激活一次才生效
            except Exception:
                pass
            self._engaged = True
            self._note = ("蛤壳模式：外接显示器 + %s，合盖不睡眠"
                          % ("已接电源" if on_ac else "允许电池"))
            return self._note

        # ---- 条件不满足 ----
        if self._engaged:
            self.reset(reason="外接显示器已断开或条件不满足，已还原合盖动作")
            return self._note
        return None

    def reset(self, reason: str = "") -> bool:
        """还原合盖动作（无条件，可安全重复调用）"""
        changed = False
        if self._engaged and self.scheme and self._saved:
            for key, ac in (("ac", False), ("dc", True)):
                v = self._saved.get(key)
                if v is None:
                    continue
                try:
                    if pp.set_lid_action(self.scheme, int(v), ac):
                        changed = True
                except Exception:
                    pass
            if changed:
                try:
                    pp.set_active(self.scheme)
                except Exception:
                    pass
        self._engaged = False
        if reason:
            self._note = reason
        return changed

    # ------------------------------------------------------------ 状态
    def report(self) -> dict:
        def _cur(ac: bool):
            try:
                if self.scheme:
                    return pp.lid_action(self.scheme, ac)
            except Exception:
                pass
            return None
        return {
            "enabled": self.enabled,
            "on_battery_allowed": self.allow_battery,
            "engaged": self._engaged,
            "monitors": self._mon,
            "external": self.external_display(),
            "lid_ac": _cur(False),
            "lid_dc": _cur(True),
            "lid_ac_label": pp.LID_LABEL.get(_cur(False), "—"),
            "saved": self.saved_action(),
            "note": self._note,
        }

    def line(self) -> str:
        """面板/状态用的一行文字"""
        if not self.enabled:
            return "合盖不休眠：未开启"
        r = self.report()
        if self._engaged:
            return "合盖不休眠：已启用（外接屏 %d 个）" % r["monitors"]
        if not r["external"]:
            return "合盖不休眠：待命（未检测到外接显示器）"
        return "合盖不休眠：待命（需接电源，或允许电池使用）"


if __name__ == "__main__":      # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lp import config as _cfg
    c = _cfg.load()
    t = ClamshellCtl(c, c.get("custom_scheme"),
                     os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "clamshell.json"))
    print(json.dumps(t.report(), ensure_ascii=False, indent=1))
    print(t.line())
