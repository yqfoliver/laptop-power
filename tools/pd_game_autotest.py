# -*- coding: utf-8 -*-
"""自动化《巫师3》负载 + PD 供电采样（一条命令内跑完，避免沙箱回收子进程）。

流程：启动 Steam 游戏 -> 等窗口出现 -> 尝试回车载入存档 -> 采样 N 秒 -> 结束游戏。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

APPID = "292030"                      # The Witcher 3: Wild Hunt
STEAM = r"C:\Program Files (x86)\Steam\Steam.exe"
SAMPLE = int(os.environ.get("PD_SAMPLE", "180"))
WAIT_UP = int(os.environ.get("PD_WAIT_UP", "100"))   # 等游戏窗口/载入存档


def find_window(part):
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32")
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                if part.lower() in b.value.lower():
                    found.append(h)
        return True
    u.EnumWindows(cb, 0)
    return found[0] if found else None


def press_enter(hwnd):
    """把窗口提到前台并回车（巫师3 主菜单默认选中「继续」）"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32")
    try:
        u.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(1.0)
    KEYEVENTF_KEYUP = 0x0002
    for _ in range(2):
        u.keybd_event(0x0D, 0, 0, 0)
        time.sleep(0.05)
        u.keybd_event(0x0D, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(2.0)


def main():
    print("1) 启动《巫师3》(Steam -applaunch %s)" % APPID)
    try:
        subprocess.Popen([STEAM, "-applaunch", APPID],
                         creationflags=0x00000008 | 0x00000200)  # DETACHED|NEW_PG
    except Exception as e:
        print("   启动失败: %r" % e)
        return 1

    hwnd = None
    t0 = time.time()
    while time.time() - t0 < WAIT_UP:
        hwnd = find_window("witcher")
        if hwnd:
            print("   游戏窗口已出现（%.0fs）" % (time.time() - t0))
            break
        time.sleep(3)
    if not hwnd:
        print("   未发现游戏窗口，仍继续采样（可能是启动器界面）")
    else:
        time.sleep(25)                 # 等主菜单稳定
        press_enter(hwnd)              # 载入最近存档
        time.sleep(20)

    print("2) 采样 %ds" % SAMPLE)
    r = subprocess.run([sys.executable,
                        os.path.join(HERE, "tools", "pd_game_test.py"),
                        str(SAMPLE), "2"],
                       cwd=HERE, capture_output=True)
    out = r.stdout.decode("mbcs", "replace")
    print(out)
    if r.stderr:
        print("[stderr]", r.stderr.decode("mbcs", "replace")[-800:])

    print("3) 结束游戏")
    subprocess.run(["taskkill", "/IM", "witcher3.exe", "/F"], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
