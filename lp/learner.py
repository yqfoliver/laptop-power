# -*- coding: utf-8 -*-
"""
自学习引擎 —— 用得越久，越贴合这台机器和这个人的习惯。

学习四条线：
  1) 进程画像：你手动给某个程序选过档位 = 投一票；
     同一程序攒够 3 票倾向同一档，就记成规则，以后它一开就直接按你的选择走。
  2) 抖动抑制：某个档位老是被几秒内切走，说明判定太急；
     程序会自动把该档位的「连续命中次数」门槛调高，减少来回切档。
  3) 参数偏好：你在面板里手动改过的旋钮值会被记住，
     下次进同一档位直接沿用你的值（并统计你偏上调还是下调）。
  4) 时间习惯：按「星期几 + 时段 + 插电与否」统计主用档位，
     样本够多时给出提醒（只建议，不强制）。

外加两条自愈：
  5) 反反复复写不进去的旋钮自动停用，不再每次轮询都白试一遍。
  6) 你手动选择 ≠ 自动判定结果时，作为负反馈，加速第 1) 条收敛。

所有数据写在 learn.json，随时可一键清空回到出厂判定。
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional


def fresh() -> Dict[str, Any]:
    """每次都要一份全新的默认结构（不能用浅拷贝，否则各实例会共用同一个嵌套字典）"""
    return copy.deepcopy(DEFAULT)

APP_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEARN_PATH = os.path.join(APP_ROOT, "learn.json")

MIN_CONF = 3            # 成规则所需票数
DWELL_MIN_N = 4         # 样本够了才动抖动阈值
SHORT_SEC = 30          # 平均停留低于此秒数视为「切太急」
LONG_SEC = 1500         # 平均停留高于此秒数视为「可以更快响应」
MAX_CONFIRM = 4
FAIL_LIMIT = 3          # 同一旋钮连续失败这么多次 -> 停用
# 停用保质期（秒）：失败多半是**瞬时**的（电源紧张时 powercfg 子进程超时、
# 驱动忙）。旧实现一旦停用就永久生效（只能手动 revive），等于一次抖动让某个
# 档位的某个能力永远消失。改成记时刻 + 到期自动解禁，成功路径不受影响。
DISABLE_TTL_S = 3 * 86400.0

DEFAULT = {
    "version": 1,
    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    "updated": "",
    "uptime_seconds": 0,     # 累计观察秒数
    "switches": 0,           # 累计切换次数
    "procs": {},             # exe -> {seen, last, votes:{mode:n}}
    "rules": {},             # exe -> {mode, conf, ac, t}
    "dwell": {},             # mode -> {n, sec}
    "confirm": {},           # mode -> 连续命中次数
    "pref": {},              # mode -> {knob: 值}
    "pref_dir": {},          # mode -> {knob: -1/0/+1}
    "fail": {},              # knob -> 失败次数
    "disabled": {},          # mode -> [knob]
    "habit": {},             # "1_afternoon_dc" -> {mode: n}
    "history": [],           # 最近 150 条
}


def _atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".lrn", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if os.path.exists(path):
            try:
                import shutil
                shutil.copy2(path, path + ".bak")
            except Exception:
                pass
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


class Learner:
    def __init__(self, path: str = LEARN_PATH):
        self.path = path
        self.data: Dict[str, Any] = fresh()
        self._lock = threading.RLock()
        self._dirty = False
        self._last_save = 0.0
        self.load()

    # ------------------------------------------------------------ 读写
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                merged = fresh()
                for k, v in d.items():
                    # 只合并键，值一律取磁盘上的（避免继承默认结构里的可变对象）
                    if isinstance(v, dict) and isinstance(merged.get(k), dict):
                        merged[k] = v
                    else:
                        merged[k] = v
                self.data = merged
        except Exception:
            pass

    def save(self, force: bool = False) -> None:
        with self._lock:
            now = time.time()
            if not force and now - self._last_save < 5:
                self._dirty = True
                return
            try:
                self.data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2))
                self._last_save = now
                self._dirty = False
            except Exception:
                pass

    def flush(self) -> None:
        self.save(force=True)

    # ------------------------------------------------------------ 1) 进程画像
    def observe(self, exe: str, mode: str, ac: bool) -> None:
        """每次场景判定调用；exe 变化时才计一次出现"""
        if not exe:
            return
        with self._lock:
            p = self.data["procs"].setdefault(exe, {"seen": 0, "last": 0, "votes": {}})
            p["seen"] = int(p.get("seen", 0)) + 1
            p["last"] = time.time()
            d = self.data["dwell"].get(mode)
            self.data["uptime_seconds"] = int(self.data.get("uptime_seconds", 0)) + 1
            self._habit(mode, ac)
            self._log(exe, mode, ac, None)
            self.save()

    def vote(self, exe: str, mode: str, ac: bool) -> Optional[str]:
        """
        用户手动选了档位 -> 记一票，必要时更新规则。
        返回新生成的规则（若有），供上层弹提示。
        """
        if not exe or mode not in ("gaming", "battery", "saver", "office", "balanced"):
            return None
        with self._lock:
            p = self.data["procs"].setdefault(exe, {"seen": 0, "last": 0, "votes": {}})
            v = p.setdefault("votes", {})
            v[mode] = int(v.get(mode, 0)) + 1

            old = self.data["rules"].get(exe)
            top = max(v.items(), key=lambda kv: kv[1])
            top_mode, top_n = top
            second = sorted(v.values())[-2] if len(v) > 1 else 0
            # 平票保护：票数分散（如 3/3）时要求多攒一票，避免「谁先到 3 谁成规则」
            need = MIN_CONF if top_n > second else MIN_CONF + 1

            rule = self.data["rules"].get(exe)
            if top_n >= need:
                if not rule or rule.get("mode") != top_mode or rule.get("ac") != ac:
                    self.data["rules"][exe] = {
                        "mode": top_mode, "conf": top_n, "ac": ac,
                        "t": time.time(),
                    }
                    self._log(exe, top_mode, ac, "learn")
                    self.save(force=True)
                    return top_mode
            self.save()
        return None

    def rule_for(self, exe: str, ac: bool) -> Optional[str]:
        """这个程序以前被你安排过哪个档位"""
        if not exe:
            return None
        with self._lock:
            r = self.data["rules"].get(exe)
            if not r:
                return None
            if r.get("ac", ac) != ac:
                return None
            return r.get("mode") if int(r.get("conf", 0)) >= MIN_CONF else None

    def unlearn(self, exe: str) -> bool:
        with self._lock:
            if exe in self.data["procs"]:
                self.data["procs"].pop(exe, None)
            self.data["rules"].pop(exe, None)
            self.save(force=True)
            return True

    # ------------------------------------------------------------ 2) 抖动抑制
    def feed(self, mode: str, seconds: float) -> None:
        """某档位应用了N秒后被调用，用来推算判定是不是太急"""
        if mode not in self.data["dwell"]:
            pass
        with self._lock:
            d = self.data["dwell"].setdefault(mode, {"n": 0, "sec": 0})
            d["n"] = int(d.get("n", 0)) + 1
            d["sec"] = float(d.get("sec", 0)) + max(0.0, seconds or 0)
            n = d["n"]
            if n >= DWELL_MIN_N:
                avg = d["sec"] / max(1, n)
                cur = int(self.data["confirm"].get(mode, 1))
                nxt = cur
                if avg < SHORT_SEC:
                    nxt = min(MAX_CONFIRM, cur + 1)
                elif avg > LONG_SEC and cur > 1:
                    nxt = cur - 1
                if nxt != cur:
                    self.data["confirm"][mode] = nxt
                    self.save(force=True)
                    return
            self.save()

    def confirm_needed(self, mode: str) -> int:
        """该档位需要连续命中几次轮询才真切（默认 1 次 = 立即切）"""
        with self._lock:
            return max(1, min(MAX_CONFIRM, int(self.data["confirm"].get(mode, 1))))

    def set_confirm(self, mode: str, n: int) -> None:
        with self._lock:
            self.data["confirm"][mode] = max(1, min(MAX_CONFIRM, int(n)))
            self.save(force=True)

    # ------------------------------------------------------------ 3) 参数偏好
    def set_pref(self, mode: str, knob: str, value: int, old: Optional[int] = None) -> None:
        """用户在面板里改了旋钮 -> 记下这个值"""
        with self._lock:
            pm = self.data["pref"].setdefault(mode, {})
            pm[knob] = int(value)
            if old is not None and old != value:
                dm = self.data["pref_dir"].setdefault(mode, {})
                dm[knob] = 1 if value > old else -1
            self.save(force=True)

    def knob_value(self, mode: str, knob: str, default: int) -> int:
        with self._lock:
            v = self.data["pref"].get(mode, {}).get(knob)
            return int(v) if v is not None else int(default)

    def clear_pref(self, mode: Optional[str] = None) -> None:
        with self._lock:
            if mode:
                self.data["pref"].pop(mode, None)
                self.data["pref_dir"].pop(mode, None)
            else:
                self.data["pref"] = {}
                self.data["pref_dir"] = {}
            self.save(force=True)

    def clear_knob_pref(self, mode: str, knob: str) -> None:
        """只清掉某个档位某个旋钮的「记忆」（面板上的 ⟲ 按钮）"""
        with self._lock:
            pm = self.data.get("pref", {}).get(mode)
            if isinstance(pm, dict):
                pm.pop(knob, None)
            dm = self.data.get("pref_dir", {}).get(mode)
            if isinstance(dm, dict):
                dm.pop(knob, None)
            self.save(force=True)

    # ------------------------------------------------------------ 4) 时间习惯
    def _habit(self, mode: str, ac: bool) -> None:
        t = time.localtime()
        bucket = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][t.tm_wday]
        hour = t.tm_hour
        span = "凌晨" if hour < 6 else "上午" if hour < 12 else "下午" if hour < 18 else "晚上"
        key = "%s_%s_%s" % (bucket, span, "ac" if ac else "dc")
        h = self.data["habit"].setdefault(key, {})
        h[mode] = int(h.get(mode, 0)) + 1
        if len(self.data["habit"]) > 120:      # 防止无限膨胀
            for k in list(self.data["habit"])[:40]:
                self.data["habit"].pop(k, None)

    def habit_hint(self, ac: Optional[bool] = None) -> Optional[str]:
        """当前时段你历史最喜欢的档位（样本够多才说）"""
        t = time.localtime()
        bucket = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][t.tm_wday]
        hour = t.tm_hour
        span = "凌晨" if hour < 6 else "上午" if hour < 12 else "下午" if hour < 18 else "晚上"
        if ac is None:
            ac = self.data.get("_ac_hint")
        key = "%s_%s_%s" % (bucket, span, "ac" if ac else "dc")
        h = self.data["habit"].get(key) or {}
        if len(h) < 5:
            return None
        mode, n = max(h.items(), key=lambda kv: kv[1])
        return {"mode": mode, "n": n, "key": key} if n >= 5 else None

    # ------------------------------------------------------------ 5) 失败自愈
    def note_fail(self, mode: str, knob: str) -> None:
        with self._lock:
            self.data["fail"][knob] = int(self.data["fail"].get(knob, 0)) + 1
            if self.data["fail"][knob] >= FAIL_LIMIT:
                d = self.data["disabled"].setdefault(mode, {})
                if isinstance(d, list):          # 旧格式（list）平滑迁移
                    d = {k: time.time() for k in d}
                    self.data["disabled"][mode] = d
                if knob not in d:
                    d[knob] = time.time()
            self.save()

    def is_disabled(self, mode: str, knob: str) -> bool:
        with self._lock:
            d = self.data["disabled"].get(mode) or {}
            if isinstance(d, list):     # 旧存档：沿用旧语义（等手动 revive）
                return knob in d
            ts = d.get(knob)
            if ts is None:
                return False
            if time.time() - float(ts) > DISABLE_TTL_S:
                d.pop(knob, None)       # 到期自动解禁：给旋钮一次重新证明的机会
                try:
                    self.save()
                except Exception:
                    pass
                return False
            return True

    def revive_all(self) -> None:
        with self._lock:
            self.data["disabled"] = {}
            self.data["fail"] = {}
            self.save(force=True)

    # ------------------------------------------------------------ 流水
    def _log(self, exe: str, mode: str, ac: bool, src: Optional[str]) -> None:
        h = self.data.get("history") or []
        h.append({"t": time.strftime("%m-%d %H:%M:%S"), "exe": exe,
                  "mode": mode, "ac": bool(ac), "src": src or ""})
        if len(h) > 150:
            del h[:len(h) - 150]
        self.data["history"] = h

    def note_switch(self, exe: str, mode: str, ac: bool) -> None:
        with self._lock:
            self.data["switches"] = int(self.data.get("switches", 0)) + 1
            self._log(exe, mode, ac, "auto")
            self.save()

    # ------------------------------------------------------------ 汇总
    def report(self, ac: Optional[bool] = None) -> Dict[str, Any]:
        with self._lock:
            d = self.data
            rules = []
            for exe, r in sorted(d.get("rules", {}).items(),
                                 key=lambda kv: -int(kv[1].get("conf", 0))):
                rules.append({"exe": exe, "mode": r.get("mode"),
                              "conf": int(r.get("conf", 0)),
                              "ac": bool(r.get("ac", True))})
            dwell = []
            for mode, v in sorted(d.get("dwell", {}).items()):
                n = int(v.get("n", 0)) or 1
                dwell.append({"mode": mode, "n": int(v.get("n", 0)),
                              "avg": int(v.get("sec", 0) / n),
                              "confirm": self.confirm_needed(mode)})
            pref = {m: {k: int(v) for k, v in (kk or {}).items()}
                    for m, kk in (d.get("pref") or {}).items()}
            upd = int(d.get("uptime_seconds", 0))
            return {
                "created": d.get("created", ""),
                "updated": d.get("updated", ""),
                "hours": round(upd / 3600.0, 2),
                "switches": int(d.get("switches", 0)),
                "known_procs": len(d.get("procs", {})),
                "rules": rules,
                "dwell": dwell,
                "pref": pref,
                "pref_dir": d.get("pref_dir") or {},
                "disabled": {m: list(v) for m, v in (d.get("disabled") or {}).items()},
                "confirm": dict(d.get("confirm") or {}),
                "history": list(d.get("history") or [])[-20:],
                "habit": self.habit_hint(ac),
            }

    # ------------------------------------------------------------ 重置
    def reset(self) -> None:
        with self._lock:
            try:
                if os.path.exists(self.path):
                    os.replace(self.path, self.path + ".old")
            except Exception:
                pass
            self.data = fresh()
            self.save(force=True)
