# -*- coding: utf-8 -*-
"""用 REST API 把本地工作树推成远程 main（git 协议被代理掐断时的后备通道）。

为什么按「整棵树」推而不是按 diff
--------------------------------
早期版本按「本地 HEAD 相对 parent 的改动」推，前提是本地历史与远程对齐。
但 API 重建的 commit sha 与本地不同，几次之后两边历史就分叉，diff 推不动。
改成：**远程内容 = 本地工作树**（git ls-files 逐个比对 blob sha，只上传
不同的那几个文件），然后建 tree → commit（parent 取远程 HEAD）→ 快进 ref。
这样无论历史怎么分叉，推完远程一定等于本地，效果与 git push 等价。

blob sha 用 `git hash-object` 现算，与 GitHub 的 blob sha 同算法，
所以只有真正改动过的文件才会上传（通常几个）。
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

    files = [f for f in git("ls-files").splitlines() if f.strip()]
    items, uploaded = [], 0
    for path in files:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            continue
        sha = subprocess.run(["git", "hash-object", "--", path],
                             capture_output=True, text=True,
                             timeout=60).stdout.strip()
        if sha and remote_blobs.get(path) == sha:
            items.append({"path": path, "mode": "100644", "type": "blob",
                          "sha": sha})
            continue
        b = api("POST", "/repos/%s/git/blobs" % repo,
                {"content": data.decode("utf-8"), "encoding": "utf-8"})
        items.append({"path": path, "mode": "100644", "type": "blob",
                      "sha": b["sha"]})
        uploaded += 1
        print("  上传 %s（%d B）" % (path, len(data)))
    print("共 %d 个文件，其中上传 %d 个" % (len(items), uploaded))

    tree = api("POST", "/repos/%s/git/trees" % repo, {"tree": items})
    msg = git("log", "-1", "--pretty=%B")
    name = git("log", "-1", "--pretty=%an") or "yqfoliver"
    email = git("log", "-1", "--pretty=%ae") or "706425202@qq.com"
    ci = api("POST", "/repos/%s/git/commits" % repo,
             {"message": msg, "tree": tree["sha"], "parents": [remote_sha],
              "author": {"name": name, "email": email},
              "committer": {"name": name, "email": email}})
    api("PATCH", "/repos/%s/git/refs/heads/main" % repo,
        {"sha": ci["sha"], "force": False})
    print("已快进 main ->", ci["sha"][:10])


if __name__ == "__main__":
    main()
