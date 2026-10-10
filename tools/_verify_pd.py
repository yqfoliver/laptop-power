"""拉起新版并当场核对：供电上限是否按「本次会话」重新学、核显按钮目标是否正确。

沙箱里后台进程会在命令边界被回收，所以拉起 + 核对必须放在同一条命令里完成。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

NO_WIN = 0x08000000
EXE = os.path.join("dist", "笔记本电源自适应.exe")


def api(path, timeout=3.0):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8753" + path,
                                    timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return {"_err": str(e)}


def main():
    if not os.path.isfile(EXE):
        print("缺少", EXE)
        return 1
    subprocess.Popen([EXE, "--autostart"], creationflags=NO_WIN)
    ok = False
    for _ in range(20):
        time.sleep(1.0)
        st = api("/api/status")
        if not st.get("_err"):
            ok = True
            break
    if not ok:
        print("面板未就绪：", api("/api/status"))
        return 1

    print("== 供电会话 ==")
    for i in range(4):
        st = api("/api/status")
        pd = st.get("pd") or {}
        print("  ac=%s supply=%s src=%s firm=%s machine=%s rate=%s session=%ss hist=%s"
              % (st.get("ac"), pd.get("supply_w"), pd.get("supply_src"),
                 pd.get("supply_firm"), pd.get("machine_w"), pd.get("rate_w"),
                 pd.get("session_s"), pd.get("history")))
        time.sleep(3.0)

    print("== 核显优先（面板当前台 ⇒ 应落到上一个真实程序）==")
    gp = api("/api/gpupick")
    cur = gp.get("current") or {}
    print("  enabled=%s process=%s mode=%s src=%s stale=%s managed=%s"
          % (gp.get("enabled"), cur.get("process"), cur.get("mode"),
             cur.get("src"), cur.get("stale"), cur.get("managed")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
