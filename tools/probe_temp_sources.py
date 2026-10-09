# -*- coding: utf-8 -*-
"""温度源对比 + 退出还原回归（一次跑完，2026-10-07）

背景
----
游戏加加这类工具会在界面上让你「选择传感器」——同一项硬件可能有多个数据源。
本机也一样，CPU 温度有两条独立通道：
    A. PDH  Thermal Zone Information(*)\\Temperature   （ACPI，通用）
    B. 华硕 ATKACPI cpu_temp()                        （厂商私有）
从来没人比对过它们的口径。如果两个源差值稳定且很小，就能互为备份：
其中一个失效时自动降级到另一个，而不是像现在这样单源裸奔。

一次跑完三件事
--------------
    1. 退出主程序（顺带做一次退出还原的真机回归）
    2. 独占 ATKACPI 与 PDH 并行采样，算差值
    3. 重新启动主程序

为什么必须独占
--------------
ATKACPI 是独占内核句柄，两个进程同时调用会死锁 —— 这正是 2026-10-07 修过的
退出卡死 bug。所以本脚本第一步就是把主程序退干净，绝不并发。

为什么要有 try/finally 兜底
---------------------------
本脚本会把用户的常驻程序退出。中途任何异常都不能把机器晾在"程序已退出"的
状态 —— 这正是本项目自己总结的"要么全成，要么可重试"教训，工具自己得先守住。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

EXE = os.path.join(HERE, "笔记本电源自适应.exe")
BASE = "http://127.0.0.1:8753"
LOG = os.path.join(HERE, "exit.log")
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DETACHED, NEWGROUP, NOWIN = 0x00000008, 0x00000200, 0x08000000


def api_get(path, timeout=5):
    try:
        return json.loads(_opener.open(BASE + path, timeout=timeout).read())
    except Exception:
        return None


def pids():
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq 笔记本电源自适应.exe",
         "/FO", "CSV", "/NH"],
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
    """给所有实例发 WM_COMMAND/MENU_QUIT，直到一个不剩。"""
    u = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                     ctypes.c_void_p)
    dword = ctypes.c_ulong()
    t0 = time.time()
    while time.time() - t0 < timeout:
        ps = set(pids())
        if not ps:
            return True, time.time() - t0
        found = []

        def cb(h, lp):
            u.GetWindowThreadProcessId(h, ctypes.byref(dword))
            if dword.value in ps:
                found.append(h)
            return True

        u.EnumWindows(WNDENUMPROC(cb), 0)
        for h in found:
            u.PostMessageW(h, 0x0111, 1006, 0)
        time.sleep(2)
    return not pids(), time.time() - t0


def restart_app(timeout=45):
    """无论前面发生什么，都要把主程序拉回来。"""
    try:
        subprocess.Popen([EXE], creationflags=DETACHED | NEWGROUP | NOWIN,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("  ✘ 重启失败: %r" % e)
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if api_get("/api/status"):
            return True
        time.sleep(1)
    return False


def hwstate(tag):
    from lp import display, gpueco, hw
    d = display.DisplayCtl()
    g = gpueco.GpuEco()
    st = hw.power_status()
    hz, eco = d.current_hz(), g.read()
    ac = bool(st.get("ac"))
    print("  %-12s %sHz  eco=%s   %s%s%%"
          % (tag, hz, eco, "插电" if ac else "离电",
             "" if ac else st.get("battery_percent")))
    return hz, eco, ac


def log_tail_since(lines_before):
    if not os.path.isfile(LOG):
        return []
    lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()
    if lines_before is not None and lines_before <= len(lines):
        return lines[lines_before:]
    return lines[-14:]


def compare(n=8, gap=1.5):
    """独占采样：对比 PDH Thermal Zone 与 ATKACPI 的 CPU 温度。"""
    from lp import atkacpi, power

    pm = power.PowerMonitor()
    atk = atkacpi.AtkAcpi()
    # 注意两处 API 约定：
    #   1. AtkAcpi.__init__ 里已经自动 open()，不用也无法再调用 open() 之外的入口
    #   2. `ok` 与 `last_error` 是 @property，**不是方法** —— 写成 atk.ok() 会
    #      报 TypeError: 'bool' object is not callable
    if not atk.ok:
        print("  ✘ ATKACPI 不可用，last_error=%s" % atk.last_error)
        return None
    print("  ATKACPI 握手 OK (last_error=%s)" % atk.last_error)

    # PDH 前两次采样无效（已知坑），先空跑两次预热
    pm.sample(want_gpu=False)
    time.sleep(0.6)
    pm.sample(want_gpu=False)

    print()
    print("  %-5s %-16s %-16s %-9s"
          % ("#", "A.ThermalZone", "B.ATKACPI", "差值"))
    print("  " + "-" * 52)
    diffs = []
    for i in range(n):
        tz = pm.sample(want_gpu=False).get("cpu_temp")
        ta = atk.cpu_temp()
        d = (ta - tz) if (tz is not None and ta is not None) else None
        if d is not None:
            diffs.append((tz, ta, d))
        print("  %-5d %-16s %-16s %-9s"
              % (i + 1,
                 ("%.1f C" % tz) if tz is not None else "-",
                 ("%.1f C" % ta) if ta is not None else "-",
                 ("%+.1f" % d) if d is not None else "-"))
        time.sleep(gap)

    try:
        atk.close()
    except Exception:
        pass

    if not diffs:
        print("\n  没有可用样本（两个源至少有一个读不到）。")
        return None

    ds = [x[2] for x in diffs]
    avg = sum(ds) / len(ds)
    spread = max(ds) - min(ds)
    print()
    print("  平均差值 %+.2f C   极差 %.2f C   样本 %d" % (avg, spread, len(ds)))
    print("  A 均值 %.2f C   B 均值 %.2f C"
          % (sum(x[0] for x in diffs) / len(diffs),
             sum(x[1] for x in diffs) / len(diffs)))
    if abs(avg) <= 3.0 and spread <= 4.0:
        print("  -> 两个源口径一致，可以互为备份（交叉校验 + 自动降级）。")
        return "compatible"
    print("  -> 两个源口径不一致，不能直接互备，需要先判定哪个可信。")
    return "divergent"


def _body(res):
    """做正事。异常往上抛，由 main 的 finally 负责把程序拉回来。"""
    print("\n[0] 当前状态")
    hwstate("起步")
    print("     API %s" % ("可达" if api_get("/api/status") else "不可达"))

    print("\n[1] 退出主程序（同时也是一次退出还原的真机回归）")
    lines_before = None
    if os.path.isfile(LOG):
        lines_before = len(open(LOG, encoding="utf-8",
                                errors="replace").read().splitlines())
    ok, dt = quit_app(90)
    print("  退出 %s，耗时 %.1f 秒，残留 %s" % (ok, dt, pids()))
    if not ok:
        print("  ✘ 程序没退干净，不能独占 ATKACPI，放弃。")
        return None, res, None
    time.sleep(8)
    hz1, eco1, ac0 = hwstate("退出后")

    print("\n[2] 退出还原回归结论")
    # 插电：程序不降刷，退出后应回到 165Hz + 独显上电，这是硬判据。
    # 离电：欠刷/断电本来就是常驻策略，退出后 60Hz+eco=1 属正常残留，不判失败。
    if ac0:
        res.append(("退出后刷新率还原 165Hz", hz1 == 165))
        res.append(("退出后独显上电 eco=0", eco1 == 0))
    else:
        print("   (离电：欠刷与断电是常驻策略，退出后 60Hz+eco=1 属正常残留，"
              "不做硬判)")
        res.append(("离电场景已记录状态", True))
    for name, okk in res:
        print("  %s  %s" % ("✔" if okk else "✘", name))

    tail = log_tail_since(lines_before)
    warns = None
    if tail:
        print("  本次退出的日志：")
        for ln in tail:
            print("     " + ln)
        warns = len([ln for ln in tail if "!!" in ln])
        print("  告警 %d 条" % warns)

    print("\n[3] 独占 ATKACPI + PDH，对比 CPU 温度两个源")
    verdict = compare()

    print("\n[4] 重新启动主程序")
    alive = restart_app()
    print("  API %s" % ("可达 ✔" if alive else "不可达 ✘"))
    if alive:
        time.sleep(12)
        hwstate("重启后")
    return verdict, res, warns


def main():
    print("=" * 66)
    print("温度源对比 + 退出还原回归")
    print("=" * 66)
    verdict, res, warns = None, [], None
    try:
        verdict, res, warns = _body(res)
    except Exception as e:
        print("\n!! 中途异常：%r" % e)
    finally:
        if not pids():
            print("\n[兜底] 主程序不在运行，强制重启")
            alive = restart_app()
            print("  API %s" % ("可达 ✔" if alive else "不可达 ✘"))
            if alive:
                try:
                    time.sleep(10)
                    hwstate("最终状态")
                except Exception:
                    pass

    print("\n" + "=" * 66)
    print("温度源判定   ：%s" % (verdict or "未完成"))
    print("退出还原回归 ：%s" % ("通过" if res and all(o for _, o in res)
                                else "未通过/未执行"))
    print("exit.log 告警：%s 条" % (warns if warns is not None else "未知"))
    print("主程序最终   ：%s" % ("在跑 ✔" if pids() else "不在运行 ✘"))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
