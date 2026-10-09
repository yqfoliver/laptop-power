# -*- coding: utf-8 -*-
"""配置键体检：config.json 里存了哪些键？代码里真正用到哪些？

找出两类问题：
  · 僵尸键 —— 配置文件里有，但从未被任何代码读取（写错名字/改名后的残留）
  · 幽灵键 —— 代码里 cfg.get("xxx") 读取，但 DEFAULTS 里没有定义（拼错的名字）

用法：python tools/config_keys_check.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LP = os.path.join(HERE, "lp")


def load_live():
    sys.path.insert(0, HERE)
    from lp import config
    return config.load(), config.DEFAULT_CONFIG


def code_blob():
    txt = []
    for root, _dirs, files in os.walk(LP):
        for f in files:
            if f.endswith(".py"):
                txt.append(open(os.path.join(root, f), encoding="utf-8",
                                errors="replace").read())
    html = os.path.join(HERE, "web", "index.html")
    if os.path.isfile(html):
        txt.append(open(html, encoding="utf-8", errors="replace").read())
    return "\n".join(txt)


def main():
    live, defaults = load_live()
    blob = code_blob()

    zombies, ghosts = [], []
    for k in sorted(live):
        if k in defaults:
            continue
        # 代码里完全没出现过这个键名 -> 残留
        if len(re.findall(r'["\']%s["\']' % re.escape(k), blob)) == 0:
            zombies.append(k)

    for k in sorted(set(defaults)):
        if not re.search(r'["\']%s["\']' % re.escape(k), blob):
            ghosts.append(k)

    print("=" * 62)
    print("配置键体检")
    print("=" * 62)
    print("config.json 键数 %d | DEFAULTS 键数 %d" % (len(live), len(defaults)))
    print()
    print("【僵尸键】配置里有、代码从不读 —— %d 个" % len(zombies))
    for k in zombies:
        print("   ✘ %-28s = %r" % (k, live.get(k)))
    if not zombies:
        print("   ✔ 无")
    print()
    print("【幽灵键】DEFAULTS 有定义、代码从不读 —— %d 个" % len(ghosts))
    for k in ghosts:
        print("   · %-28s = %r" % (k, defaults.get(k)))
    if not ghosts:
        print("   ✔ 无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
