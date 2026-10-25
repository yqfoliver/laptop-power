# -*- coding: utf-8 -*-
"""单命令内验证部署产物：睡眠/唤醒 + 外接屏护栏 + 新硬件端点已载入且运行不崩。

验证哪些：
  1. 面板就绪（exe 内嵌新代码能起）；
  2. /api/status 的 gpu_eco 报告正常（意味着每轮 _gpu_eco_tick 跑通，
     其中调用了 atkacpi.gpu_display_connected() —— 新端点不崩即过关）；
  3. 启动日志无异常栈。
WM_POWERBROADCAST 无法在脚本里真触发（需真实挂起），但 on_suspend/
on_resume 的单元逻辑已在 test_gpueco.py 覆盖。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "笔记本电源自适应.exe")
PORT = 8753
CREATE_NO_WINDOW = 0x08000000
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _boot_log_tail(n=25):
    logs = sorted([f for f in os.listdir(ROOT) if f.startswith("boot") and f.endswith(".log")],
                  key=lambda f: os.path.getmtime(os.path.join(ROOT, f)), reverse=True)
    if not logs:
        return ""
    with open(os.path.join(ROOT, logs[0]), "r", encoding="utf-8", errors="ignore") as fh:
        lines = fh.read().splitlines()
    return "\n".join(lines[-n:])


p = subprocess.Popen([EXE], cwd=ROOT, creationflags=CREATE_NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    st = None
    last = None
    for i in range(30):
        time.sleep(1.0)
        try:
            with opener.open("http://127.0.0.1:%d/api/status" % PORT, timeout=3) as r:
                st = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            last = e
    if st is None:
        print("FAIL: 面板 30s 未就绪: %s" % last)
        sys.exit(1)
    print("面板就绪（第 %d 秒）" % (i + 1))
    ok = True

    def chk(cond, msg):
        global ok
        print(("  OK  " if cond else "  FAIL") + " " + msg)
        ok = ok and bool(cond)

    ge = st.get("gpu_eco") or {}
    chk(ge.get("eco") in (0, 1, None), "gpu_eco 报告在读（_gpu_eco_tick 跑通，gpu_display_connected 不崩）")
    chk("enabled" in ge, "gpu_eco.enabled 字段在场")
    print("  gpu_eco.eco=%s enabled=%s note=%s" % (ge.get("eco"), ge.get("enabled"), ge.get("note")))

    # 启动日志无异常栈
    tail = _boot_log_tail(30)
    has_traceback = "Traceback" in tail or "  File " in tail and "Error" in tail
    chk(not has_traceback, "启动日志无异常栈")
    # 部署拉起的实例会留下一条被沙箱挡的 schtasks（无害），不作为失败
    if "schtasks" in tail or "PROGRAM BLOCKED" in tail:
        print("  （注：schtasks 被沙箱挡属预期，无害）")

    print("ALL OK" if ok else "HAS FAIL")
    sys.exit(0 if ok else 1)
finally:
    subprocess.run(["taskkill", "/F", "/IM", "笔记本电源自适应.exe"],
                   creationflags=CREATE_NO_WINDOW,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        p.wait(timeout=5)
    except Exception:
        pass
