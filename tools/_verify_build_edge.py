# -*- coding: utf-8 -*-
"""起新 exe -> 立刻验证接口（Host 校验 / 体检 / 状态）-> 优雅退出。单次调用内完成。"""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request as u

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(APP, "笔记本电源自适应.exe")
op = u.build_opener(u.ProxyHandler({}))


def get(path, timeout=10):
    return json.loads(op.open("http://127.0.0.1:8753" + path, timeout=timeout).read())


def post(path, obj, headers):
    body = json.dumps(obj).encode()
    req = u.Request("http://127.0.0.1:8753" + path, data=body, headers=headers)
    try:
        r = op.open(req, timeout=8)
        return r.status, r.read()[:80]
    except Exception as e:
        code = getattr(e, "code", None)
        return code, str(e)[:80]


print("拉起 exe ...")
p = subprocess.Popen([EXE], cwd=APP, close_fds=True,
                     creationflags=0x00000008 | 0x00000200)
ok = False
for _ in range(40):
    time.sleep(0.5)
    try:
        st = get("/api/status", 3)
        ok = True
        break
    except Exception:
        continue
print("就绪:", ok, "pid:", p.pid)
if not ok:
    sys.exit(1)

print("mode=%s ac=%s batt=%s" % (st.get("mode"), st.get("ac"), st.get("battery")))
print("hz:", (st.get("display") or {}).get("current_hz"),
      "care:", (st.get("care") or {}).get("charge_line"))

ck = get("/api/checkup", 20)
print("checkup: score=%s ms=%s" % (ck.get("score"), ck.get("ms")))
print("  items:", [(i["key"], i["ok"]) for i in ck.get("items", [])])
print("  startups:", len(ck.get("startups") or []),
      "自启项:", [x["name"] for x in (ck.get("startups") or []) if x.get("self")])

print("跨站 POST（应 403）:", post("/api/notify", {"title": "x"},
                                   {"Content-Type": "text/plain",
                                    "Origin": "http://evil.com"}))
print("同源 POST（应 200）:", post("/api/notify", {"title": "x"},
                                   {"Content-Type": "application/json",
                                    "Origin": "http://127.0.0.1:8753"}))

# 优雅退出，让 cleanup 还原硬件
h = ctypes.windll.user32.FindWindowW("LaptopPowerTrayWin", None)
print("托盘 hwnd:", h)
if h:
    ctypes.windll.user32.PostMessageW(h, 0x0111, 1006, 0)
    for _ in range(20):
        time.sleep(0.5)
        if not ctypes.windll.user32.FindWindowW("LaptopPowerTrayWin", None):
            break
print("退出后 hwnd:", ctypes.windll.user32.FindWindowW("LaptopPowerTrayWin", None))
