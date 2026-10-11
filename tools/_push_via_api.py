# -*- coding: utf-8 -*-
"""用 REST API 把本地工作树推成远程 main（git 协议被代理掐断时的后备通道）。

为什么按「整棵树」推而不是按 diff
--------------------------------
早期版本按「本地 HEAD 相对 parent 的改动」推，前提是本地历史与远程对齐。
但 API 重建的 commit sha 与本地不同，几次之后两边历史就分叉，diff 推不动。
改成：**远程内容 = 本地索引树**（`git ls-files -s` 逐个比对 blob sha，只上传
不同的那几个文件），然后建 tree → commit（parent 取远程 HEAD）→ 快进 ref。
这样无论历史怎么分叉，推完远程一定等于本地，效果与 git push 等价。

关键坑（都踩过）：
· blob sha 必须取**索引**的（`git ls-files -s`），不能用
  `git hash-object <工作树文件>` —— 后者按原始字节（CRLF）算，而索引是
  LF 规范化的，所有文本文件都会被误判成"有差异"整棵重传，树哈希对不上。
· 内容必须 base64 上传（encoding="base64"）。utf-8 解码会静默丢掉
  二进制文件（ico/dll）和 UTF-16 文件（autostart xml）—— 远端缺文件。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gh_api import repo_slug, request, token  # noqa: E402

API = "https://api.github.com"


def git(*args):
    return subprocess.run(["git"] + list(args), capture_output=True,
                          text=True, timeout=120).stdout.strip()


def api(method, path, data=None):
    payload = None
    if data is not None:
        payload = data if isinstance(data, str) else json.dumps(data)
    h = {"Accept": "application/vnd.github+json",
         "Content-Type": "application/json"}
    st, body = request(method, API + path, token(), payload, h, 120)
    if st >= 400 or st == 0:
        raise SystemExit("API %s %s -> HTTP %s: %s"
                         % (method, path, st,
                            body[:200].decode("utf-8", "ignore")))
    return json.loads(body.decode("utf-8"))


def main():
    repo = repo_slug()
    ref = api("GET", "/repos/%s/git/ref/heads/main" % repo)
    remote_sha = ref["object"]["sha"]
    remote_commit = api("GET", "/repos/%s/commits/%s" % (repo, remote_sha))
    remote_tree = api("GET", "/repos/%s/git/trees/%s?recursive=1"
                      % (repo, remote_commit["commit"]["tree"]["sha"]))
    remote_blobs = {e["path"]: e["sha"] for e in remote_tree["tree"]
                    if e["type"] == "blob"}
    print("remote HEAD %s | 远程 %d 个文件" % (remote_sha[:10], len(remote_blobs)))

    # 索引条目：<mode> <sha> <stage>\t<path>。sha 与本地 tree 里的 blob 完全一致
    import base64
    entries = []
    for line in git("ls-files", "-s").splitlines():
        meta, _, path = line.partition("\t")
        mode, sha, _stage = meta.split()
        if path.strip():
            entries.append((mode, sha, path.strip()))
    print("本地索引 %d 个文件" % len(entries))

    items, uploaded = [], 0
    for mode, sha, path in entries:
        if remote_blobs.get(path) == sha:
            items.append({"path": path, "mode": mode, "type": "blob",
                          "sha": sha})
            continue
        # 直接从对象库取内容：与索引 sha 严格一致（工作树文件可能是 CRLF，
        # 直接读会上传出另一个 sha）；cat-file 二进制安全
        data = subprocess.run(["git", "cat-file", "blob", sha],
                              capture_output=True, timeout=120).stdout
        if not data:
            raise SystemExit("!! 对象库读不到 %s (%s)" % (path, sha[:10]))
        b = api("POST", "/repos/%s/git/blobs" % repo,
                {"content": base64.b64encode(data).decode("ascii"),
                 "encoding": "base64"})
        if b["sha"] != sha:
            raise SystemExit("!! %s blob sha 不一致（索引 %s vs 上传 %s）"
                             % (path, sha[:10], b["sha"][:10]))
        items.append({"path": path, "mode": mode, "type": "blob",
                      "sha": b["sha"]})
        uploaded += 1
        print("  上传 %s（%d B）" % (path, len(data)))
    print("共 %d 个文件，其中上传 %d 个" % (len(items), uploaded))

    tree = api("POST", "/repos/%s/git/trees" % repo, {"tree": items})
    msg = git("log", "-1", "--pretty=%B")
    # 作者信息一律动态取（git log 缺失时退 git config；用户名/邮箱不进源码）
    name = git("log", "-1", "--pretty=%an") or git("config", "user.name")
    email = git("log", "-1", "--pretty=%ae") or git("config", "user.email")
    ci = api("POST", "/repos/%s/git/commits" % repo,
             {"message": msg, "tree": tree["sha"], "parents": [remote_sha],
              "author": {"name": name, "email": email},
              "committer": {"name": name, "email": email}})
    api("PATCH", "/repos/%s/git/refs/heads/main" % repo,
        {"sha": ci["sha"], "force": False})
    print("已快进 main ->", ci["sha"][:10])


if __name__ == "__main__":
    main()
