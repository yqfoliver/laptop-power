# -*- coding: utf-8 -*-
"""logtrim：G-Helper 风格的概率自清理日志。"""
import os
import random
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lp import logtrim


def _tmp():
    fd, p = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    return p


def test_log_line_appends():
    p = _tmp()
    try:
        logtrim.log_line(p, "hello")
        with open(p, "r", encoding="utf-8") as f:
            assert f.read() == "hello\n"
    finally:
        os.remove(p)


def test_trim_inplace_keeps_tail():
    p = _tmp()
    try:
        with open(p, "w", encoding="utf-8") as f:
            for i in range(10):
                f.write("line%d\n" % i)
        logtrim._trim_inplace(p, 3)
        with open(p, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        # 末尾 3 行 + 一个「略去」标记行
        assert lines[-3:] == ["line7", "line8", "line9"]
        assert lines[0].startswith("...")
    finally:
        os.remove(p)


def test_over_max_bytes_forces_trim():
    p = _tmp()
    try:
        with open(p, "w", encoding="utf-8") as f:
            for i in range(5):
                f.write("x" * 20 + "\n")
        # 已超小体积阈值 -> 必裁；keep=2
        logtrim.log_line(p, "new", keep_lines=2, max_bytes=10)
        with open(p, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        assert lines[-1] == "new"
        # 标记行 + 末尾 2 行 + 新写入 1 行
        assert len(lines) <= 4
    finally:
        os.remove(p)


def test_probabilistic_always_and_never():
    # 1% 概率方案：random=0 必裁，random=1 必不裁（且未超体积、未超行数）
    p = _tmp()
    try:
        with open(p, "w", encoding="utf-8") as f:
            for i in range(5):
                f.write("line%d\n" % i)
        old = random.random
        random.random = lambda: 0.0
        try:
            logtrim.log_line(p, "appended", keep_lines=2, max_bytes=10**9)
        finally:
            random.random = old
        with open(p, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        # 5 行裁到 2 行 + 标记 + 新行 = 最多 4 行
        assert len(lines) <= 4 and lines[-1] == "appended"

        # 必不裁：文件小且行数少
        p2 = _tmp()
        try:
            with open(p2, "w", encoding="utf-8") as f:
                f.write("only\n")
            old = random.random
            random.random = lambda: 1.0
            try:
                logtrim.log_line(p2, "added", keep_lines=2, max_bytes=10**9)
            finally:
                random.random = old
            with open(p2, "r", encoding="utf-8") as f:
                content = f.read()
            assert content == "only\nadded\n"
        finally:
            os.remove(p2)
    finally:
        os.remove(p)


def test_thread_safety():
    p = _tmp()
    try:
        errs = []

        def worker(n):
            try:
                for i in range(n):
                    logtrim.log_line(p, "t%d" % i, max_bytes=10**9, prob=0.0)
            except Exception as e:  # noqa
                errs.append(e)

        ts = [threading.Thread(target=worker, args=(50,)) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errs, "并发写入抛出异常: %r" % errs
        with open(p, "r", encoding="utf-8") as f:
            n = sum(1 for _ in f)
        assert n == 8 * 50
    finally:
        os.remove(p)


if __name__ == "__main__":
    test_log_line_appends()
    test_trim_inplace_keeps_tail()
    test_over_max_bytes_forces_trim()
    test_probabilistic_always_and_never()
    test_thread_safety()
    print("test_logtrim: all passed")
