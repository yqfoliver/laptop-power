# -*- coding: utf-8 -*-
"""静态交叉检查：config 键的「声明 vs 读取」是否对得上。

三类问题都能一次性抓出来：
  1. 读了但没声明（拼写错误 / 已删除的键）—— 读出来恒为默认值，功能静默失效
  2. 声明了但没人读 —— 面板能改、改了没用
  3. cfg[...] = 写入但未声明 —— 保存后不会被 sanitize 管，也不进迁移
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lp import config

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lp")

READ_RE = re.compile(
    r"""(?:\bcfg|\bself\.cfg|\bself\.mgr\.cfg|\bmgr\.cfg|\bc)\s*\.get\(\s*["']([a-z0-9_]+)["']""")
READ2_RE = re.compile(r"""(?:\bcfg|\bself\.cfg|\bmgr\.cfg)\s*\[\s*["']([a-z0-9_]+)["']\s*\]""")
K_RE = re.compile(r"""self\._k\(\s*["']([a-z0-9_]+)["']""")
WRITE_RE = re.compile(r"""(?:\bcfg|\bself\.cfg|\bmgr\.cfg)\s*\[\s*["']([a-z0-9_]+)["']\s*\]\s*=""")

reads, writes = {}, {}
files = []
for fn in sorted(os.listdir(ROOT)):
    p = os.path.join(ROOT, fn)
    if fn.endswith(".py") and os.path.isfile(p):
        files.append(p)
_mp = os.path.join(os.path.dirname(ROOT), "main.py")
if os.path.isfile(_mp):
    files.append(_mp)
for p in files:
    fn = os.path.basename(p)
    src = open(p, encoding="utf-8").read()
    for rx, bag, tag in ((READ_RE, reads, "get"), (READ2_RE, reads, "[]"),
                         (K_RE, reads, "_k"), (WRITE_RE, writes, "=")):
        for m in rx.finditer(src):
            bag.setdefault(m.group(1), set()).add("%s:%s" % (fn, tag))

DECL = set(config.DEFAULT_CONFIG)

print("== 1) 读了但 DEFAULT_CONFIG 里没有（拼错 / 已删）==")
bad = sorted(set(reads) - DECL)
for k in bad:
    print("  %-32s %s" % (k, sorted(reads[k])))

print("\n== 2) 声明了但源码里连字面量都没出现（纯死键）==")
all_src = "\n".join(open(p, encoding="utf-8").read() for p in files)
for k in sorted(DECL):
    if '"%s"' % k not in all_src and "'%s'" % k not in all_src:
        print("  %-32s = %r" % (k, config.DEFAULT_CONFIG[k]))

print("\n== 3) 只在写、从不读 ==")
for k in sorted(set(writes) - set(reads) - DECL):
    print("  %-32s %s" % (k, sorted(writes[k])))
for k in sorted((set(writes) & DECL) - set(reads)):
    print("  %-32s (已声明) %s" % (k, sorted(writes[k])))

print("\n键总数: 声明 %d / 读过 %d" % (len(DECL), len(reads)))
