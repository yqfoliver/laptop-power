"""更新 Release 说明正文。

全部动态生成：仓库名取 git remote，release 按 tag 查 ID，文件数/测试数
现算 —— 硬编码这些常量会在 fork 或加测试后立刻过期。
"""
import glob
import json
import os
import re
import subprocess

TAG = os.environ.get("RELEASE_TAG", "v5.0")
UA = "laptop-power-release"


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


def token() -> str:
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, timeout=60).stdout or ""
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def curl(tok, method, url, data=None):
    cmd = ["curl", "-sS", "-X", method, url,
           "-H", "Authorization: token " + tok,
           "-H", "Accept: application/vnd.github+json",
           "-H", "User-Agent: " + UA,
           "-H", "Content-Type: application/json",
           "-w", "\n%{http_code}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    r = subprocess.run(cmd, input=data, capture_output=True,
                       text=True, timeout=180)
    body, _, code = r.stdout.rpartition("\n")
    return body.strip(), code.strip()


def repo_stats():
    """现算文件数与测试数，避免写死后过期。"""
    try:
        files = subprocess.run(["git", "ls-files"], capture_output=True,
                               text=True, timeout=60).stdout.split()
    except Exception:
        files = []
    tests = sorted(glob.glob("test_*.py"))
    return len(files), len(tests)


def main():
    repo = repo_slug()
    n_files, n_tests = repo_stats()
    dl = ("https://github.com/%s/releases/latest/download/"
          "LaptopPowerAuto.exe" % repo)

    body = """笔记本电源自适应 %s —— **零依赖、完全静默**的 Windows 笔记本电源管理常驻程序。

下载下方 exe，双击即可运行（**不需要管理员、不需要装驱动**）。

## 主要功能

- **五档策略**（游戏 / 办公 / 续航 / 极限省电 / 平衡）+ 按前台程序自动判定
- **离电续航**：独显 ACPI 级断电、降刷新率、限 CPU 上限、亮度分级 —— 层层往下压整机的实际功耗
- **Type-C 弱电源保护**：按「本次供电会话」现测这根线的可持续上限（换充电器自动作废重测），逼近上限时逐级压制，**绝不让电池倒贴**
- **供电档位策略**：按实测能力自动分档 —— 65W 档喂不饱独显就切核显渲染，100W 档允许独显但按供电预算收紧让渡停止点，原装适配器档不干预
- **插电游戏功耗分配**：按实测功耗把 CPU 的瓦数让给独显，并知道什么时候该停
- **核显优先调度**：核显跑得动的程序不唤醒独显（内置分级名单 + 手动试探实测 3D 占用后再固化）
- **硬件自适应**：首次启动在本机现测独显/屏幕刷新率/温度线/核显档位，**源码不含任何一台机器的标定值**
- **面板**：WebView2 网页形态（默认）/ 原生 GDI 窗口；托盘右键切档、开关面板、退出

## 硬件适配

首次启动在本机现测并生成 `hwprofile.json`（运行时数据，不入库）：独显有无与型号、
NVML 温度阈值、屏幕**真实存在**的刷新率档位、核显档位、厂商通道可用性。测不到的一律
退回保守兜底值，并在运行中用实测继续修正。

供电能力按「充电会话」分别测量：换充电器必然经历一次拔电，旧值即作废重新现测。
额定功率在普通权限下读不到（UCSI 无用户态通道），只能测量。

## 注意事项

- 未签名 exe 可能被 **Microsoft Defender 误删**（症状：开机不自启、exe 消失、日志为空）。请把程序所在目录加入 Defender 排除项。
- 本程序在华硕（ATKACPI）机型上开发。其它品牌的厂商接口大概率不可用，但**自建计划 / 处理器上限 / 刷新率 / 亮度 / PD 预算**部分仍然有效。复刻指南见仓库内 `用WorkBuddy复刻到其它品牌.md`。
- 附件 `LaptopPowerAuto.exe` 就是主程序（与中文名 `笔记本电源自适应.exe` 内容完全一致；GitHub 会把中文文件名重命名为 `default.exe`，所以统一用英文名发布）。
- **下载直链**：%s

## 源码

%d 个文件，含 %d 套回归测试（全部离线运行、不碰真硬件）。构建方式见 README。
""" % (TAG, dl, n_files, n_tests)

    tok = token()
    if not tok:
        raise SystemExit("取不到 GitHub 令牌（git credential fill）")
    rel, code = curl(tok, "GET",
                     "https://api.github.com/repos/%s/releases/tags/%s"
                     % (repo, TAG))
    if code != "200":
        raise SystemExit("查 release 失败 HTTP %s: %s" % (code, rel[:200]))
    rid = json.loads(rel).get("id")
    if not rid:
        raise SystemExit("release %s 没有 id" % TAG)

    _, code = curl(tok, "PATCH",
                   "https://api.github.com/repos/%s/releases/%s" % (repo, rid),
                   data=json.dumps({"body": body}))
    print("HTTP", code, "| repo", repo, "| %d 文件 / %d 套测试"
          % (n_files, n_tests))
    if code != "200":
        raise SystemExit("更新失败")


if __name__ == "__main__":
    main()
