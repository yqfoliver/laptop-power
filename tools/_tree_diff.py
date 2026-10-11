# -*- coding: utf-8 -*-
"""定位本地树与远端树的哈希差异（blob 已确认一致，查结构层）。"""
import json
import subprocess
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), "tools"))
from _gh_api import repo_slug, request, token  # noqa: E402


def local_entries(sha):
    raw = subprocess.run(["git", "cat-file", "-p", sha],
                         capture_output=True).stdout
    txt = raw.decode("utf-8", "replace")
    out = {}
    for line in txt.splitlines():
        if "\t" not in line:
            continue
        meta, name = line.split("\t", 1)
        mode, typ, h = meta.split()
        out[name] = (mode, typ, h)
    return out


def remote_entries(sha):
    st, b = request("GET", "https://api.github.com/repos/%s/git/trees/%s"
                    % (repo_slug(), sha), token(), None, None, 60)
    d = json.loads(b.decode("utf-8"))
    out = {}
    for e in d["tree"]:
        out[e["path"]] = (e["mode"], e["type"], e["sha"])
    return out, d.get("truncated", False)


def main():
    lt = subprocess.run(["git", "rev-parse", "main^{tree}"],
                        capture_output=True, text=True).stdout.strip()
    st, b = request("GET", "https://api.github.com/repos/%s/commits/main"
                    % repo_slug(), token(), None, None, 60)
    rt = json.loads(b.decode("utf-8"))["commit"]["tree"]["sha"]
    print("local root :", lt)
    print("remote root:", rt)

    stack = [("(root)", lt, rt)]
    seen = 0
    while stack:
        name, l, r = stack.pop()
        seen += 1
        L = local_entries(l)
        R, trunc = remote_entries(r)
        if trunc:
            print("!! 远端树截断", name)
        for n in sorted(set(L) | set(R)):
            if L.get(n) != R.get(n):
                print("DIFF %s/%s" % (name, n))
                print("  local ", L.get(n))
                print("  remote", R.get(n))
                if (L.get(n) and L[n][1] == "tree" and R.get(n)
                        and R[n][1] == "tree"):
                    stack.append(("%s/%s" % (name, n), L[n][2], R[n][2]))
    print("比较了 %d 棵树" % seen)


if __name__ == "__main__":
    main()
