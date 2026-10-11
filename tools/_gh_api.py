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
import io
import subprocess
import urllib.error
import urllib.request

UA = "laptop-power-release"


_TOK_CACHE = {"v": None}


def token() -> str:
    """取令牌（带缓存）。

    GCM 偶尔返回空（尤其并发/刚被卡住过一次时），而每个请求都去取一次的话，
    中途取到空就会 401 —— 整棵树推到一半失败。取一次就缓存住。
    """
    if _TOK_CACHE["v"]:
        return _TOK_CACHE["v"]
    v = _token_once()
    if not v:                      # 空值不缓存，下个请求再试一次
        return ""
    _TOK_CACHE["v"] = v
    return v


def _token_once() -> str:
    """真正去 GCM 取一次；GCM 会间歇性返回空（刷新令牌时出网被拦），重试几次。"""
    for _i in range(3):
        v = _token_ask()
        if v:
            return v
        import time as _t
        _t.sleep(2.0)
    return ""


ASK = "protocol=https\nhost=github.com\n\n"


def _run_cred(cmd) -> str:
    """跑一条取凭据的命令，从 stdout 里抠 password=（命令失败/超时就当没取到）。"""
    try:
        r = subprocess.run(cmd, input=ASK, capture_output=True, text=True,
                           timeout=60)
    except Exception:
        return ""
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def _token_ask() -> str:
    """从本机 Git Credential Manager 取令牌（不落盘、不打印）。

    三条路按顺序试，哪条通走哪条 —— 2026-10-11 实测只有第 2 条通：

    1. `git credential fill`：在这台机器上直接报
       `fatal: could not read Username for 'https://github.com'`
       —— 全局配置里 `credential.helper=` 是空值（Git for Windows 用它
       屏蔽系统级 helper），靠 `helperselector` 中转，而 selector 在这台
       机器上返回空。git push 自己加 `-c credential.helper=manager` 能过。
    2. `git credential-manager get`：直呼 GCM 本体，稳定返回 username/password。
    3. `git-credential-helper-selector fill`：换个机器可能只有这条通。

    第 1 条走**临时文件中转**而不是管道：git credential 会拉起 GCM 子进程，
    子进程继承管道句柄 —— 父进程超时被杀后管道仍不关闭，`capture_output`
    会永久阻塞在 read() 上（实测：60s timeout 形同虚设，脚本挂十几分钟）。
    """
    import tempfile
    fd_in, pin = tempfile.mkstemp(prefix="ghcred_in_")
    fd_out, pout = tempfile.mkstemp(prefix="ghcred_out_")
    out = ""
    try:
        with os.fdopen(fd_in, "w") as f:
            f.write(ASK)
        with open(pin, "r") as fi, open(pout, "w") as fo:
            try:
                subprocess.run(["git", "credential", "fill"],
                               stdin=fi, stdout=fo, stderr=subprocess.DEVNULL,
                               timeout=45)
            except Exception:
                pass
        try:
            out = io.open(pout, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            out = ""
    finally:
        for p in (pin, pout):
            try:
                os.remove(p)
            except Exception:
                pass
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()

    # 直呼 GCM 本体 / selector（这两条是同步管道，GCM 不会拉起交互子进程，
    # 不存在上面那个挂死问题，可以直接用 capture_output）
    for cmd in (["git", "credential-manager", "get"],
                ["git-credential-helper-selector", "fill"]):
        v = _run_cred(cmd)
        if v:
            return v
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
            timeout=90, tries=3):
    """发一个 REST 请求，返回 (status, body_bytes)。status=0 表示两个通道都失败。

    带重试（2026-10-11）：这个环境的出网通道会毫无征兆地翻脸——urllib 突然
    SSL UNEXPECTED_EOF、curl 突然 Connection aborted，过几秒又都好了。
    一次失败就判死的话，整棵树要上传几十个文件时几乎必然中途失败。
    """
    st, body = 0, b""
    for _i in range(max(1, int(tries))):
        st, body = _once(method, url, tok, data, headers, timeout)
        if st:
            return st, body
        import time as _t
        _t.sleep(2.0 + 2.0 * _i)
    return st, body


def _once(method, url, tok, data, headers, timeout):
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
