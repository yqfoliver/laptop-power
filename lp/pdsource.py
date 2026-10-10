# -*- coding: utf-8 -*-
"""供电能力辨识：给「当前插着的这个充电器」单独建一份档案。

为什么需要这一层
----------------
电源上限**不是机器的属性，是「当前这个充电器」的属性**。同一个程序会
在 200W 原装适配器、100W PD、65W 氮化镓之间来回换，它们的能力差 3 倍。

旧实现把学到的值持久成一个全局常量（config.pd_supply_w）：在 200W 适配器上
学到 130W，拔掉换 100W 的 Type-C，那个 130W **不会作废** —— 控制器以为还有
130W 余量，全程不压制，电池一路放电。用户看到的现象就是「Type-C 打游戏
一直从电池取电」。

能不能直接读充电器的额定功率？
------------------------------
读不到。本机实测：UCSI 设备确实存在（`ACPI\\USBC000`，设备管理器里叫
「UCM-UCSI ACPI 设备」），但**用户态没有任何可用通道**：

- `root\\wmi` 下没有 UCSI / PD / Type-C 相关的任何类；
- MUTT 工具包里的 `UcsiControl.exe`（唯一能发 GET_PDOS 读充电器广播 PDO 的
  东西）本机不存在；
- 走 UCSI 命令要先开驱动测试接口：管理员改注册表 + 禁用/启用设备重启驱动
  —— 与本项目「零依赖、普通权限、完全静默」的底线直接冲突。

`GET_PDOS` 本来能拿到充电器广播的最高 PDO（就是额定能力），但这条路在普通
权限下封死。所以结论是：**额定功率读不到，只能测量**。

既然只能测量，那就必须解决「测量结果属于谁」的问题 —— 这就是本模块：
把测量值绑定到**供电会话**。

供电会话
--------
一次会话 = 从 AC 接入到 AC 断开。换充电器必然经历一次 AC 断开（拔了才换得
成），所以：

    AC 断开 ⇒ 会话结束 ⇒ 作废旧值，下次接入重新现测

冷启动（程序刚起来）时无法知道「我不在的时候有没有换过充电器」，所以只有
在「上次记录离得很近」（默认 10 分钟内，说明只是程序重启）时才沿用旧值，
否则一律当作新会话重新学。宁可多学几秒，也不能拿错电源的数字去赌。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from .config import APP_ROOT  # 数据文件跟 config.json 走同一个目录（onefile 下是 exe 目录）

STATE_PATH = os.path.join(APP_ROOT, "pd_supply.json")

VERSION = 1

# 冷启动时沿用上次测量值的时间窗（秒）。
# 超过这个间隔 = 程序（或机器）离开过很久，期间很可能换过充电器 ⇒ 不沿用。
COLD_REUSE_S = 600.0

# 归档最近多少次会话（面板显示「上次 130W / 本次 89W」用）
HISTORY_MAX = 5


class PdSource:
    """跟踪供电会话，并为每个会话单独记住测到的供电能力。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or STATE_PATH
        self.ac_prev: Optional[bool] = None     # 上一轮看到的 AC 状态
        self.session_start: Optional[float] = None   # 本次会话开始时间
        self.adopted = False                    # 当前值是「沿用上次」还是「本次实测」
        self.state: Dict[str, Any] = {"version": VERSION, "current": None,
                                      "history": []}
        self._load()

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("version") == VERSION:
                self.state = d
                self.state.setdefault("history", [])
                self.state.setdefault("current", None)
        except Exception:
            self.state = {"version": VERSION, "current": None, "history": []}

    def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ------------------------------------------------------------------ 会话
    def note_ac(self, ac: bool) -> str:
        """每轮调用一次，告知当前 AC 状态。

        返回：
            "new"  —— 这是一次新的供电会话（刚插上电），旧值必须作废
            "gone" —— AC 断开，会话结束
            "same" —— 会话延续，沿用当前值
        """
        prev = self.ac_prev
        self.ac_prev = bool(ac)
        if not ac:
            # 拔电：会话结束。归档当前值，并把 current 置空（下次接入重新学）
            if prev is not False:
                self._archive()
            self.session_start = None
            self.adopted = False
            return "gone"
        if prev is True:
            return "same"
        # prev 为 None（程序刚启动首次观测）或 False（刚插上电）
        self.session_start = time.time()
        self.adopted = False
        return "new"

    def _archive(self) -> None:
        cur = self.state.get("current")
        if not cur:
            return
        try:
            w = float(cur.get("watts"))
        except Exception:
            return
        if not (25.0 <= w <= 330.0):
            return
        h: List[Dict[str, Any]] = list(self.state.get("history") or [])
        # 拔电期间 note_ac 每轮都会走到这里，靠下面的 current=None 自然去重
        h.append({"watts": round(w, 1),
                  "firm": bool(cur.get("firm")),
                  "start_ts": cur.get("session_start"),
                  "end_ts": round(time.time(), 1)})
        self.state["history"] = h[-HISTORY_MAX:]
        self.state["current"] = None
        self._save()

    def session_age(self) -> Optional[float]:
        if not self.session_start:
            return None
        return time.time() - self.session_start

    # ------------------------------------------------------------------ 取值
    def adopt_on_start(self) -> Optional[float]:
        """冷启动能否沿用上次测到的值。

        只有「上次记录离现在很近」才沿用 —— 间隔一长就说明程序/机器离开过，
        期间很可能换过充电器，拿旧数字当真值正是旧实现的事故成因。
        """
        a = self.adopt()
        return None if a is None else a["watts"]

    def adopt(self) -> Optional[Dict[str, Any]]:
        """同上，但连 `firm` 一起返回。

        firm 必须跟着走：上次会话若被放电事件精确标定过（firm=True），这个值
        就是真上限，冷启动后可以直接拿来做「提前限帧」判据；若只是充电下界
        （firm=False），它只能证明「电源至少这么大」，拿它做预判会在弱电源上
        把日常轻载误判成快供不上 → 限帧降刷，纯误伤。
        """
        cur = self.state.get("current")
        if not cur:
            return None
        try:
            w = float(cur.get("watts"))
            ts = float(cur.get("ts", 0))
        except Exception:
            return None
        if not (25.0 <= w <= 330.0):
            return None
        if ts <= 0 or (time.time() - ts) > COLD_REUSE_S:
            return None
        self.adopted = True
        # 沿用也要重新确认：交给放电/充电分支继续修正
        return {"watts": w, "firm": bool(cur.get("firm"))}

    def save(self, watts: Optional[float], firm: bool = False) -> None:
        """记录本次会话实测到的供电能力（限频由调用方控制）。"""
        if watts is None:
            return
        try:
            w = float(watts)
        except Exception:
            return
        if not (25.0 <= w <= 330.0):
            return
        self.adopted = False
        self.state["current"] = {"watts": round(w, 1),
                                 "firm": bool(firm),
                                 "ts": round(time.time(), 1),
                                 "session_start": (round(self.session_start, 1)
                                                   if self.session_start else None)}
        self._save()

    def forget(self) -> None:
        """作废旧值（换了充电器 / 读数明显不对）。"""
        self.state["current"] = None
        self.adopted = False
        self._save()

    # ------------------------------------------------------------------ 展示
    def history_watts(self) -> List[float]:
        out: List[float] = []
        for h in (self.state.get("history") or []):
            try:
                out.append(round(float(h.get("watts")), 1))
            except Exception:
                pass
        return out

    def summary(self) -> Dict[str, Any]:
        cur = self.state.get("current") or {}
        return {
            "watts": cur.get("watts"),
            "firm": bool(cur.get("firm")),
            "adopted": self.adopted,
            "session_s": (round(self.session_age(), 1)
                          if self.session_age() is not None else None),
            "history": self.history_watts(),
        }
