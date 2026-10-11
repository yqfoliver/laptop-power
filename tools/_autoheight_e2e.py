# -*- coding: utf-8 -*-
"""高度自适应的端到端验证用 mock 服务（tools/_autoheight_e2e.py）。

复刻用户 2026-10-11 截图里的真实状态：pd.tier=unknown 的两行长文案
（advline 是 white-space:pre-line，\\n 会真换行），再加养护最长建议 ——
这正是把旧固定窗口(604)顶爆的那组文案。
只服务 GET：/ → web/index.html，/api/status → canned JSON。端口 8798。
"""
import functools
import http.server
import json
import os
import socketserver
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "web")
PORT = 8798

sys.path.insert(0, HERE)

LONG_ADVICE = ("你常带出门（日均离电 6.0h）但上限只到 80% → 白砍 20% 续航，"
               "换来的寿命收益不划算。建议改回 100%，只在明确要长期插电那几天"
               "用「本次充满 / 回养护」临时压低。去 MyASUS → 设备设置 → "
               "电池健康充电 切换，或用面板上的「本次充满 / 回养护」（首次需确认一次 UAC）"
               "另外，电量计每隔一两个月需要一次完整满充校准，"
               "否则 SOC 漂移会出现「显示 30% 突然关机」。"
               "近期若有出行计划，出发前记得充满；长期插电时再切回养护上限即可。")

CARE_WORST = {
    "enabled": True, "score": 88, "advice": [LONG_ADVICE,
                                             "距上次满充校准已 45 天（周期 30 天）→ 该做一次完整充放来修正 SOC 漂移"],
    "charge_line": "充电上限：100%（已下发，待生效）",
    "limit_state": {"value": 100, "state": "pending", "note": "已下发，等待 EC 生效"},
    "limit_pending": True,
    "charge_limit": {"supported": True, "value": 80, "mode": "balanced",
                     "mode_cn": "平衡模式", "label": "充电上限 80%（平衡模式）",
                     "myasus": True},
    "scene": "mobile", "dc_h_day": 6.0,
    "intent": {"reason": "travel", "created": 0, "expire": 0,
               "prev_limit": 80, "target": 100},
    "intent_active": True, "intent_reason": "",
    "target_limit": 80, "full_limit": 100,
    "calib_due": True, "calib_days_since": 45,
    "stat": {"full_h_day": 0.4, "hot_h_day": 0.0},
    "today": {}, "relief_on": False, "relief_reason": "", "relief_cap": None,
    "hot_charge_c": 88, "hot_cpu_c": 95, "thermal_cap": 80,
}

GPU_WORST = {
    "enabled": True, "igpu_tier": 3, "managed": 2, "n_igpu": 2, "note": "",
    "probe": None,
    "current": {"process": "League of Legends.exe",
                "path": "C:\\Riot Games\\League of Legends\\League of Legends.exe",
                "mode": "igpu", "src": "builtin", "note": "英雄联盟",
                "tier": 2, "managed": True},
}

STATUS = {
    "mode": "office", "auto": True, "ac": True,
    "battery": {"percent": 99, "ac": True, "charging": True,
                "rate_w": -12.4, "health": 84, "full_mwh": 61500,
                "charge_mwh": 60800, "cycle": 112},
    "battery_info": {"display_hz": 165, "care": CARE_WORST},
    "alloc": {"enabled": True, "verdict_cn": "中等负载", "cpu_w": 42.1,
              "gpu_w": None, "system_w": 54.2, "gpu_temp": 49,
              "gpu_util": 0, "cpu_temp": 93, "envelope_w": 110,
              "gpu_tgp_max": 100, "reason": "", "events": []},
    "pd": {"active": False, "tier": "unknown", "supply_w": None,
           "supply_src": None, "machine_w": 41.0,
           "force_igpu": False},
    "gpu_eco": {"eco": 0},
    "thermal": {"fan_cpu": 38, "fan_gpu": 37, "cpu_c": 93, "gpu_c": 49,
                "asus_mode": "full"},
    "load": {"cpu": 28, "disk": 5, "net": 24, "gpu": 0},
    "gpupick": GPU_WORST,
}


class H(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/status"):
            body = json.dumps(STATUS, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


def main():
    handler = functools.partial(H, directory=WEB)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("mock 服务: http://127.0.0.1:%d/" % PORT, flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
