# -*- coding: utf-8 -*-
"""发布前隐私终检：对 git 索引里的每个文件读 bytes，同时比对两种编码。

教训（2026-10-09）：UTF-16 的 xml 与二进制的 lnk 用普通 grep 双双漏检，
所以必须**按字节**扫，且 ASCII 与 UTF-16LE 各查一遍。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

NEEDLES = ["yqfol", "C:\\Users", "FA401", "FA401WV", "TUF Gaming",
           "73000", "60536", "60.2Wh", "60.2 Wh"]
SKIP_EXT = (".ico", ".dll", ".png", ".exe", ".mp4", ".webm")
SKIP_FILES = ("tools/_privacy_scan.py",)     # 脚本自己含这些关键词


def variants(s):
    out = set()
    for enc in ("utf-8", "utf-16-le", "mbcs"):
        try:
            out.add(s.encode(enc))
        except Exception:
            pass
    return out


pats = []
for n in NEEDLES:
    pats.append((n, variants(n)))

files = subprocess.run(["git", "ls-files"], capture_output=True,
                       text=True, encoding="utf-8").stdout.splitlines()
hits = []
for rel in files:
    if not rel.strip():
        continue
    if rel.lower().endswith(SKIP_EXT) or rel.replace("\\", "/") in SKIP_FILES:
        continue
    p = os.path.join(ROOT, rel)
    if not os.path.isfile(p):
        continue
    try:
        data = open(p, "rb").read()
    except Exception:
        continue
    low = data.lower()
    for name, vs in pats:
        for v in vs:
            if v.lower() in low:
                hits.append((rel, name))
                break

print("扫描 %d 个已入库文件" % len(files))
if hits:
    print("!! 命中 %d 处：" % len(hits))
    for rel, name in hits:
        print("   %-50s  %s" % (rel, name))
    sys.exit(1)
print("干净：没有用户名 / 家目录 / 机型 / 本机电池容量残留")
