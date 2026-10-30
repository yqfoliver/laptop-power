# -*- coding: utf-8 -*-
"""把面板离屏渲染成 PNG，人眼核对体检卡的排版（不启动常驻程序）。

做法：本地起一个只回 mock JSON 的 http 服务（/api/status 给假状态，
/api/checkup 给**真实**的 checkup.run_checks(None) 结果），然后用 Edge
headless 截图。这样看到的是真实 CSS 排版 + 真实启动项数据。

用法：python tools/_shot_checkup.py [输出.png] [宽] [高]
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WEB = os.path.join(ROOT, "web")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

STATUS = {
    "mode": "office", "mode_label": "办公·跟手", "reason": "前台 code.exe",
    "ac": True, "ac_unstable": False, "auto": True, "foreground": "Code.exe",
    "panel_font_scale": 1.0, "busy": 12, "cooldown_left": 0,
    "load": {"state": "轻负载", "heavy": False, "cpu": 12, "disk": 3,
             "net": 1, "gpu": 0},
    "alloc": {"ok": True, "active": False, "verdict_cn": "判定中", "cpu_w": 12.0,
              "gpu_w": 0.0, "system_w": 24.0, "envelope_w": 110,
              "gpu_tgp_max": 100, "cpu_temp": 55, "gpu_temp": None,
              "gpu_util": None, "reason": "", "events": []},
    "thermal": {"mode": "perf"},
    "fan": {"fan_cpu": 30, "fan_gpu": 30, "temp_cpu": 55, "perf_label": "性能"},
    "gpu_eco": {"eco": 0, "mode": "standard", "has_dgpu": True},
    "gpupick": {"enabled": True, "probe": None,
                "current": {"process": "code.exe", "path": r"C:\x\code.exe",
                            "mode": None, "src": "builtin", "note": "",
                            "managed": False, "stale": False}},
    "pd": {},
    "battery_info": {
        "percent": 76, "charging": True, "discharging": False, "rate_avg_w": 12.0,
        "health_pct": 75.7, "capacity_mwh": 52000, "full_mwh": 69000,
        "display_hz": 60, "display_changed": False, "eta_minutes": 90,
        "care": {"enabled": True, "score": 88, "target_limit": 80,
                 "intent_active": False, "calib_due": False, "advice_short": "",
                 "charge_limit": {"supported": True, "value": 80, "mode_cn": "保养"},
                 "charge_line": "充电上限 80%（养护）",
                 "limit_state": {"state": "synced"}},
    },
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(STATUS)
        elif path == "/api/checkup":
            from lp import checkup
            self._json(checkup.run_checks(None))
        else:
            self._json({})


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tools", "ck_shot.png")
    w = sys.argv[2] if len(sys.argv) > 2 else "700"
    h = sys.argv[3] if len(sys.argv) > 3 else "1500"

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("serving on %d" % port)

    prof = tempfile.mkdtemp(prefix="lppa_shot_")
    # 第 4 参 nock = 不带 ?ck（看默认收起的样子）
    q = "" if (len(sys.argv) > 4 and sys.argv[4] == "nock") else "?ck"
    url = "http://127.0.0.1:%d/%s" % (port, q)
    cmd = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
           "--hide-scrollbars", "--user-data-dir=" + prof,
           "--window-size=%s,%s" % (w, h),
           "--virtual-time-budget=6000",
           "--screenshot=" + out, url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        print("edge rc=%s  %s" % (r.returncode, (r.stderr or b"")[-300:]))
    finally:
        srv.shutdown()
    if os.path.isfile(out):
        print("OK -> %s  (%d bytes)" % (out, os.path.getsize(out)))
    else:
        print("FAIL: 没有产出图片")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
