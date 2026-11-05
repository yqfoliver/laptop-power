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

def _bi(ac, pct, care):
    """真实 battery_info 的必要字段（renderBatt 会读的那些）。
    旧夹具只放 display_hz/care，percent/eta/健康度/容量全是 undefined，
    电池卡量到的不是真实高度。取用户 10-20 截图的真实数值。"""
    return {"ok": True, "present": True, "ac": ac, "percent": pct,
            "capacity_mwh": 26500, "full_mwh": 55244,
            "designed_mwh": 73000, "health_pct": 75.7, "cycles": None,
            "voltage_v": 15.9, "rate_w": 25.6, "rate_avg_w": 25.8,
            "charging": ac, "discharging": not ac, "topup": False,
            "critical": False, "eta_minutes": None if ac else 174,
            "state_cn": "充电中" if ac else "放电中",
            "display_hz": 165, "display_orig_hz": 165,
            "display_available": [60, 165], "display_changed": False,
            "display_err": "", "top_procs": [], "brightness_applied": None,
            "brightness_dimmed": False, "brightness_cap": 45,
            "care": care}


CARE_WORST = {
    "enabled": True, "score": 88, "advice": [LONG_ADVICE,
                                             "距上次满充校准已 45 天（周期 30 天）→ 该做一次完整充放来修正 SOC 漂移"],
    # 2026-10-20：面板只渲染 advice_short，长 advice 退居 CLI。
    # 夹具要跟着真实数据结构走，否则量到的不是面板实际高度。
    "advice_short": "满充搁置 4.0h/天 · 上限可压到 80%",
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
    "current": {"process": "League of Legends.exe",
                "path": "C:\\Riot Games\\League of Legends\\League of Legends.exe",
                "mode": "igpu", "src": "builtin", "note": "英雄联盟",
                "tier": 2, "managed": True,
                "now": {"which": "dgpu", "why": "busy"}},
}

STATUS = {
    "mode": "office", "auto": True, "ac": True,
    # 与真实 status() 对齐：battery 是**数字**（st.get("battery_percent")），
    # 不是 dict——旧夹具写成 dict，页面渲染出「🔋 [object Object]%」
    "battery": 99,
    "reason": "使用电池 · explorer · 轻负载",
    "battery_info": _bi(True, 99, CARE_WORST),
    "alloc": {"enabled": True, "verdict_cn": "中等负载", "cpu_w": 42.1,
              "gpu_w": None, "system_w": 54.2, "gpu_temp": 49,
              "gpu_util": 0, "cpu_temp": 93, "envelope_w": 110,
              "gpu_tgp_max": 100, "reason": "", "events": []},
    # 2026-10-24：supply_w=91/tier=unknown 才会真的走「充电器测定中」两行
    # 长文案分支——旧夹具 supply_w=None，advText 落到养护短行，docstring 说
    # 的「两行长文案最坏情况」从来没被真正量过（夹具漂移）。
    "pd": {"active": False, "tier": "unknown", "supply_w": 91.0,
           "supply_src": None, "machine_w": 41.0,
           "force_igpu": False},
    "gpu_eco": {"eco": 0},
    # thermal 的真实键是 mode（thermal.summary()），不是 asus_mode——
    # 旧夹具写 as_mode 导致「华硕性能模式」隐藏规则从来没被真正验证过。
    # LONG：工具 perf 档 + 厂商「全速」→ 一致 → 标签隐藏（trim 目标）。
    "thermal": {"fan_cpu": 38, "fan_gpu": 37, "cpu_c": 93, "gpu_c": 49,
                "mode": "perf", "label": "性能优先"},
    # renderThermal 读的是顶层 state.fan（不是 state.thermal）
    "fan": {"available": True, "source": "ACPI", "fan_cpu": 38, "fan_gpu": 37,
            "temp_cpu": 93, "temp_gpu": 49, "perf_mode": "full",
            "perf_label": "全速"},
    "load": {"cpu": 28, "disk": 5, "net": 24, "gpu": 0},
    "gpupick": GPU_WORST,
}

# 短状态：复刻用户 2026-10-11 反馈「底部空一大片」那张截图 —— 内容明显比
# 初始窗口(604)矮，用来证明新版测量能正确把窗口缩回去（旧 scrollHeight 会因
# 视口托底卡在 604 缩不回去）。
CARE_SHORT = {
    "enabled": True, "score": 75, "advice": [],
    "charge_line": "充电上限：100%（满容量·无保护）",
    "limit_state": {"value": 100, "state": "synced"},
    "charge_limit": {"supported": True, "value": 100, "mode": "balanced",
                     "myasus": True},
    "scene": "desk", "intent_active": False, "target_limit": 80,
    "full_limit": 100, "calib_due": True, "calib_days_since": 45,
}
SHORT_STATUS = {
    "mode": "office", "auto": True,
    # 同 STATUS：battery 是数字；short 夹具复刻用户 10-20「离电 48%」那张截图
    "battery": 48,
    "ac": False,
    "reason": "使用电池 · explorer · 轻负载",
    "battery_info": _bi(False, 48, CARE_SHORT),
    "alloc": {"enabled": True, "verdict_cn": "判定中", "cpu_w": None,
              "gpu_w": None, "system_w": None, "gpu_temp": None,
              "gpu_util": 0, "cpu_temp": None, "envelope_w": 110,
              "gpu_tgp_max": 100,
              "reason": "非游戏档，不参与功耗重分配", "events": []},
    "pd": {"active": False, "tier": "unknown", "supply_w": 82,
           "supply_src": "本次实测（充电下界）", "machine_w": 41.0,
           "force_igpu": False},
    # 离电 + 独显已断电：复刻用户 10-20 截图（「独显 —」噪音行的来源）
    "gpu_eco": {"eco": 1},
    # SHORT：工具 auto 档 + 厂商「全速」→ 不一致 → 标签显示
    # （复刻用户 10-20 截图里那行，验证「只有真信号才出现」的另一半）
    "thermal": {"fan_cpu": 38, "fan_gpu": 37, "cpu_c": 93, "gpu_c": 49,
                "mode": "auto", "label": "自动"},
    "fan": {"available": True, "source": "ACPI", "fan_cpu": 38, "fan_gpu": 37,
            "temp_cpu": 93, "temp_gpu": 49, "perf_mode": "full",
            "perf_label": "全速"},
    "load": {"cpu": 28, "disk": 5, "net": 24, "gpu": None},
    "gpupick": {"enabled": True, "current": {"process": "explorer.exe",
               "mode": "dgpu", "src": "builtin",
               "now": {"which": "igpu", "why": "idle"}}},
}


class H(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/status"):
            # 页面固定轮询 /api/status（不带 query），short 场景只能靠
            # Referer 区分：Edge 同源 fetch 默认会带完整 Referer。
            # 曾因只查 self.path 导致 short 夹具从来没被真正喂到页面里过。
            ref = self.headers.get("Referer") or ""
            payload = SHORT_STATUS if ("short" in self.path or "short" in ref) else STATUS
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
