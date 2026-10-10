# -*- coding: utf-8 -*-
"""静默底线下的可诊断性：把吞掉的异常写进日志文件。

为什么需要它
------------
程序对外是**完全静默**的：不弹窗、不黑窗、不气泡，巡检主循环用
``except Exception: pass`` 兜住一切。这在正常使用下是对的，但一旦某个子系统
开始每轮抛异常（驱动升级、权限变化、接口消失），表现是「功能悄悄停了」，
而日志一片空白 —— 既没法自查，也没法远程帮用户定位。

所以补一层只写文件的错误日志：
  · 默认开启，只落盘，**不弹任何东西**（不破坏静默底线）
  · **去重**：同样的「位置+异常类型+消息」在 5 分钟内只记一次并累加次数，
    否则每 5 秒一行会把日志刷爆
  · **限长**：超过 ~256 KB 自动截断留后半段（最新的一定在）
  · 所有写入失败都吞掉：日志本身绝不能影响主程序
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional, Tuple

NAME = "error.log"
MAX_BYTES = 256 * 1024
DEDUP_S = 300.0          # 同一错误 5 分钟内只写一次（后续只累加次数）
KEEP_ON_TRIM = 64 * 1024  # 超长时保留最后这么多字节

_lock = threading.Lock()
_path: Optional[str] = None
_recent: Dict[Tuple[str, str, str], list] = {}   # key -> [首次时间, 上次时间, 次数, 行号]
_enabled = True
_seq = 0


def set_path(path: str) -> None:
    global _path
    _path = path


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)


def _file() -> Optional[str]:
    if _path:
        return _path
    try:
        from .config import APP_ROOT
        return os.path.join(APP_ROOT, NAME)
    except Exception:
        return os.path.join(os.path.expanduser("~"), NAME)


def log(scope: str, exc: BaseException) -> None:
    """记一次异常（去重 + 限长）。scope 形如 "loop" / "pd" / "alloc"。"""
    if not _enabled:
        return
    try:
        key = (str(scope), type(exc).__name__,
               str(exc)[:160].replace("\n", " "))
    except Exception:
        return
    now = time.time()
    try:
        with _lock:
            rec = _recent.get(key)
            if rec is not None and now - rec[1] < DEDUP_S:
                rec[2] += 1
                rec[1] = now
                # 次数变化时补一行汇总，避免「只有一条、看不出还在发生」
                if rec[2] in (10, 50, 200, 1000):
                    _write("[%s] %s | %s: %s | 已重复 %d 次"
                           % (time.strftime("%m-%d %H:%M:%S"), scope,
                              type(exc).__name__, str(exc)[:120], rec[2]))
                return
            _recent[key] = [now, now, 1, 0]
            if len(_recent) > 200:                     # 防止 key 无限膨胀
                for k in [k for k, v in _recent.items()
                          if now - v[1] > DEDUP_S * 4][:100]:
                    _recent.pop(k, None)
            _write("[%s] %s | %s: %s"
                   % (time.strftime("%m-%d %H:%M:%S"), scope,
                      type(exc).__name__, str(exc)[:200].replace("\n", " ")))
    except Exception:
        pass


def _write(line: str) -> None:
    try:
        p = _file()
        if not p:
            return
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        # 限长：太大就只留后半段（最新的一定在）
        try:
            if os.path.isfile(p) and os.path.getsize(p) > MAX_BYTES:
                with open(p, "rb") as f:
                    f.seek(-KEEP_ON_TRIM, os.SEEK_END)
                    tail = f.read()
                with open(p, "w", encoding="utf-8", errors="ignore") as f:
                    f.write("...(略去较早内容)\n")
                    f.write(tail.decode("utf-8", "ignore"))
        except Exception:
            pass
        with open(p, "a", encoding="utf-8", errors="ignore") as f:
            f.write(line + "\n")
    except Exception:
        pass


def tail(n: int = 40) -> str:
    """给 --selftest / 面板用：看最近几行。"""
    try:
        p = _file()
        if not p or not os.path.isfile(p):
            return ""
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""
