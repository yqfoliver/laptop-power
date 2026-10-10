# -*- coding: utf-8 -*-
"""GitHub REST 调用的通用通道（发布脚本共用）。

为什么两个通道都要有
--------------------
沙箱/代理环境下「哪个客户端能出网」会翻转，而且两次踩的方向相反：

- 2026-10-10 早些时候：Python urllib 走 api.github.com 报
  `SSLV3_ALERT_HANDSHAKE_FAILURE`，curl 正常 ⇒ 脚本全改成 curl。
- 同日晚些时候：curl 变成 `Recv failure: Connection was aborted`（HTTP/1.1
  也一样），urllib 反而通了。

只绑一个通道，网络一变发布就断。所以这里先 urllib、再 curl，哪个能用用哪个。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

UA = "laptop-power-release"


def token() -> str:
    """从本机 Git Credential Manager 取令牌（不落盘、不打印）。"""
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, timeout=60).stdout or ""
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def repo_slug() -> str:
    """仓库名从 git remote 自动取，别硬编码 —— fork 后脚本要能直接用。"""
    try:
        out = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True, timeout=30).stdout or ""
        m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?\s*$",
                      out.strip())
        if m:
            return "%s/%s" % (m.group(1), m.group(2))
    except Exception:
        pass
    env = os.environ.get("GITHUB_REPO", "")
    if env:
        return env
    raise SystemExit("取不到仓库名：请设 GITHUB_REPO=owner/repo 或配置 git remote origin")


def _via_urllib(method, url, tok, data, headers, timeout):
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if tok:
        req.add_header("Authorization", "token " + tok)
    req.add_header("User-Agent", UA)
    body = None
    if data is not None:
        body = data.encode("utf-8") if isinstance(data, str) else data
    try:
        with urllib.request.urlopen(req, data=body, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也是有效响应，正文里带着错误原因，要交回调用方
        return e.code, (e.read() or b"")


def _via_curl(method, url, tok, data, headers, timeout):
    cmd = ["curl", "-sS", "--http1.1", "-X", method, url,
           "-H", "User-Agent: " + UA,
           "-w", "\n%{http_code}"]
    if tok:
        cmd += ["-H", "Authorization: token " + tok]
    for k, v in (headers or {}).items():
        cmd += ["-H", "%s: %s" % (k, v)]
    inp = None
    if data is not None:
        cmd += ["--data-binary", "@-"]
        inp = data.encode("utf-8") if isinstance(data, str) else data
    try:
        r = subprocess.run(cmd, input=inp, capture_output=True,
                           timeout=timeout)
    except Exception:
        return 0, b""
    raw = r.stdout or b""
    body, _, code = raw.rpartition(b"\n")
    try:
        return int(code.strip() or 0), body
    except Exception:
        return 0, raw


def request(method, url, tok=None, data=None, headers=None,
            timeout=90):
    """发一个 REST 请求，返回 (status, body_bytes)。status=0 表示两个通道都失败。"""
    st, body = 0, b""
    try:
        st, body = _via_urllib(method, url, tok, data, headers, timeout)
    except Exception:
        st, body = 0, b""
    if st:
        return st, body
    return _via_curl(method, url, tok, data, headers, timeout)


def request_json(method, url, tok=None, data=None, timeout=90):
    """同上，带 JSON 头。data 为 dict/None 时自动序列化。"""
    payload = None
    if data is not None:
        payload = data if isinstance(data, str) else json.dumps(data)
    h = {"Accept": "application/vnd.github+json",
         "Content-Type": "application/json"}
    st, body = request(method, url, tok, payload, h, timeout)
    return st, body
