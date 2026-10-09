# -*- coding: utf-8 -*-
"""精简版网页面板端到端验证：起 exe → 抓 / 与 /api/status → 断言新页面 → 干净退出。"""
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

EXE = os.path.join(HERE, "笔记本电源自适应.exe")
DETACHED, NEW_GROUP, NO_WINDOW = 0x00000008, 0x00000200, 0x08000000
WM_COMMAND = 0x0111
MENU_QUIT = 1006
_u = ctypes.WinDLL("user32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)


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


def main():
    for pid in pids_of_exe():
        h = _k.OpenProcess(0x0001, False, pid)
        if h:
            _k.TerminateProcess(h, 0)
            _k.CloseHandle(h)
    time.sleep(1)

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
        print("FAIL: 面板没起来")
        return 1

    ok = True
    html = op.open("http://127.0.0.1:8753/", timeout=5).read().decode("utf-8")
    checks = [
        ("新页面含「游戏功耗分配」", "游戏功耗分配" in html),
        ("新页面含「散热与风扇」", "散热与风扇" in html),
        ("新页面含「电池与续航」", "电池与续航" in html),
        ("旧设置面板已移除", "设置 / 名单" not in html),
        ("旧自学习块已移除", "自学习" not in html),
        ("旧旋钮块已移除", "renderKnobs" not in html),
        ("旧体检块已移除", "checkCard" not in html),
    ]
    for name, good in checks:
        print(("✔" if good else "✘") + " " + name)
        ok = ok and good

    st = json.loads(op.open("http://127.0.0.1:8753/api/status", timeout=5).read())
    for key in ("mode_label", "reason", "battery_info", "load", "thermal",
                "fan", "alloc", "pd", "gpu_eco", "auto", "busy"):
        if key not in st:
            print("✘ status 缺字段: " + key)
            ok = False
    print("mode=%s auto=%s battery=%s" % (st.get("mode_label"),
          st.get("auto"), (st.get("battery_info") or {}).get("percent")))

    for hwnd in windows_of(pids_of_exe()):
        _u.PostMessageW(hwnd, WM_COMMAND, MENU_QUIT, 0)
    time.sleep(2.5)
    left = pids_of_exe()
    print("剩余进程：%s" % (left or "无 ✔"))
    return 0 if ok and not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
