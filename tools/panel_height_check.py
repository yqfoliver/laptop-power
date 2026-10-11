# -*- coding: utf-8 -*-
"""面板高度校准：量 .wrap 的真实底边，加行/加卡后必须重跑。

两条改进（2026-10-11）：
1) **不走系统代理**：127.0.0.1 会被代理掐成 502 Bad Gateway（此前直接崩在
   取 status 那一行）。用 `ProxyHandler({})` 绕过。
2) **用「最坏情况」文案**：测量的意义是防止溢出，所以 care 那段要填最长的
   一条建议 + 意图激活 + 校准到期，而不是拿当前这一刻的短文本量。
   有运行中的实例就复用它（只读 status，不打扰）；没有才自己起一个并负责退出。
"""
import functools
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "web")
PORT = 8799
EXE = os.path.join(HERE, "笔记本电源自适应.exe")

LONG_ADVICE = ("你常带出门（日均离电 6.0h）但上限只到 80% → 白砍 20% 续航，"
               "换来的寿命收益不划算。建议改回 100%，只在明确要长期插电那几天"
               "用「本次充满 / 回养护」临时压低。去 MyASUS → 设备设置 → "
               "电池健康充电 切换，或用面板上的「本次充满 / 回养护」（首次需确认一次 UAC）")

CARE_WORST = {
    "enabled": True, "score": 88, "advice": [LONG_ADVICE,
                                             "距上次满充校准已 45 天（周期 30 天）→ 该做一次完整充放来修正 SOC 漂移"],
    # 最长的一条：待生效文案 + 下面 JS 会再拼「本次允许充满（满充校准）…」
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


def pids_of_exe():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq 笔记本电源自适应.exe",
                          "/FO", "CSV", "/NH"], capture_output=True,
                         timeout=15).stdout.decode("mbcs", "replace")
    pids = []
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].startswith("笔记本电源自适应"):
            try:
                pids.append(int(parts[1]))
            except Exception:
                pass
    return pids


def fetch_status():
    """带 no-proxy 取一次真实 status；取不到返回 None。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        return json.loads(op.open("http://127.0.0.1:8753/api/status",
                                  timeout=5).read().decode("utf-8", "ignore"))
    except Exception:
        return None


def main():
    sys.path.insert(0, HERE)
    try:
        from lp.webview2panel import PANEL_W, PANEL_H
    except Exception:
        PANEL_W, PANEL_H = 560, 528

    st = fetch_status()
    started = False
    if st is None:
        if not os.path.isfile(EXE):
            print("没有运行中的面板，也找不到 exe —— 用最小 mock 量一次")
            st = {"mode": "balanced", "auto": True, "ac": True,
                  "battery": {"percent": 79, "ac": True}}
        else:
            print("没有运行中的面板，临时起一个（测完就退出）")
            subprocess.Popen([EXE],
                             creationflags=0x00000008 | 0x00000200 | 0x08000000,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = True
            for i in range(40):
                time.sleep(1)
                st = fetch_status()
                if st:
                    print("面板就绪（%ds）" % (i + 1))
                    break
    else:
        print("复用运行中的面板实例（只读 status）")

    if st is None:
        print("取不到 status，放弃")
        return 1

    # 覆盖成「最长文案」的最坏情况
    # 注意 care 在 **battery_info 里**（面板 `const B = state.battery_info`），
    # 放到顶层等于没覆盖 —— 2026-10-11 踩过：测出来是真实短文案，偏乐观 49px。
    st["gpupick"] = GPU_WORST
    bi = st.setdefault("battery_info", {})
    if not isinstance(bi, dict):
        bi = {}
        st["battery_info"] = bi
    bi["care"] = CARE_WORST
    st["pd"] = dict(st.get("pd") or {})
    st["pd"]["tier"] = "weak"
    st["pd"]["supply_w"] = 56.0
    st["pd"]["machine_w"] = 41.0
    st["pd"]["force_igpu"] = True

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=WEB)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("静态服务: http://127.0.0.1:%d  视口: %dx%d" % (PORT, PANEL_W, PANEL_H))

    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(channel="msedge")
            pg = b.new_page(viewport={"width": PANEL_W, "height": PANEL_H})
            pg.route("**/api/status", lambda r: r.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(st, ensure_ascii=False)))
            pg.goto("http://127.0.0.1:%d/index.html" % PORT, timeout=30000)
            pg.wait_for_timeout(1800)
            info = pg.evaluate("""() => {
                const w = document.querySelector('.wrap');
                const r = w.getBoundingClientRect();
                const bc = document.getElementById('battCard');
                return {
                  bottom: Math.round(r.bottom), height: Math.round(r.height),
                  battBottom: bc ? Math.round(bc.getBoundingClientRect().bottom) : null,
                  chgText: (document.getElementById('chgLine')||{}).textContent,
                  scrollH: document.documentElement.scrollHeight,
                };
            }""")
            print("wrap 底边: %d  高: %d  (窗口可用高 %d)"
                  % (info["bottom"], info["height"], PANEL_H))
            print("电池卡底边: %s" % info["battBottom"])
            print("充电行文案: %s" % (info["chgText"] or "").replace("\n", " | "))
            print("文档 scrollHeight: %d" % info["scrollH"])
            over = info["bottom"] - PANEL_H
            if over > 0:
                print("!! 溢出 %d px —— PANEL_H 至少要 %d" % (over, info["bottom"] + 6))
            elif over > -14:
                print("!! 余量只剩 %d px，再加一行就会溢出" % (-over))
            else:
                print("OK：还有 %d px 余量" % (-over))
            os.makedirs(os.path.join(HERE, "build", "check"), exist_ok=True)
            pg.screenshot(path=os.path.join(HERE, "build", "check", "panel_care.png"))
            b.close()
    finally:
        httpd.shutdown()
        if started:
            # 只退自己起的那个：按 exe 名枚举 pid 发退出命令（onefile 是双进程）
            import ctypes
            _k = ctypes.WinDLL("kernel32", use_last_error=True)
            for pid in pids_of_exe():
                h = _k.OpenProcess(0x0001, False, pid)
                if h:
                    _k.TerminateProcess(h, 0)
                    _k.CloseHandle(h)
            print("已退出临时实例")
    return 0


if __name__ == "__main__":
    sys.exit(main())
