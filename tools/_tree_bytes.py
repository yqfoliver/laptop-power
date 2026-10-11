# -*- coding: utf-8 -*-
"""用远端树条目按 git 原始格式重算树哈希，定位与本地树的字节差异。"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), "tools"))
from _gh_api import repo_slug, request, token  # noqa: E402


def rebuild(entries):
    # git tree 对象：<mode ascii> <name utf8> NUL <20 字节二进制 sha>，按名字排序
    def key(e):
        return e["path"].encode("utf-8")
    buf = b""
    for e in sorted(entries, key=key):
        if e["type"] == "commit":       # 子模块条目名是 160000
            mode = e["mode"]
        else:
            mode = e["mode"]
        buf += mode.encode() + b" " + e["path"].encode("utf-8") + b"\0"
        buf += bytes.fromhex(e["sha"])
    hdr = ("tree %d" % len(buf)).encode() + b"\0"
    h = subprocess.run(["git", "hash-object", "-t", "tree", "--stdin"],
                       input=hdr + buf, capture_output=True)
    return h.stdout.decode().strip()


def main():
    lt = subprocess.run(["git", "rev-parse", "main^{tree}"],
                        capture_output=True, text=True).stdout.strip()
    st, b = request("GET", "https://api.github.com/repos/%s/commits/main"
                    % repo_slug(), token(), None, None, 60)
    rt = json.loads(b.decode("utf-8"))["commit"]["tree"]["sha"]
    st2, b2 = request("GET", "https://api.github.com/repos/%s/git/trees/%s"
                      % (repo_slug(), rt), token(), None, None, 60)
    d = json.loads(b2.decode("utf-8"))
    print("远端条目重算 sha:", rebuild(d["tree"]))
    print("远端声称 sha    :", rt)
    print("本地 sha        :", lt)

    # 本地根树原始字节 vs 由远端条目重建的字节
    local_raw = subprocess.run(["git", "cat-file", "tree", lt],
                               capture_output=True).stdout
    print("本地原始字节数:", len(local_raw))

    def parse(raw):
        out, i = [], 0
        while i < len(raw):
            sp = raw.index(b" ", i)
            nul = raw.index(b"\0", sp)
            mode = raw[i:sp].decode()
            name = raw[sp + 1:nul].decode("utf-8", "replace")
            sha = raw[nul + 1:nul + 21].hex()
            out.append((mode, name, sha))
            i = nul + 21
        return out

    L = parse(local_raw)
    R = [(e["mode"], e["path"], e["sha"]) for e in d["tree"]]
    ls, rs = set(L), set(R)
    print("仅本地:", sorted(ls - rs)[:6])
    print("仅远端:", sorted(rs - ls)[:6])


if __name__ == "__main__":
    main()
