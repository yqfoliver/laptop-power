# -*- coding: utf-8 -*-
"""面板高度校准：新增「核显优先」卡片后，量 .wrap 的真实底边。

走本地静态服务 + mock /api/status（真实状态取自正在运行的面板，再补上
gpupick 字段），不打扰运行中的实例。
"""
import functools
import http.server
import json
import os
import socketserver
import threading
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "web")
PORT = 8799

# 1) 真实状态（旧版面板没有 gpupick，手动补一份「有内容」的最坏情况）
st = json.load(urllib.request.urlopen("http://127.0.0.1:8753/api/status", timeout=5))
st["gpupick"] = {
    "enabled": True,
    "igpu_tier": 3,
    "managed": 2,
    "n_igpu": 2,
    "note": "",
    "probe": None,
    "current": {
        "process": "League of Legends.exe",
        "path": "C:\\Riot Games\\League of Legends\\League of Legends.exe",
        "mode": "igpu",
        "src": "builtin",
        "note": "英雄联盟",
        "tier": 2,
        "managed": True,
    },
}

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=WEB)
socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
print("静态服务已起: http://127.0.0.1:%d" % PORT)

try:
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="msedge")
        # 视口高度跟随 PANEL_H（面板窗口实际尺寸），溢出会被 overflow:hidden 吃掉
        try:
            import sys as _sys
            _sys.path.insert(0, HERE)
            from lp.webview2panel import PANEL_W, PANEL_H
            vp = {"width": PANEL_W, "height": PANEL_H}
        except Exception:
            vp = {"width": 560, "height": 528}
        print("视口:", vp)
        pg = b.new_page(viewport=vp)
        pg.route("**/api/status", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(st, ensure_ascii=False)))
        pg.goto("http://127.0.0.1:%d/index.html" % PORT, timeout=30000)
        pg.wait_for_timeout(1800)
        info = pg.evaluate("""() => {
            const w = document.querySelector('.wrap');
            const r = w.getBoundingClientRect();
            const gp = document.getElementById('gpCard');
            return {
              bottom: Math.round(r.bottom),
              height: Math.round(r.height),
              gpHeight: gp ? Math.round(gp.getBoundingClientRect().height) : null,
              gpText: gp ? gp.innerText.replace(/\\n/g, ' | ') : null,
              scrollH: document.documentElement.scrollHeight,
            };
        }""")
        print("wrap 底边:", info["bottom"], " 高:", info["height"])
        print("核显卡高度:", info["gpHeight"])
        print("卡片内容:", info["gpText"])
        print("文档 scrollHeight:", info["scrollH"])
        pg.screenshot(path=os.path.join(HERE, "build", "check", "panel_gp.png"))
        b.close()
finally:
    httpd.shutdown()
