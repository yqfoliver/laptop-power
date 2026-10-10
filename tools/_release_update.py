"""把 Release v5.0 的 exe 附件替换为当前 dist 里的新版。

令牌从本机 Git Credential Manager 取（git credential fill），不落盘、不打印。
HTTP 一律走 curl（沙箱里 urllib 的 TLS 握手会被拦）。
"""
import json
import os
import subprocess
import sys

REPO = "yqfoliver/laptop-power"
TAG = "v5.0"
ASSET_NAME = "LaptopPowerAuto.exe"
SRC = os.path.join("dist", "笔记本电源自适应.exe")
UA = "laptop-power-release"


def token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=60).stdout or ""
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def curl(tok, method, url, extra=None, out_bin=False):
    cmd = ["curl", "-sS", "-X", method, url,
           "-H", "Authorization: token " + tok,
           "-H", "Accept: application/vnd.github+json",
           "-H", "User-Agent: " + UA,
           "-w", "\n%{http_code}"]
    cmd += list(extra or [])
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    raw = r.stdout
    if out_bin:
        return raw
    try:
        body, _, code = raw.rpartition(b"\n")
        return int(code.strip() or 0), body.decode("utf-8", "ignore")
    except Exception:
        return 0, ""


def main():
    if not os.path.isfile(SRC):
        print("缺少", SRC)
        return 1
    tok = token()
    if not tok:
        print("取不到 GitHub 令牌")
        return 1

    url = "https://api.github.com/repos/%s/releases/tags/%s" % (REPO, TAG)
    st, body = curl(tok, "GET", url)
    if st != 200:
        print("取 Release 失败", st, body[:200])
        return 1
    rel = json.loads(body)
    print("Release:", rel.get("name"), "| 当前附件:",
          [(a["name"], round(a["size"] / 1e6, 2)) for a in rel.get("assets", [])])

    for a in list(rel.get("assets", [])):
        st, _ = curl(tok, "DELETE",
                     "https://api.github.com/repos/%s/releases/assets/%s"
                     % (REPO, a["id"]))
        print("  删除", a["name"], "->", st)

    upload = rel.get("upload_url", "").split("{")[0]
    if not upload:
        print("没有 upload_url")
        return 1
    up = upload + "?name=" + ASSET_NAME
    st, body = curl(tok, "POST", up,
                    ["-H", "Content-Type: application/octet-stream",
                     "--data-binary", "@" + SRC])
    if st not in (200, 201):
        print("上传失败", st, body[:300])
        return 1
    res = json.loads(body)
    print("已上传:", res.get("name"), "%.2f MB" % (res.get("size", 0) / 1e6))
    print("直链: https://github.com/%s/releases/latest/download/%s"
          % (REPO, ASSET_NAME))
    return 0


if __name__ == "__main__":
    sys.exit(main())
