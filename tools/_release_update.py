"""把指定 Release 的 exe 附件替换为当前 dist 里的新版。

令牌从本机 Git Credential Manager 取（git credential fill），不落盘、不打印。
HTTP 走 tools/_gh_api（urllib / curl 双通道，哪个能用用哪个）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gh_api import repo_slug, request, request_json, token  # noqa: E402

TAG = os.environ.get("RELEASE_TAG", "v5.0")
ASSET_NAME = "LaptopPowerAuto.exe"
SRC = os.path.join("dist", "笔记本电源自适应.exe")


def main():
    if not os.path.isfile(SRC):
        print("缺少", SRC)
        return 1
    repo = repo_slug()
    tok = token()
    if not tok:
        print("取不到 GitHub 令牌")
        return 1
    base = "https://api.github.com/repos/%s" % repo

    st, body = request_json("GET", "%s/releases/tags/%s" % (base, TAG), tok)
    if st != 200:
        print("取 Release 失败", st, body[:200])
        return 1
    rel = json.loads(body.decode("utf-8", "ignore"))
    print("Release:", rel.get("name"), "| 当前附件:",
          [(a["name"], round(a["size"] / 1e6, 2)) for a in rel.get("assets", [])])

    for a in list(rel.get("assets", [])):
        st, _ = request_json("DELETE", "%s/releases/assets/%s" % (base, a["id"]),
                             tok)
        print("  删除", a["name"], "->", st)

    upload = rel.get("upload_url", "").split("{")[0]
    if not upload:
        print("没有 upload_url")
        return 1
    with open(SRC, "rb") as f:
        blob = f.read()
    st, body = request("POST", upload + "?name=" + ASSET_NAME, tok, blob,
                       {"Accept": "application/vnd.github+json",
                        "Content-Type": "application/octet-stream"},
                       timeout=600)
    if st not in (200, 201):
        print("上传失败", st, body[:300])
        return 1
    res = json.loads(body.decode("utf-8", "ignore"))
    print("已上传:", res.get("name"), "%.2f MB" % (res.get("size", 0) / 1e6))
    print("直链: https://github.com/%s/releases/latest/download/%s"
          % (repo, ASSET_NAME))
    return 0


if __name__ == "__main__":
    sys.exit(main())
