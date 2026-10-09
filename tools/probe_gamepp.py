# -*- coding: utf-8 -*-
"""列出本机进程，定位游戏加加 / 第三方硬件监控工具的真实进程名。

只读诊断，不改任何东西。
"""
import subprocess
import sys

KEYS = [
    "game", "pp", "jia", "buff", "monitor", "hardware", "fan", "temp",
    "sensor", "perf", "osd", "overlay", "rtss", "after", "hwinfo", "aida",
    "libre", "openhard", "throttle", "sysinfo", "info", "tool", "helper",
]


def main():
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, timeout=40,
    ).stdout.decode("mbcs", "replace")

    names = []
    for ln in out.splitlines():
        p = [x.strip('"') for x in ln.split('","')]
        if len(p) >= 1 and p[0]:
            names.append(p[0])
    names = sorted(set(names))
    print("进程总数 %d" % len(names))

    hits = [n for n in names if any(k in n.lower() for k in KEYS)]
    print("\n=== 疑似硬件监控/游戏工具（%d） ===" % len(hits))
    for n in hits:
        print("  " + n)

    print("\n=== 全部进程 ===")
    for n in names:
        print("  " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
