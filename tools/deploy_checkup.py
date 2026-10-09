# -*- coding: utf-8 -*-
"""
部署新版 exe 并在「真·exe 环境」里验证体检功能：
  1. 杀掉旧实例  2. 覆盖主目录 exe  3. 启动  4. 打 /api/checkup  5. 收尾退出

验证完会把自己启动的实例干净退出，不留常驻进程。
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
sys.path.insert(0, HERE)
SRC = os.path.join("dist", "笔记本电源自适应.exe")
DST = os.path.join(HERE, "笔记本电源自适应.exe")

DETACHED = 0x00000008
NEW_GROUP = 0x00000200
NO_WINDOW = 0x08000000


def pids_of_exe() -> list:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq 笔记本电源自适应.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=15).stdout.decode("mbcs", "replace")
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].startswith("笔记本电源自适应"):
            try:
                pids.append(int(parts[1]))
            except Exception:
                pass
    return pids


def kill_others():
    """杀掉正在运行的本程序实例"""
    for pid in pids_of_exe():
        h = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)   # TERMINATE
        if h:
            ctypes.windll.kernel32.TerminateProcess(h, 0)
            ctypes.windll.kernel32.CloseHandle(h)
            print("已结束旧实例 pid=%d" % pid)
    time.sleep(1.0)


def api(url: str, timeout: float = 6.0):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return json.loads(op.open(url, timeout=timeout).read().decode("utf-8"))


def post(url: str, data: dict):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    return json.loads(op.open(req, timeout=6).read().decode("utf-8"))


def send_quit(pids) -> bool:
    """给目标进程的窗口发「退出」菜单命令，让它走正常退出路径（不留僵尸托盘图标）。

    注意：PyInstaller onefile 是父子双进程，真正的窗口在子进程里，
    所以要按 exe 名把 pid 全找出来一起发。"""
    u = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    found = []
    targets = set(pids)

    def cb(hwnd, lp):
        dword = ctypes.c_ulong()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(dword))
        if dword.value in targets:
            found.append(hwnd)
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    if not found:
        return False
    WM_COMMAND, MENU_QUIT = 0x0111, 1006
    for h in found:
        u.PostMessageW(h, WM_COMMAND, MENU_QUIT, 0)
    return True


def main():
    if not os.path.isfile(SRC):
        print("找不到新 exe：%s（先跑 build_exe.py）" % SRC)
        return 1
    kill_others()
    shutil.copyfile(SRC, DST)
    print("已覆盖：%s（%.1f MB）" % (DST, os.path.getsize(DST) / 1048576))

    # 记下当前计划，收尾时还原
    from lp import powercfgctl as pc
    try:
        plan0 = pc.active_scheme()
    except Exception:
        plan0 = None

    p = subprocess.Popen([DST], creationflags=DETACHED | NEW_GROUP | NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("启动 pid=%d" % p.pid)

    base = "http://127.0.0.1:8753/"
    ok = False
    for i in range(30):
        time.sleep(1)
        try:
            st = api(base + "api/status")
            print("面板就绪（%ds）：mode=%s" % (i + 1, st.get("mode")))
            ok = True
            break
        except Exception:
            continue
    if not ok:
        print("面板没起来 —— 检查 面板地址.txt 或 startup_err.log")
        return 1

    d = api(base + "api/checkup?deep=1")
    print("\n/api/checkup：items=%d startups=%d score=%s ms=%s"
          % (len(d.get("items", [])), len(d.get("startups", [])),
             d.get("score"), d.get("ms")))
    for x in d.get("items", []):
        print("   %-6s %-16s %s" % ({True: "OK", False: "!!", None: "--"}[x["ok"]],
                                    x["title"], x["value"]))
    n_self = sum(1 for s in d.get("startups", []) if s.get("self"))
    print("启动项：共 %d，本程序标记 %d 条" % (len(d.get("startups", [])), n_self))
    try:
        post(base + "api/startup_set", {"id": "", "on": False})
        print("空 id 竟然成功了？！")
    except Exception as e:
        print("空 id 被拒（预期）：%s" % getattr(e, "code", e))

    # 收尾：干净退出（走托盘退出消息，避免留下僵尸图标）
    if not send_quit(pids_of_exe()):
        print("没找到窗口，直接结束进程")
        h = ctypes.windll.kernel32.OpenProcess(0x0001, False, p.pid)
        if h:
            ctypes.windll.kernel32.TerminateProcess(h, 0)
            ctypes.windll.kernel32.CloseHandle(h)
    else:
        print("已发送退出指令")
    time.sleep(2)
    if plan0:
        try:
            pc.set_active(plan0)
            print("电源计划已还原：%s" % plan0)
        except Exception as e:
            print("还原计划失败：%r" % e)
    print("完成 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
