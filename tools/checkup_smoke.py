# -*- coding: utf-8 -*-
"""体检接口真实环境冒烟：只读扫描 + HTTP 路由（不做任何真实启停操作）"""
import os
import sys
import json
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lp import checkup


class StubMgr:
    cfg = {}
    manual = None

    def status(self):
        return {"scheme_name": "自建·自适应", "proc_max": 100}

    def learn_report(self):
        return {}


def main():
    t0 = time.perf_counter()
    st = checkup.startup_items()
    print("启动项 %d 条（扫描 %.1f ms）" % (len(st), (time.perf_counter() - t0) * 1000))
    for x in st[:12]:
        print("   %-8s %-10s %-28s %s" % ("[开]" if x["enabled"] else "[关]",
                                          x["where"], x["name"][:26],
                                          (x["cmd"] or "")[:64]))
    res = checkup.run_checks(StubMgr(), deep=True)
    print("\n体检 %d 分，耗时 %.1f ms" % (res["score"], res["ms"]))
    for x in res["items"]:
        print("   %-6s %-16s %s" % ({True: "OK", False: "!!", None: "--"}[x["ok"]],
                                    x["title"], x["value"]))
    top = []
    for p in res["top_procs"][:5]:
        top.append((p.get("name"), p.get("percent")) if isinstance(p, dict) else tuple(p)[:2])
    print("   后台占用 top:", top)
    print("   ", checkup.summary_line(res))

    # HTTP 路由（起在随机端口，避免和常驻实例抢 8753）
    from lp import webui
    srv = webui.start(StubMgr(), port=0)
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        d = json.loads(op.open(srv.url + "api/checkup?deep=1", timeout=8).read().decode("utf-8"))
        print("\nGET /api/checkup -> items=%d startups=%d score=%s"
              % (len(d["items"]), len(d["startups"]), d["score"]))
        d2 = json.loads(op.open(srv.url + "api/checkup", timeout=8).read().decode("utf-8"))
        print("GET /api/checkup(浅) -> ms=%s" % d2["ms"])
        # 只测非法输入，绝不碰真实启动项
        req = urllib.request.Request(srv.url + "api/startup_set",
                                     data=json.dumps({"id": "", "on": False}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            op.open(req, timeout=5).read()
            print("POST 空 id -> 未报错（异常）")
        except Exception as e:
            print("POST 空 id -> HTTP %s（预期 400）" % getattr(e, "code", e))
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
