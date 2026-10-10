# -*- coding: utf-8 -*-
"""部署含硬件自适应的新 exe：覆盖根目录 exe → 拉起 → 验证 hw 档案出现在面板数据里。"""
import json
import os
import shutil
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dist", "笔记本电源自适应.exe")
DST = os.path.join(ROOT, "笔记本电源自适应.exe")

if os.path.isfile(SRC):
    shutil.copy2(SRC, DST)
    print("已覆盖 exe: %.2f MB" % (os.path.getsize(DST) / 1e6))
else:
    print("!! 找不到", SRC)
    raise SystemExit(1)

DETACHED = 0x00000008
NEW_GROUP = 0x00000200
p = subprocess.Popen([DST], cwd=ROOT,
                     creationflags=DETACHED | NEW_GROUP, close_fds=True)
print("已拉起 pid:", p.pid)

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for i in range(25):
    time.sleep(1)
    try:
        r = op.open("http://127.0.0.1:8753/api/status", timeout=3).read()
        d = json.loads(r.decode("utf-8"))
        print("面板已就绪（第 %d 秒）" % (i + 1))
        print("hw:", json.dumps(d.get("hw", {}), ensure_ascii=False))
        print("档位:", d.get("mode_label"), "| 原因:", d.get("reason"))
        break
    except Exception as e:
        last = e
else:
    print("面板未就绪:", last)
