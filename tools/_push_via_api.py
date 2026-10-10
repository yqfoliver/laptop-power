# -*- coding: utf-8 -*-
"""通过 REST API 推送本地 HEAD commit（git 协议被代理 502 掐断时的后备通道）。

原理：拿远程 main 的 HEAD 作 parent，把本地 HEAD 相对其 parent 改动的文件
逐个建 blob（串行，并发会被掐），建 tree（base_tree=远程树，只带改动项），
建 commit，PATCH refs/heads/main。本地与远程基于同一 parent，等价于快进推送。
"""
import json
import subprocess
import sys

sys.path.insert(0, "tools")
from _gh_api import repo_slug, request, token  # noqa: E402


API = "https://api.github.com"


def api(method, path, data=None):
    """返回 (status, 解析后的 JSON)。path 形如 /repos/...。"""
    payload = None
    if data is not None:
        payload = data if isinstance(data, str) else json.dumps(data)
    h = {"Accept": "application/vnd.github+json", "Content-Type": "application/json"}
    st, body = request(method, API + path, token(), payload, h, 120)
    if st >= 400 or st == 0:
        raise SystemExit("API %s %s -> HTTP %s: %s"
                         % (method, path, st, body[:200].decode("utf-8", "ignore")))
    return json.loads(body.decode("utf-8"))


def git(*args):
    return subprocess.run(["git"] + list(args), capture_output=True,
                          text=True, timeout=120).stdout.strip()


def main():
    repo = repo_slug()
    local_head = git("rev-parse", "HEAD")
    parent = git("rev-parse", "HEAD~1")
    remote = api("GET", "/repos/%s/git/ref/heads/main" % repo)
    remote_sha = remote["object"]["sha"]
    print("remote HEAD:", remote_sha[:10], "| local parent:", parent[:10])
    if remote_sha != parent:
        # 远程可能有新提交：先核对是否同源
        info = api("GET", "/repos/%s/commits/%s" % (repo, remote_sha))
        parents = [p["sha"] for p in info.get("parents", [])]
        if parent not in parents and remote_sha != parent:
            raise SystemExit("远程 main 与本地分叉，需人工处理")

    # 本地 HEAD 相对 parent 的改动文件（状态字母 + 路径）
    out = git("diff-tree", "--no-commit-id", "--name-status", "-r", local_head)
    changes = []
    for line in out.splitlines():
        st, _, path = line.partition("\t")
        path = path.strip()
        if st == "D":
            changes.append(("D", path, None))
        else:
            with open(path, "rb") as f:
                data = f.read()
            r = api("POST", "/repos/%s/git/blobs" % repo,
                    {"content": data.decode("utf-8"), "encoding": "utf-8"})
            changes.append((st, path, r["sha"]))
            print("  blob", path, r["sha"][:10])
        if st == "R":       # 重命名很少见，人工处理
            raise SystemExit("重命名请人工处理: %s" % line)

    base = api("GET", "/repos/%s/commits/%s" % (repo, remote_sha))
    base_tree = base["commit"]["tree"]["sha"]
    items = []
    for st, path, sha in changes:
        items.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})
    tr = api("POST", "/repos/%s/git/trees" % repo,
                     {"base_tree": base_tree, "tree": items})
    print("tree:", tr["sha"][:10])

    msg = git("log", "-1", "--pretty=%B", local_head)
    author = git("log", "-1", "--pretty=%an <%ae>", local_head)
    name, _, email = author.partition(" <")
    email = email.rstrip(">")
    ci = api("POST", "/repos/%s/git/commits" % repo,
                     {"message": msg, "tree": tr["sha"], "parents": [remote_sha],
                      "author": {"name": name, "email": email,
                                 "date": git("log", "-1", "--pretty=%aI")},
                      "committer": {"name": name, "email": email,
                                    "date": git("log", "-1", "--pretty=%cI")}})
    print("commit:", ci["sha"][:10])

    r = api("PATCH", "/repos/%s/git/refs/heads/main" % repo,
            {"sha": ci["sha"], "force": False})
    print("ref updated ->", r["object"]["sha"][:10])


if __name__ == "__main__":
    main()
