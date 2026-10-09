# -*- coding: utf-8 -*-
"""本地控制面板：一个极小的 HTTP 服务 + 静态页面（零第三方依赖）"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

if getattr(sys, "frozen", False):
    ROOT = sys._MEIPASS              # 打包后 web/ 资源在解包目录里
    # exe 旁边若存在 web/index.html（用户目录里就是有），优先用磁盘版：
    # 改面板 UI 不用重新打包，改完重启即生效
    _SIDE_WEB = os.path.join(os.path.dirname(sys.executable), "web")
    if os.path.isfile(os.path.join(_SIDE_WEB, "index.html")):
        ROOT = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Handler(BaseHTTPRequestHandler):
    server_version = "LaptopPower/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):      # 静音
        pass

    # ------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        # 哨兵：记录页面/接口访问，用于排查"标签页反复重载"问题。
        # 只记页面加载，不记 /api/ 轮询（每 2 秒三个请求，会把这个日志写成 GB 级）
        if not path.startswith("/api/"):
            try:
                with open(os.path.join(ROOT, "webui_access.log"), "a", encoding="utf-8") as _lf:
                    _lf.write("%s GET %s UA=%s REF=%s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"), path,
                        (self.headers.get("User-Agent") or "")[:160],
                        (self.headers.get("Referer") or "")[:80]))
            except Exception:
                pass
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), MIME.get(".html", "text/html"))
            except Exception:
                self._send(404, b"missing index.html", "text/plain")
            return

        if path.startswith("/api/"):
            self._api_get(path)
            return

        # 静态资源：必须做目录逃逸检查（GET /../config.json 否则能读到配置/学习数据）
        rel = unquote(path).lstrip("/").replace("/", os.sep)
        full = os.path.realpath(os.path.join(WEB_DIR, rel))
        root = os.path.realpath(WEB_DIR)
        if (full == root or full.startswith(root + os.sep)) and os.path.isfile(full):
            ext = os.path.splitext(full)[1]
            try:
                with open(full, "rb") as f:
                    body = f.read()
            except Exception:
                body = b""
            self._send(200, body, MIME.get(ext, "application/octet-stream"))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        try:
            self._api_post(path, data)
        except Exception as e:            # 任何参数错误都不该让连接悬空
            self._json(400, {"error": str(e)})

    # ------------------------------------------------------------------
    def _api_get(self, path: str):
        mgr = self.server.mgr
        if path == "/api/status":
            self._json(200, mgr.status())
        elif path == "/api/config":
            self._json(200, mgr.cfg)
        elif path == "/api/schemes":
            try:
                from . import powercfgctl as pc
                self._json(200, pc.list_schemes())
            except Exception as e:
                self._json(200, {"error": str(e)})
        elif path == "/api/learn":
            try:
                self._json(200, mgr.learn_report())
            except Exception as e:
                self._json(200, {"error": str(e)})
        elif path == "/api/checkup":
            # 系统体检 + 启动项清单（微软电脑管家式）。deep=1 时多采一次进程占用
            try:
                from . import checkup
                deep = "deep" in self.path
                self._json(200, checkup.run_checks(mgr, deep=deep))
            except Exception as e:
                self._json(200, {"error": str(e), "items": [], "startups": []})
        elif path == "/api/thermal":
            try:
                from . import hw as _hw
                out = dict(mgr.thermal.summary())
                out["line"] = mgr.thermal.line()
                out["hw_line"] = mgr.thermal.hw_line()
                out["fan"] = _hw.fan_status()
                self._json(200, out)
            except Exception as e:
                self._json(200, {"error": str(e)})
        else:
            self._json(404, {"error": "no route"})

    def _api_post(self, path: str, data: dict):
        mgr = self.server.mgr
        if path == "/api/mode":
            key = (data or {}).get("mode")
            if key in ("gaming", "battery", "saver", "office", "balanced"):
                mgr.set_manual(key)
                mgr.cfg["auto_mode"] = False          # 手动选档 = 暂时脱离自动
                from . import config as cfgmod
                cfgmod.save(mgr.cfg)
                self._json(200, mgr.status())
            elif key == "":                            # 空 = 取消锁定、回到自动
                from . import config as cfgmod
                mgr.cfg["auto_mode"] = True
                mgr.manual = None
                mgr.cfg["manual_override"] = None
                cfgmod.save(mgr.cfg)
                mgr.set_auto()
                self._json(200, mgr.status())
            else:
                self._json(400, {"error": "bad mode"})
        elif path == "/api/auto":
            mgr.cfg["auto_mode"] = bool(data.get("on", True))
            from . import config as cfgmod
            cfgmod.save(mgr.cfg)
            if mgr.cfg["auto_mode"]:
                mgr.set_auto()
            self._json(200, mgr.status())
        elif path == "/api/settings":
            from . import config as cfgmod
            for k in ("low_battery_percent", "poll_seconds", "notify",
                      "minimize_to_tray", "open_panel_on_start"):
                if k in data:
                    mgr.cfg[k] = data[k]
            # 游戏档 CPU/独显 功耗分配参数（2026-10-04 新增）
            for k in ("alloc_enabled", "power_envelope_watts", "gpu_tgp_max_watts",
                      "gpu_sweet_watts",
                      "board_overhead_watts", "cpu_temp_limit_c", "gpu_temp_limit_c",
                      "alloc_min_cpu_pct", "alloc_step_pct", "alloc_cooldown_seconds",
                      "auto_game_gpu_pct"):
                if k in data:
                    mgr.cfg[k] = data[k]
            # 离电续航参数（2026-10-04 新增）
            for k in ("battery_eco_percent", "battery_saver_percent",
                      "refresh_on_battery", "dc_refresh_hz", "battery_eta",
                      "dc_brightness_cap", "idle_dim_enabled",
                      "panel_engine",
                      "panel_font_scale",
                      "idle_dim_level", "idle_dim_after_s",
                      "gpu_eco_auto"):
                if k in data:
                    mgr.cfg[k] = data[k]
            # 电池充放电管理参数（2026-10-05 新增）
            for k in ("battery_care_enabled", "care_full_soc_pct",
                      "care_deep_soc_pct", "care_advise_charge_limit_pct",
                      "care_hot_charge_c", "care_heat_relief",
                      "care_heat_relief_cpu_pct", "care_heat_relief_clear_c",
                      "care_thermal_relief", "care_hot_cpu_c",
                      "care_thermal_clear_c", "care_thermal_cpu_pct"):
                if k in data:
                    mgr.cfg[k] = data[k]
            # 散热/噪音策略参数（2026-10-05 新增，参考 G-Helper 标定）
            for k in ("thermal_quiet_cpu_pct", "thermal_quiet_relief_c",
                      "thermal_quiet_relief_pct", "thermal_quiet_gpu_cpu_temp_c",
                      "thermal_quiet_gpu_temp_c", "thermal_perf_relief_c",
                      "thermal_perf_relief_pct", "thermal_perf_gpu_cpu_temp_c",
                      "thermal_perf_gpu_temp_c", "thermal_atk_enabled",
                      "thermal_quiet_atk_mode", "thermal_perf_atk_mode"):
                if k in data:
                    mgr.cfg[k] = data[k]
            # 允许直接指定硬件旋钮目标值（高级；留空即用策略默认）
            for m in ("quiet", "perf"):
                for k in ("syscoolpol", "perfboostmode", "perfepp"):
                    key = "thermal_%s_%s" % (m, k)
                    if key in data and data[key] not in (None, ""):
                        try:
                            mgr.cfg[key] = int(data[key])
                        except Exception:
                            pass
            if "game_list" in data:
                mgr.cfg["game_list"] = [x.strip() for x in data["game_list"] if str(x).strip()]
            if "office_list" in data:
                mgr.cfg["office_list"] = [x.strip() for x in data["office_list"] if str(x).strip()]
            mgr.reload_alloc_cfg()
            cfgmod.save(mgr.cfg)
            self._json(200, {"ok": True})
        elif path == "/api/recheck":
            if mgr.manual:
                mgr.manual = None
                mgr.cfg["manual_override"] = None
            mgr.apply(mgr.detect(), reason="手动触发重新判定")
            self._json(200, mgr.status())
        elif path == "/api/autostart":
            from . import config as cfgmod
            on = bool(data.get("on", False))
            on = cfgmod.set_autostart(on)
            mgr.cfg["autostart"] = on
            cfgmod.save(mgr.cfg)
            self._json(200, {"ok": on, "enabled": cfgmod.autostart_enabled()})
        elif path == "/api/learn_reset":
            mgr.learn_reset()
            self._json(200, mgr.learn_report())
        elif path == "/api/learn_toggle":
            on = bool(data.get("on", True))
            mgr.learn_set_enabled(on)
            self._json(200, {"on": on})
        elif path == "/api/learn_forget":
            exe = str((data or {}).get("exe") or "").strip()
            if exe:
                mgr.learn_forget(exe.lower())
            self._json(200, mgr.learn_report())
        elif path == "/api/learn_revive":
            mgr.learn_revive()
            self._json(200, {"ok": True})
        elif path == "/api/panel":
            cb = getattr(self.server, "show_panel", None)   # 唤起原生面板
            if callable(cb):
                try:
                    cb()
                except Exception:
                    pass
            self._json(200, {"ok": True})
        elif path == "/api/knob":
            mode = (data or {}).get("mode")
            knob = (data or {}).get("knob")
            value = (data or {}).get("value")
            if mode is None or knob is None or value is None:
                self._json(400, {"error": "need mode/knob/value"})
            else:
                self._json(200, mgr.set_knob_pref(mode, knob, int(value)))
        elif path == "/api/pref_clear":
            mode = (data or {}).get("mode")
            knob = (data or {}).get("knob")
            if mode is None or knob is None:
                self._json(400, {"error": "need mode/knob"})
            else:
                self._json(200, mgr.clear_knob_pref(mode, knob))
        elif path == "/api/thermal_set":
            mode = str((data or {}).get("mode") or "").lower()
            if mode not in ("auto", "quiet", "perf"):
                self._json(400, {"error": "bad thermal mode"})
            else:
                self._json(200, mgr.set_thermal_mode(mode))
        elif path == "/api/startup_set":
            # 启停一条开机启动项（可逆改名，不删数据）
            try:
                from . import checkup
                sid = str((data or {}).get("id") or "")
                on = bool(data.get("on", False))
                if not sid:
                    self._json(400, {"error": "need id"})
                else:
                    self._json(200, checkup.set_startup(sid, on))
            except Exception as e:
                self._json(200, {"ok": False, "msg": str(e)})
        elif path == "/api/notify":
            if mgr.on_notify:
                mgr.on_notify(data.get("title", ""), data.get("text", ""))
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "no route"})


class WebServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, mgr, port=0):
        self.mgr = mgr
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.server_address[1]


DEFAULT_PORT = 8753


def start(mgr, port: int = 0) -> WebServer:
    """
    起本地面板服务。
    优先用固定端口（方便收藏/自启后随时打开），被占用时退回随机端口。
    """
    for p in ([port] if port else [DEFAULT_PORT, 0]):
        try:
            srv = WebServer(mgr, port=p)
        except OSError:
            continue
        try:
            with open(os.path.join(ROOT, "面板地址.txt"), "w", encoding="utf-8") as f:
                f.write(srv.url)
        except Exception:
            pass
        print("控制面板：%s" % srv.url, flush=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
    raise RuntimeError("无法启动本地面板服务")


_PANEL_TITLE = "笔记本电源自适应"


def _focus_existing_panel() -> bool:
    """如果已有浏览器窗口正显示面板（活动标签=面板），直接聚焦它，不开新标签"""
    try:
        import ctypes
        from ctypes import wintypes as wt
        u = ctypes.WinDLL("user32")
        k = ctypes.WinDLL("kernel32")

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                        ("d1", ctypes.c_ulonglong), ("d2", wt.DWORD), ("d3", wt.DWORD),
                        ("d4", wt.DWORD), ("d5", ctypes.c_long), ("d6", wt.DWORD),
                        ("exe", wt.WCHAR * 260)]

        browsers = {}
        h = k.CreateToolhelp32Snapshot(2, 0)
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        ok = k.Process32FirstW(h, ctypes.byref(pe))
        while ok:
            n = str(pe.exe).lower()
            if n in ("msedge.exe", "chrome.exe", "msedge_proxy.exe"):
                browsers[pe.th32ProcessID] = n
            ok = k.Process32NextW(h, ctypes.byref(pe))
        k.CloseHandle(h)
        if not browsers:
            return False

        found = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def cb(hwnd, lp):
            if not u.IsWindowVisible(hwnd):
                return True
            pid = wt.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in browsers:
                return True
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if _PANEL_TITLE in buf.value:
                found.append(hwnd)
            return True

        u.EnumWindows(CB(cb), 0)
        if not found:
            return False
        hwnd = found[0]
        # 最小化则还原，再拉到前台
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)          # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def open_browser(url: str) -> None:
    # 智能模式：面板已经开着就聚焦，不再堆新标签
    if _focus_existing_panel():
        return
    try:
        webbrowser.open(url)
    except Exception:
        try:
            os.startfile(url)      # type: ignore[attr-defined]
        except Exception:
            pass
