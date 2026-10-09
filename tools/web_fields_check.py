# -*- coding: utf-8 -*-
"""字段一致性检查：网页面板引用的 status 字段 vs 程序实际返回的字段。

为什么要单独做这个工具：面板经过多次精简/重写，JS 里可能还留着已被删掉的
status 字段路径（后端改名或去掉后前端静默显示空白），肉眼很难发现。
用法：
    python tools/web_fields_check.py            # 对运行中的实例检查
"""
import os
import re
import sys
import json
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(HERE, "web", "index.html")
BASE = "http://127.0.0.1:8753"


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def referenced_fields(path):
    """从 index.html 里抽出所有被引用的 status 字段路径。

    识别两类：
      state.<key>                       -> 顶层字段
      const X = state.<key>  +  X.<k>   -> 子对象字段
    """
    src = open(path, encoding="utf-8").read()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)

    top, sub = {}, {}
    for k in re.findall(r"\bstate\.(\w+)", src):
        top[k] = top.get(k, 0) + 1
    # const B = state.battery_info||{};  / const t = state.thermal||{};
    alias = dict(re.findall(r"const\s+(\w+)\s*=\s*state\.(\w+)", src))
    for short, full in alias.items():
        for k in re.findall(r"\b%s\.(\w+)" % re.escape(short), src):
            sub.setdefault(full, {})[k] = sub.get(full, {}).get(k, 0) + 1
    return top, sub, alias


def main():
    if not os.path.isfile(HTML):
        print("找不到 web/index.html"); return 2
    try:
        d = json.loads(_opener().open(BASE + "/api/status", timeout=5).read())
    except Exception as e:
        print("无法连接 pid 面板（%s）\n请确认程序正在运行。" % e)
        return 2

    top, sub, alias = referenced_fields(HTML)
    miss, ok = [], 0

    for k in sorted(top):
        if k not in d:
            miss.append("顶层  state.%s" % k)
        else:
            ok += 1
    for parent, keys in sorted(sub.items()):
        node = d.get(parent)
        if not isinstance(node, dict):
            miss.append("子对象 state.%s 不是对象（实际 %r）" % (parent, type(node).__name__))
            continue
        for k in sorted(keys):
            if k not in node:
                miss.append("子对象 state.%s.%s" % (parent, k))
            else:
                ok += 1

    print("=" * 62)
    print("面板字段一致性检查")
    print("=" * 62)
    print("index.html 引用：顶层 %d 个，子对象 %s" % (len(top), {p: len(v) for p, v in sub.items()}))
    print("别名映射：%s" % alias)
    print("实际 status 顶层字段 %d 个" % len(d))
    print()
    if miss:
        print("【缺失字段】%d 处 —— 面板这些位置会显示空白：" % len(miss))
        for m in miss:
            print("   ✘", m)
    else:
        print("✔ 引用的字段全部存在（%d 处引用全部命中）" % ok)

    # 反向：status 有但页面没用（只是信息，不是 bug）
    unused = [k for k in d if k not in top and k not in alias.values()]
    if unused:
        print()
        print("[信息] status 里未被面板使用的字段（%d）：%s" % (len(unused), ", ".join(sorted(unused))))
    return 1 if miss else 0


if __name__ == "__main__":
    sys.exit(main())
