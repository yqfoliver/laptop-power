# -*- coding: utf-8 -*-
"""端到端验证「退出还原」（2026-10-07）

A-B-A-B 测出退出后 60Hz / 独显 Eco 都没还原，根因在 lp/display.py。
修完之后必须跑一遍真机闭环，不能只靠单元测试（单元测试用的是假 user32）。

流程：
    1. 从干净态起步：165Hz + 独显上电（eco=0）
    2. 启动程序 → 离电应自动降刷到 60Hz + 断电（eco=1）
    3. 退出程序 → **必须**回到 165Hz + eco=0
    4. 读 exit.log 尾部，确认没有「!! 还原失败」告警

用法：python tools/verify_exit_restore.py
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from lp import display, gpueco  # noqa: E402

EXE = os.path.join(HERE, "笔记本电源自适应.exe")
BASE = "http://127.0.0.1:8753"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api_get(path, timeout=5):
    try:
        return _opener.open(BASE + path, timeout=timeout).read()
    except Exception:
        return None


def pids():
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq 笔记本电源自适应.exe", "/FO", "CSV", "/NH"],
        capture_output=True, timeout=20).stdout.decode("mbcs", "replace")
    r = []
    for line in out.splitlines():
        p = [x.strip('"') for x in line.split('","')]
        if len(p) >= 2 and p[0].startswith("笔记本电源自适应"):
            try:
                r.append(int(p[1]))
            except Exception:
                pass
    return r


def quit_app(timeout=90):
    u = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    dword = ctypes.c_ulong()
    t0 = time.time()
    while time.time() - t0 < timeout:
        ps = set(pids())
        if not ps:
            return True
        found = []

        def cb(h, lp):
            u.GetWindowThreadProcessId(h, ctypes.byref(dword))
            if dword.value in ps:
                found.append(h)
            return True

        u.EnumWindows(WNDENUMPROC(cb), 0)
        for h in found:
            u.PostMessageW(h, 0x0111, 1006, 0)     # WM_COMMAND / MENU_QUIT
        time.sleep(2)
    return not pids()


def state(tag):
    d = display.DisplayCtl()
    g = gpueco.GpuEco()
    print("  %-10s %sHz  eco=%s" % (tag, d.current_hz(), g.read()))
    return d.current_hz(), g.read()


def main():
    print("=" * 62)
    print("退出还原端到端验证")
    print("=" * 62)

    print("\n[1] 起步：拨到干净态 165Hz + 独显上电")
    d = display.DisplayCtl()
    g = gpueco.GpuEco()
    d.restore(force_hz=165)
    g.set(0)
    time.sleep(3)
    hz0, eco0 = state("干净态")
    if hz0 != 165 or eco0 != 0:
        print("!! 起步状态不对，后续结论无效")
        return 1

    print("\n[2] 启动程序")
    DETACHED, NEWGROUP, NOWIN = 0x00000008, 0x00000200, 0x08000000
    subprocess.Popen([EXE], creationflags=DETACHED | NEWGROUP | NOWIN,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 45:
        if api_get("/api/status"):
            break
        time.sleep(1)
    print("    API %s" % ("可达" if api_get("/api/status") else "不可达"))
    time.sleep(25)                      # 等离电策略落地（降刷 + 断电）
    hz1, eco1 = state("运行中")

    print("\n[3] 退出程序")
    ok = quit_app(90)
    print("   退出 %s（残留 %s）" % ("成功" if ok else "超时", pids()))
    time.sleep(8)
    hz2, eco2 = state("退出后")

    print("\n" + "=" * 62)
    res = []
    res.append(("运行中降刷到 60Hz", hz1 == 60))
    res.append(("运行中独显断电 eco=1", eco1 == 1))
    res.append(("退出后刷新率还原 165Hz", hz2 == 165))
    res.append(("退出后独显上电 eco=0", eco2 == 0))
    for name, okk in res:
        print("  %s  %s" % ("✔" if okk else "✘", name))

    print("\n[4] exit.log 尾部（看有没有「!!」告警）")
    log = os.path.join(HERE, "exit.log")
    if os.path.isfile(log):
        lines = open(log, encoding="utf-8", errors="replace").read().splitlines()
        for ln in lines[-6:]:
            print("   " + ln)
        warn = [ln for ln in lines[-12:] if "!!" in ln]
        print("   最近 12 行里的告警：%d 条" % len(warn))
    return 0 if all(o for _, o in res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
