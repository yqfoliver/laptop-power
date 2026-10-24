# -*- coding: utf-8 -*-
"""单命令内验证部署产物：exe 内嵌新面板 HTML + battery_info 数据形状。"""
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

p = subprocess.Popen([EXE], cwd=ROOT, creationflags=CREATE_NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    html = st = None
    for i in range(30):
        time.sleep(1.0)
        try:
            with opener.open("http://127.0.0.1:%d/api/status" % PORT, timeout=3) as r:
                st = json.loads(r.read().decode("utf-8"))
            with opener.open("http://127.0.0.1:%d/index.html" % PORT, timeout=3) as r:
                html = r.read().decode("utf-8")
            break
        except Exception as e:
            last = e
    if st is None or html is None:
        print("FAIL: 面板 30s 未就绪: %s" % last)
        sys.exit(1)
    print("面板就绪（第 %d 秒）" % (i + 1))
    ok = True

    def chk(cond, msg):
        global ok
        print(("  OK  " if cond else "  FAIL") + " " + msg)
        ok = ok and bool(cond)

    chk('id="battHw"' not in html, "新 HTML：battHw 行已删")
    chk("健康度 \" + Math.round" in html, "新 HTML：合并行文案在场")
    chk("已限帧 " in html, "新 HTML：限帧信号条件显示在场")
    chk("插电跑几分钟重负载即自动标定；未标定前" in html,
        "新 HTML：测定中第二行已收紧")
    bi = st.get("battery_info") or {}
    need = ["percent", "health_pct", "capacity_mwh", "full_mwh", "charging",
            "discharging", "rate_avg_w", "display_hz", "display_changed",
            "trend_override", "care"]
    chk(all(k in bi for k in need), "battery_info 数据形状完好")
    print("  percent=%s health=%s cap=%s/%s charging=%s discharging=%s hz=%s" % (
        bi.get("percent"), bi.get("health_pct"), bi.get("capacity_mwh"),
        bi.get("full_mwh"), bi.get("charging"), bi.get("discharging"),
        bi.get("display_hz")))
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
