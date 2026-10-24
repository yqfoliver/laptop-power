# -*- coding: utf-8 -*-
"""单命令内在线验证 S4 废除 + 忙闲闸（绕过系统代理，验证后即弃）。"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "笔记本电源自适应.exe")
PORT = 8753

# 不走系统代理（127.0.0.1 会被代理掐成 502）
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CREATE_NO_WINDOW = 0x08000000
p = subprocess.Popen([EXE], cwd=ROOT, creationflags=CREATE_NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    st = None
    for i in range(30):
        time.sleep(1.0)
        try:
            with opener.open("http://127.0.0.1:%d/api/status" % PORT,
                             timeout=3) as r:
                st = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            last = e
    if st is None:
        print("FAIL: 面板 30s 未就绪: %s" % last)
        sys.exit(1)
    print("面板就绪（第 %d 秒）" % (i + 1))
    pd = st.get("pd") or {}
    ok = True

    def chk(cond, msg):
        global ok
        print(("  OK  " if cond else "  FAIL") + " " + msg)
        ok = ok and bool(cond)

    chk("gpu_off" not in pd, "pd 报告无 gpu_off 字段")
    chk("gpu_cut" not in pd, "pd 报告无 gpu_cut 字段")
    chk(pd.get("stage", 0) <= 3, "stage <= 3（S4 已废除）: %s" % pd.get("stage"))
    chk("force_igpu" in pd, "force_igpu 字段在场（事前核显决策通道）")
    chk("tier" in pd, "tier 字段在场: %s" % pd.get("tier"))
    bat = st.get("battery_info") or {}
    chk("trend_override" in bat, "battery.trend_override 诊断字段在场")
    print("  tier=%s force_igpu=%s supply_w=%s stage=%s"
          % (pd.get("tier"), pd.get("force_igpu"), pd.get("supply_w"),
             pd.get("stage")))
    print("  note=%s" % pd.get("note"))
    print("ALL OK" if ok else "HAS FAIL")
    sys.exit(0 if ok else 1)
finally:
    # onefile 双进程：按 exe 名枚举全部 pid 结束
    subprocess.run(["taskkill", "/F", "/IM", "笔记本电源自适应.exe"],
                   creationflags=CREATE_NO_WINDOW,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        p.wait(timeout=5)
    except Exception:
        pass
