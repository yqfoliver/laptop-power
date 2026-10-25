# -*- coding: utf-8 -*-
"""G-Helper 借鉴：日志「概率自清理」。

背景
----
程序对外完全静默，但有几个只写文件的诊断日志会在长时间运行中无限增长：

  · webui_access.log  面板每次页面加载一行（HTTP 服务是多线程的）
  · wv2_child.log     面板子进程的生命周期事件
  · panel_ui_err.log  绘制异常堆栈

它们原先都用最朴素的 ``open(path, "a")`` 追加、没有任何上限，跑久了能涨到
数 MB（本机实测 webui_access.log 3.7MB、wv2_child.log 2.9MB）。

G-Helper 的做法很轻：每次写日志时，以极小概率（约 1%）把文件截断到末尾
最近 N 行。这样**不必每次写都去 stat / 读全文件**，长期又把体积压住了。
我们照抄这个思路，并额外加一道「硬性体积兜底」：超过 ``max_bytes`` 就必裁，
避免纯概率方案在两次裁切之间无上限、极端情况下短期爆量。

线程安全：追加与裁切都在同一把锁内完成，多线程 HTTP 也不会交叉损坏。
"""
from __future__ import annotations

import os
import random
import threading

DEFAULT_KEEP_LINES = 2000
DEFAULT_PROB = 0.01                # G-Helper 同款：每次写约 1% 概率裁切
DEFAULT_MAX_BYTES = 512 * 1024     # 硬性体积兜底：超 512KB 必裁

_lock = threading.Lock()


def log_line(path: str, line: str,
             keep_lines: int = DEFAULT_KEEP_LINES,
             prob: float = DEFAULT_PROB,
             max_bytes: int = DEFAULT_MAX_BYTES) -> None:
    """原子地：追加一行 +（小概率 / 超体积时）截断到末尾 keep_lines 行。

    ``line`` 不含末尾换行，本函数统一补 ``\\n``。所有失败都吞掉——
    日志清理绝不能影响主程序。
    """
    try:
        with _lock:
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            do_trim = False
            try:
                if max_bytes and os.path.isfile(path) \
                        and os.path.getsize(path) > max_bytes:
                    do_trim = True          # 硬性兜底：超体积必裁
                elif random.random() < prob:
                    do_trim = True          # G-Helper 风格：小概率裁切
            except OSError:
                do_trim = False
            if do_trim:
                _trim_inplace(path, keep_lines)
            with open(path, "a", encoding="utf-8", errors="ignore") as f:
                f.write(line + "\n")
    except Exception:
        pass


def _trim_inplace(path: str, keep_lines: int) -> None:
    """读全文件、保留末尾 keep_lines 行、原地覆盖。调用方需持 _lock。"""
    try:
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) <= keep_lines:
            return
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            f.write("...(略去较早内容，保留最近 %d 行)\n" % keep_lines)
            f.writelines(lines[-keep_lines:])
    except Exception:
        pass


def trim_now(path: str, keep_lines: int = DEFAULT_KEEP_LINES) -> None:
    """手动触发一次裁切（如部署后清掉历史大日志）。"""
    try:
        with _lock:
            _trim_inplace(path, keep_lines)
    except Exception:
        pass
