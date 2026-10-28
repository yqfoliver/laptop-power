# -*- coding: utf-8 -*-
"""前端面板（web/index.html）回归测试。

用 node + 最小 DOM 垫片真跑页面脚本，断言「点击之后 DOM 变成什么样」。
这些 bug 全是静态检查看不出来的：函数定义了没绑定、每次轮询重建按钮吃掉点击、
读数没了进度条留残影、离电就一律说「核显模式」、后端挂了面板假装没事。

node 不在时整组跳过（不影响其它测试），但会明确打印 skip。
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.join(HERE, "tools", "_web_sim.js")
HTML = os.path.join(HERE, "web", "index.html")


def main():
    print("== 面板前端行为（node + DOM 垫片）==")
    if not os.path.isfile(HTML):
        print("FAIL: 找不到 web/index.html")
        return 1
    if not os.path.isfile(SIM):
        print("FAIL: 找不到 tools/_web_sim.js")
        return 1
    node = shutil.which("node")
    if not node:
        print("skip: 没有 node，跳过前端行为测试（静态断言仍可由 CI 之外手工跑）")
        return 0
    p = subprocess.run([node, SIM, HTML], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    print(out.rstrip())
    if p.returncode != 0:
        print("FAIL: 前端行为断言未通过（返回码 %d）" % p.returncode)
        return 1
    if "全部通过" not in out:
        print("FAIL: 没有拿到「全部通过」结论，测试可能没跑完")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
