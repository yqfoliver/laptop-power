# -*- coding: utf-8 -*-
"""真·exe 验证睿频策略（GHelper Turbo Boost 档）是否随档位生效。

流程：结束旧实例 → 起 exe → 依次切 游戏 / 续航 / 平衡 档 → 每次读自建计划的
PERFBOOSTMODE（AC/DC）→ 与期望值比对 → 切回平衡并干净退出。
"""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
sys.path.insert(0, HERE)

from lp import powrprof as pp            # noqa: E402

EXE = os.path.join(HERE, "笔记本电源自适应.exe")
DETACHED, NEW_GROUP, NO_WINDOW = 0x00000008, 0x00000200, 0x08000000
WM_COMMAND, MENU_QUIT = 0x0111, 1006
_u = ctypes.WinDLL("user32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)
BOOST_CN = {0: "禁用", 1: "启用", 2: "激进", 3: "高效启用", 4: "高效激进"}


def pids_of_exe():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq 笔记本电源自适应.exe",
                          "/FO", "CSV", "/NH"], capture_output=True,
                         timeout=15).stdout.decode("mbcs", "replace")
    pids = []
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].startswith("笔记本电源自适应"):
            try:
                pids.append(int(parts[1]))
            except Exception:
                pass
    return pids


def windows_of(pids):
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    out = []

    def cb(hwnd, lp):
        pid = ctypes.c_ulong()
        _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            out.append(hwnd)
        return True

    _u.EnumWindows(CB(cb), 0)
    return out


def post(path, payload):
    req = urllib.request.Request("http://127.0.0.1:8753" + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=8).read())


def main():
    for pid in pids_of_exe():
        h = _k.OpenProcess(0x0001, False, pid)
        if h:
            _k.TerminateProcess(h, 0)
            _k.CloseHandle(h)
    time.sleep(1.5)

    subprocess.Popen([EXE], creationflags=DETACHED | NEW_GROUP | NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for i in range(40):
        time.sleep(1)
        try:
            op.open("http://127.0.0.1:8753/api/status", timeout=3).read()
            print("面板就绪（%ds）" % (i + 1))
            break
        except Exception:
            pass
    else:
        print("FAIL 面板没起来")
        return 1

    ok = True
    # 期望值：gaming ac=2 / battery dc=3（插电状态，按 AC 行判定）
    for mode, want in (("gaming", 2), ("office", 1), ("battery", 1)):
        post("/api/mode", {"mode": mode})
        time.sleep(4)
        scheme = pp.active_scheme()
        got = pp.read_attr(scheme, "PERFBOOSTMODE", False)
        good = (got == want)
        ok = ok and good
        print("%s 睿频 -> %s(%s)  期望 %s(%s)  %s"
              % (mode, got, BOOST_CN.get(got, "?"), want, BOOST_CN.get(want, "?"),
                 "✔" if good else "✘"))

    # 还原：切回系统平衡（自建计划的睿频不再生效，活动计划回到原计划）
    post("/api/mode", {"mode": "balanced"})
    time.sleep(3)
    print("已切回平衡，活动计划=%s" % pp.active_scheme())

    for hwnd in windows_of(pids_of_exe()):
        _u.PostMessageW(hwnd, WM_COMMAND, MENU_QUIT, 0)
    time.sleep(2.5)
    left = pids_of_exe()
    print("剩余进程：%s" % (left or "无 ✔"))
    return 0 if ok and not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
