# -*- coding: utf-8 -*-
"""
笔记本电源自适应 —— 入口

规则（按优先级）：
  1. 手动锁定某个档位 -> 一直保持，直到解锁
  2. 检测到游戏        -> 游戏档（最高帧率；低电量自动降一档）
  3. 拔掉电源          -> 续航档（最长续航）
  4. Word / 浏览器     -> 办公档（跟手优先）
  5. 其余              -> 系统自带「平衡」，一个字节都不改

运行结构：
  主线程 = Win32 消息泵（负责托盘图标、托盘菜单、热键）
  工作线程 = 场景检测循环 + 本地控制面板 HTTP 服务
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from lp import tray as traylib
except Exception:
    traylib = None

WPM = 0  # 消息泵是否可用


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    from lp import config, diagnose, hw
    from lp import powercfgctl as pc
    from lp import profiles

    # 自启排查（2026-10-07）：开机没起来时，最要紧的是知道「到底走到哪一步」。
    # 这条日志写在**最早期**，早于单实例判定和 Manager 构造 —— 只要进程被拉起，
    # 就一定有一行；反过来，开机后这里没有 14:5x 的记录，就说明外部没把它拉起来。
    if "--wv2-child" not in argv:
        try:
            _log_boot("被调用 argv=%s" % (" ".join(argv) or "(无参数)"))
        except Exception:
            pass

    cfg = config.load()

    # ------------------------------------------------------- WebView2 面板子进程
    # 早于一切重量级逻辑：这个模式只负责开一个承载 WebView2 的窗口，
    # 不碰电源、不碰托盘、不写全局变量，父进程没了就自己退出。
    if "--wv2-child" in argv:
        return _wv2_child(argv)

    if "--engine=webview2" in argv:
        _WV2_FLAG[0] = True       # 命令行强制用 WebView2（临时试用，不改 config）

    # ------------------------------------------------------- 命令行模式
    if "--status" in argv:
        st = hw.power_status()
        print("电源：%s   电量：%s%%   当前计划：%s"
              % ("插电" if st["ac"] else "电池", st.get("battery_percent"), pc.active_scheme()))
        return 0

    if "--preview" in argv:
        from lp.manager import Manager
        m = Manager(cfg)
        for key in ("gaming", "battery", "saver", "office", "balanced"):
            r = m.apply(key, reason="预览", dry=True)
            print("[%s]" % profiles.PROFILES[key]["label"])
            for c in r["changed"]:
                print("    ·", c)
            if r.get("note"):
                print("    ", r["note"])
        return 0

    if "--hw" in argv:
        # 打印本机硬件自适应档案（只读探测，不写电源计划、不改注册表）
        from lp import hwprofile as _hw
        from lp.nvmlctl import Nvml
        from lp.display import DisplayCtl
        s = _hw.ensure(cfg, nvml=Nvml(), display=DisplayCtl()).summary()
        print("硬件自适应档案（本机现测，结果不随源码分发）：")
        for k, label in (("dgpu_name", "独显"), ("igpu_name", "核显"),
                         ("atkacpi_ok", "厂商通道"), ("hz", "当前刷新率"),
                         ("available_hz", "可用刷新率"),
                         ("gpu_tgp_max_w", "独显上限(W)"),
                         ("gpu_temp_limit_c", "独显温度线(℃)"),
                         ("dc_refresh_hz", "离电降刷"),
                         ("gpu_peak_w", "实测峰值(W)")):
            print("  %-14s %s" % (label, s.get(k)))
        print("  取值来源     %s" % ("、".join(s.get("sources") or []) or "通用兜底"))
        return 0

    if "--selftest" in argv:
        print("管理员权限：%s" % diagnose.is_admin())
        print("系统计划：%s" % pc.list_schemes())
        print("当前计划：%s" % pc.active_scheme())
        print("ATKACPI：%s" % diagnose.atk_available())
        # 静默底线下的可诊断性：巡检里吞掉的异常都记在这里（去重/限长）
        try:
            from lp import errlog
            t = errlog.tail(12).strip()
            print("error.log：%s" % (t if t else "（空 —— 近期没有内部异常）"))
        except Exception:
            pass
        return 0

    for flag, key in (("--gaming", "gaming"), ("--battery", "battery"),
                      ("--saver", "saver"),
                      ("--office", "office"), ("--balanced", "balanced")):
        if flag in argv:
            from lp.manager import Manager
            m = Manager(cfg)
            m.apply(key, reason="命令行")
            print("已套用：%s" % profiles.PROFILES[key]["label"])
            return 0

    # ------------------------------------------------------- 常驻模式
    # 已有实例在跑时，--panel 只是唤醒那个实例的原生面板
    if "--panel" in argv and _already_running():
        base = _panel_url() or "http://127.0.0.1:8753/"
        try:
            import json as _json
            import urllib.request as _ur
            op = _ur.build_opener(_ur.ProxyHandler({}))
            req = _ur.Request(base + "api/panel",
                              data=_json.dumps({}).encode("utf-8"),
                              headers={"Content-Type": "application/json"})
            op.open(req, timeout=4).read()
        except Exception:
            pass
        return 0

    # 单实例：已经有一个在跑就不再起第二个，避免双实例抢电源计划
    if _already_running():
        _log_boot("退出：检测到已有实例在跑（面板端口已被占用）——本次不再启动")
        print("检测到程序已在运行，本次直接退出（面板：%s）"
              % (_panel_url() or "http://127.0.0.1:8753/"))
        return 0

    from lp.manager import Manager
    from lp import tray as traylib
    from lp import webui

    def on_notify(title, text):
        try:
            traylib_tray.notify(title, text)
        except Exception:
            pass

    mgr = Manager(cfg, on_notify=on_notify)

    try:
        mgr.ensure_scheme()
    except Exception as e:
        print("初始化电源计划失败：%s\n（请右键「以管理员身份运行」）" % e)

    global _MGR
    _MGR[0] = mgr

    srv = webui.start(mgr)
    url = srv.url
    _log_exit("启动 pid=%d 面板=%s 引擎=%s"
              % (os.getpid(), url, cfg.get("panel_engine", "auto")))

    # 原生桌面面板（GHelper 式独立窗口）；失败时回退网页面板
    try:
        from lp.native_panel import NativePanel
        _PANEL[0] = NativePanel(mgr, url)
    except Exception:
        _PANEL[0] = None
    srv.show_panel = lambda: _SHOW_PANEL.__setitem__(0, True)
    if "--panel" in argv:
        _SHOW_PANEL[0] = True

    traylib_tray = traylib.Tray(on_command=lambda mid: on_tray_cmd(mid, url))
    traylib_tray.menu_provider = lambda: tray_menu(mgr)
    if not traylib_tray.start("笔记本电源自适应"):
        print("[提示] 托盘图标创建失败，面板仍可通过 %s 访问" % url)
    _TRAY[0] = traylib_tray

    # 自启以注册表为准；顺手刷新一次 vbs，让候选解释器列表跟上当前环境
    _AUTO[0] = bool(config.autostart_enabled())
    if _AUTO[0]:
        try:
            config._write_vbs()
        except Exception:
            pass
        # 自启加固（2026-10-08）：Run 键在本机开机瞬间拉不起进程（boot.log 无痕），
        # 补一条计划任务通道，程序自己用 schtasks 建 —— 静默，失败只写日志。
        try:
            from lp import autostart_task
            autostart_task.ensure_async()
        except Exception:
            pass

    if cfg.get("open_panel_on_start", False):
        webui.open_browser(url)

    register_hotkey(traylib_tray.hwnd)

    if not cfg.get("first_run_done"):
        cfg["first_run_done"] = True
        cfg["autostart_notified"] = True     # 静默要求：首次运行不弹任何气泡
        config.save(cfg)

    # 工作线程：检测循环
    def worker():
        mgr.start()
        while not getattr(mgr, "_stop", False):
            time.sleep(1)

    t = __import__("threading").Thread(target=worker, daemon=True)
    t.start()

    # 主线程：Win32 消息泵（必须，托盘点击/菜单/热键都靠它）
    _pump()
    # 关键：先删托盘图标再退出。漏了这条，点「退出」后图标会变成
    # 点不动的「僵尸图标」挂到下次鼠标划过任务栏为止。
    _log_exit("泵已返回")
    try:
        traylib_tray.destroy()
        _log_exit("托盘图标已销毁")
    except Exception as e:
        _log_exit("托盘销毁异常 %r" % e)
    _log_exit("mgr.stop 开始")
    mgr.stop()
    _log_exit("mgr.stop 结束")
    mgr.flush_learn()
    _log_exit("正常结束")
    return 0


def _recorded_port() -> int:
    """读「面板地址.txt」里记录的实际端口（8753 被占时服务会回退随机端口）"""
    try:
        from lp import config          # 本函数是模块级，不能靠 main() 的局部导入名
        p = os.path.join(config.APP_ROOT, "面板地址.txt")
        with open(p, encoding="utf-8") as f:
            t = f.read().strip()
        return int(t.rstrip("/").rsplit(":", 1)[-1])
    except Exception:
        return 0


def _port_is_ours(port: int) -> bool:
    """端口上是不是本程序？用 /api/status 的 JSON 特征校验，
    避免「8753 被别的程序占用」时误判成自己已在运行而拒绝启动。"""
    if not port:
        return False
    try:
        import json as _json
        import urllib.request as _ur
        op = _ur.build_opener(_ur.ProxyHandler({}))     # 绕开系统代理
        with op.open("http://127.0.0.1:%d/api/status" % port, timeout=2.0) as r:
            data = _json.loads(r.read().decode("utf-8", "replace"))
        return isinstance(data, dict) and ("knobs" in data or "mode" in data)
    except Exception:
        return False


def _already_running() -> bool:
    """本程序是否已在运行（8753 或记录端口）"""
    return _port_is_ours(8753) or _port_is_ours(_recorded_port())


def _panel_url() -> str:
    """已在运行实例的面板地址；找不到返回空串"""
    for port in (8753, _recorded_port()):
        if _port_is_ours(port):
            return "http://127.0.0.1:%d/" % port
    return ""


# =========================================================== WebView2 面板（进程隔离）
WV2_CLASS = "LaptopPowerPanelWV2"
_WV2_FLAG = [False]        # 本次运行是否被命令行强制要求用 WebView2


def _wv2_child(argv) -> int:
    """子进程：只承载 WebView2 窗口 + 永久消息泵。

    为什么要独立进程：WebView2 是进程内 COM，历史上它初始化链路一旦出问题就是
    硬崩溃（访问违例），没有任何异常可以捕获。放进子进程之后，最坏情况是"面板
    崩了"，电源管理本体（管着电源计划/独显/刷新率的那个）照常工作。
    """
    try:
        args = argv[argv.index("--wv2-child") + 1:]
        url = args[0] if args else "http://127.0.0.1:8753/"
        parent_pid = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
    except Exception:
        return 2
    try:
        from lp import webview2panel as wv
    except Exception:
        return 3
    p = wv.WebView2Panel(url)
    try:
        if not p._ensure_window():
            return 4
        if not p.ensure():
            p._pump_wait(20.0)          # ensure() 内部失败也要看 note
            _wv2_child_log("ensure 失败: %s" % p._note)
            return 5
    except Exception as e:
        _wv2_child_log("初始化异常 %r" % e)
        return 6
    end = time.time() + 25
    while time.time() < end and not p.ready and not p.failed:
        p._pump_wait(0.3)
    if not p.ready:
        _wv2_child_log("未就绪: %s failed=%s" % (p._note, p.failed))
        return 7
    _wv2_child_log("就绪 hwnd=%s parent_pid=%s" % (p.hwnd, parent_pid))

    hparent = 0
    if parent_pid:
        try:
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int,
                                        ctypes.c_ulong]
            # 关键：要 WaitForSingleObject 监听进程句柄，必须申请 SYNCHRONIZE。
            # 只申请 PROCESS_QUERY_LIMITED_INFORMATION(0x1000) 能打开句柄，
            # 但 Wait 会直接返回 WAIT_FAILED(-1)，看起来像"父进程永远不死"。
            access = 0x1000 | 0x00100000      # QUERY_LIMITED | SYNCHRONIZE
            hparent = int(k32.OpenProcess(access, False, parent_pid) or 0)
            if not hparent:
                _wv2_child_log("OpenProcess(父) 失败 err=%d" %
                               ctypes.get_last_error())
        except Exception as e:
            _wv2_child_log("OpenProcess(父) 异常 %r" % e)
            hparent = 0
    else:
        _wv2_child_log("警告：没拿到父进程 pid，失去自动回收能力")
    try:
        _beat = [0.0]
        while not p._exiting:
            p._pump_once(0.4)     # 无条件泵：就绪之后仍要处理 WM_COPYDATA/WM_CLOSE
            # 父进程没了就自动退出，绝不留下孤儿窗口
            if hparent:
                try:
                    rc = ctypes.windll.kernel32.WaitForSingleObject(
                        ctypes.c_void_p(hparent), 0)
                    if rc == 0:
                        _wv2_child_log("父进程已退出 -> 自行回收")
                        break
                    if time.time() - _beat[0] > 3:
                        _beat[0] = time.time()
                        _wv2_child_log("心跳 wait=%d" % rc)
                except Exception as e:
                    if time.time() - _beat[0] > 3:
                        _beat[0] = time.time()
                        _wv2_child_log("心跳异常 %r" % e)
            else:
                if time.time() - _beat[0] > 3:
                    _beat[0] = time.time()
                    _wv2_child_log("心跳（无父句柄）")
    finally:
        if hparent:
            try:
                ctypes.windll.kernel32.CloseHandle(hparent)
            except Exception:
                pass
    _wv2_child_log("子进程退出")
    return 0


def _wv2_child_log(line: str) -> None:
    """面板子进程自己的日志：不干扰主程序的 exit.log"""
    try:
        from lp import config
        with open(os.path.join(config.APP_ROOT, "wv2_child.log"), "a",
                  encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), line))
    except Exception:
        pass


def _wv2_find() -> int:
    """找已经存在的面板子进程窗口"""
    try:
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        return int(u.FindWindowW(WV2_CLASS, None) or 0)
    except Exception:
        return 0


def _wv2_send(cmd: str) -> bool:
    """给面板子进程发命令（带超时，子进程卡住也不会拖死主进程）"""

    class CDS(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_void_p), ("cbData", ctypes.c_uint),
                    ("lpData", ctypes.c_void_p)]

    hwnd = _wv2_find()
    if not hwnd:
        return False
    data = cmd.encode("utf-8")
    buf = ctypes.create_string_buffer(data)
    cds = CDS(0, len(data), ctypes.cast(buf, ctypes.c_void_p))
    try:
        u = ctypes.windll.user32
        u.SendMessageTimeoutW.restype = ctypes.c_int
        u.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                          ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_uint, ctypes.c_uint,
                                          ctypes.POINTER(ctypes.c_ulong)]
        res = ctypes.c_ulong()
        ok = u.SendMessageTimeoutW(ctypes.c_void_p(hwnd), 0x004A, None,
                                   ctypes.byref(cds), 0x0002, 1500,
                                   ctypes.byref(res))
        return bool(ok)
    except Exception:
        return False


def _wv2_spawn(url: str) -> bool:
    """拉起承载 WebView2 的兄弟进程"""
    pid = os.getpid()
    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--wv2-child", url, str(pid)]
        cwd = None
    else:
        cmd = [sys.executable, os.path.join(here, "main.py"),
               "--wv2-child", url, str(pid)]
        cwd = here
    flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED|NEW_GROUP|NO_WINDOW
    try:
        subprocess.Popen(cmd, cwd=cwd, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        _wv2_child_log("spawn 失败 %r" % e)
        return False


def _open_webview2(url) -> bool:
    """用 WebView2 打开面板（进程隔离版）。

    默认关闭：要在 config.json 里设 "panel_engine": "webview2"，或用命令行
    --engine=webview2 临时试一次。GDI 面板仍然是无依赖的默认形态。
    """
    try:
        engine = (_MGR[0].cfg.get("panel_engine", "auto") if _MGR[0]
                  else "auto")
    except Exception:
        engine = "auto"
    if _WV2_FLAG[0]:
        engine = "webview2"
    if engine != "webview2":
        return False
    try:
        from lp.webview2panel import WebView2Panel as _WVP   # noqa: F401
    except Exception:
        return False

    if not _wv2_find():
        if not _wv2_spawn(url):
            return False
        # 首次创建要拉 Chrome 进程树，给足时间（本机实测 ~2s，冷启动留 15s）
        for _ in range(150):
            time.sleep(0.1)
            if _wv2_find():
                break
        else:
            _wv2_child_log("等不到面板窗口")
            return False
    return _wv2_send("show\t%s" % url)


def register_hotkey(hwnd: int) -> bool:
    """Ctrl+Alt+P 打开控制面板"""
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                                     ctypes.c_uint]
        u.RegisterHotKey.restype = ctypes.c_int
        return bool(u.RegisterHotKey(hwnd, 1, 0x0002 | 0x0001, 0x50))   # CTRL|ALT, 'P')
    except Exception:
        return False


def tray_menu(mgr):
    """托盘右键菜单 —— 刻意做成微软电脑管家式的短菜单：打开/设置/开关/退出"""
    st = mgr.status()
    return [
        (traylib.MENU_OPEN, "打开", False),
        (traylib.MENU_SETTINGS, "设置", False),
        # 第三个元素是 MF_CHECKED 标志：**开着才打勾**。
        # 曾写成 `not st.get("auto")`，结果开着不打勾、关着反倒打勾，状态读反。
        (traylib.MENU_AUTO, "自动判定：%s" % ("开" if st.get("auto") else "关"),
         bool(st.get("auto"))),
        (traylib.MENU_AUTOSTART, "开机自启：%s" % ("开" if _AUTO[0] else "关"),
         bool(_AUTO[0])),
        (traylib.MENU_QUIT, "退出", False),
    ]


def on_tray_cmd(mid, url):
    from lp import config             # 本函数是模块级，config 不能靠 main() 的局部导入名
    from lp import webui
    mgr = _MGR[0]
    if mid == traylib.MENU_OPEN:
        if _open_webview2(url):
            return
        panel = _PANEL[0]
        if panel is not None and panel.show():
            return
        webui.open_browser(url)          # 原生面板不可用时回退网页
    elif mid == traylib.MENU_SETTINGS:
        # 网页高级设置已精简移除，「设置」与「打开」等效：优先原生面板
        if _open_webview2(url):
            return
        panel = _PANEL[0]
        if panel is not None and panel.show():
            return
        try:
            webui.open_browser(url)
        except Exception:
            pass
    elif mid == traylib.MENU_AUTO:
        mgr.cfg["auto_mode"] = not mgr.cfg.get("auto_mode", True)
        config.save(mgr.cfg)
        if mgr.cfg["auto_mode"]:
            mgr.set_auto()
        else:
            mgr.apply(mgr.detect(), reason="手动触发")
    elif mid == traylib.MENU_LEARN:
        from lp import config
        mgr.learn_set_enabled(not mgr.cfg.get("learn_enabled", True))
        mgr.apply(mgr.detect(), reason="切换自学习")
        mgr._learn_cache_at = 0.0
    elif mid == traylib.MENU_AUTOSTART:
        toggle_autostart(mgr)
    elif mid == traylib.MENU_RECHECK:
        mgr.manual = None
        mgr.cfg["manual_override"] = None
        mgr.apply(mgr.detect(), reason="托盘触发")
    elif mid == traylib.MENU_BALANCED:
        mgr.apply("balanced", reason="托盘：回到系统平衡")
    elif mid == traylib.MENU_ABOUT:
        try:
            from lp import hw
            info = hw.system_info()
            txt = "本机会\nCPU：%s\n显卡：%s\n机型：%s" % (
                info.get("cpu", "?"), " / ".join(info.get("gpus") or ["?"]),
                info.get("model", "?"))
            lr = mgr.learn_report()
            txt += ("\n\n自学习\n观察 %.1f 小时 · 切档 %d 次\n已记住 %d 条程序规则"
                    % (lr.get("hours", 0), lr.get("switches", 0), lr.get("rules_n", len(lr.get("rules", []))))
                    + ("" if lr.get("rules") else "（还没有，手动选几次档位试试）"))
            if _TRAY[0]:
                _TRAY[0].notify("本机信息", txt[:200])
        except Exception:
            pass
    elif mid == traylib.MENU_QUIT:
        _QUIT[0] = True


def toggle_autostart(mgr) -> bool:
    """托盘里开关开机自启"""
    from lp import config
    on = not _AUTO[0]
    ok = config.set_autostart(on)
    if ok:
        mgr.cfg["autostart"] = on
        config.save(mgr.cfg)
    _AUTO[0] = ok and on
    if _TRAY[0]:
        try:
            _TRAY[0].notify("开机自启", "已%s" % ("开启" if on else "关闭"))
        except Exception:
            pass
    return _AUTO[0]


_MGR = [None]
_TRAY = [None]
_AUTO = [False]
_QUIT = [False]
_PANEL = [None]
_WV = [None]          # WebView2 面板（None=未创建 / False=失败不再试 / 实例）
_SHOW_PANEL = [False]


def _log_boot(reason: str) -> None:
    """启动留痕（boot.log）：只记「被调用了 / 为什么没起来」。

    放在最早期，比 exit.log 的「启动」行更早 —— exit.log 那行在单实例判定和
    Manager 构造之后才写，进程若在那之前退出就完全查不到痕迹。
    """
    try:
        from lp import config as _cfg
        with open(os.path.join(_cfg.APP_ROOT, "boot.log"), "a",
                  encoding="utf-8") as f:
            f.write("%s  pid=%d  %s\n" % (time.strftime("%F %T"),
                                          os.getpid(), reason))
    except Exception:
        pass


def _log_exit(reason: str) -> None:
    """把「程序为什么退出」写进 exit.log。
    常驻程序最怕的是"悄悄没了"却查不到原因——崩溃没有 traceback，
    所以消息泵的每个出口都留一行时间戳。"""
    try:
        from lp import config as _cfg        # config 是 main() 里的局部导入名
        with open(os.path.join(_cfg.APP_ROOT, "exit.log"), "a",
                  encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%F %T"), reason))
    except Exception:
        pass


def _pump():
    """主线程消息泵"""
    u = ctypes.WinDLL("user32", use_last_error=True)
    WM_QUIT = 0x0012
    class MSG(ctypes.Structure):
        _fields_ = [("hWnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                    ("time", ctypes.c_uint), ("pt", ctypes.c_long * 2)]
    u.PeekMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p,
                               ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
    u.PeekMessageW.restype = ctypes.c_bool
    u.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p,
                              ctypes.c_uint, ctypes.c_uint]
    u.GetMessageW.restype = ctypes.c_bool
    u.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    u.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]

    msg = MSG()
    while not _QUIT[0]:
        if u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):   # PM_REMOVE
            if msg.message == WM_QUIT:
                _log_exit("收到 WM_QUIT")
                break
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        else:
            m = _MGR[0]
            if _SHOW_PANEL[0]:
                _SHOW_PANEL[0] = False
                p = _PANEL[0]
                shown = False
                if _open_webview2(_panel_url() or "http://127.0.0.1:8753/"):
                    shown = True
                elif p is not None:
                    try:
                        shown = bool(p.show())
                    except Exception:
                        shown = False
                if not shown:
                    try:
                        from lp import webui
                        webui.open_browser(_panel_url() or "http://127.0.0.1:8753/")
                    except Exception:
                        pass
            if m:
                # traylib_tray 是 main() 的局部名，这里必须用全局 _TRAY
                # （写错成 traylib_tray 会 NameError，被吞掉后托盘提示永远不刷新）
                t = _TRAY[0]
                if t is not None:
                    try:
                        m._tray = t
                    except Exception:
                        pass
                try:
                    m.pump_tick()
                except Exception:
                    pass
            time.sleep(0.05)
    _log_exit("消息泵退出（quit_flag=%s）" % _QUIT[0])
    u.PostQuitMessage(0)
    # 退出清理：还原本程序动过的硬件状态（独显 Eco 断电 / 散热旋钮 / 风扇模式 / 刷新率）
    m = _MGR[0]
    _log_exit("cleanup: 进入（mgr=%s）" % ("有" if m else "无"))
    if m:
        # 退出清理必须**留痕**。以前这里清一色 except: pass，还原失败时
        # 毫无痕迹 —— 用户只会觉得"屏幕坏了"，而日志里干干净净。
        # 2026-10-07 A-B-A-B 实测就是被这个静默坑了：刷新率和独显 Eco
        # 都没还原，基线段的功耗因此和续航档几乎一样。
        #
        # ① 先停巡检，再碰硬件。
        #    manager.stop() 只是置标志，巡检循环跑在另一个线程里 ——
        #    如果这里直接去还原，主线程和巡检线程会**同时**对同一个
        #    ATKACPI 设备句柄发 DeviceIoControl。实测后果：驱动卡死，
        #    gpu_eco.restore() 永不返回，进程退不出去（exit.log 里能看到
        #    "cleanup: gpu_eco.restore 开始" 之后再没有任何一行）。
        try:
            m.stop()
        except Exception:
            pass
        time.sleep(1.5)          # 让巡检循环从 sleep 里醒过来退出

        # ② 兜底：退出路径上任何一个硬件调用都不许无限期阻塞。
        #    驱动/固件的行为我们控制不了，但"程序能不能退出"必须可控。
        def _bounded(name, fn, timeout=12.0):
            import threading as _th
            box = {}

            def _run():
                try:
                    box["ok"] = fn()
                except Exception as e:      # noqa: BLE001
                    box["err"] = repr(e)

            t = _th.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive():
                _log_exit("!! cleanup: %s 超时 %.0fs 未返回（放弃，避免退出卡死）"
                          % (name, timeout))
                return None
            if box.get("err"):
                _log_exit("!! cleanup: %s 异常 %s" % (name, box["err"]))
                return None
            return box.get("ok")

        def _eco():
            m.gpu_eco.restore()
            return m.gpu_eco.report()

        rep = _bounded("gpu_eco.restore", _eco, 12.0)
        if rep and rep.get("eco") == 1:
            _log_exit("!! 退出后独显仍处于断电：%s"
                      % (rep.get("note") or "回读=1"))

        _bounded("thermal.hw_restore", m.thermal.hw_restore, 8.0)

        # 刷新率（2026-10-06 补）：离电降刷是 refresh_on_battery 的常驻策略，
        # 不随档位切换还原，以前退出时漏了这一项 —— 结果用户退出程序后屏幕
        # 一直停在 60Hz（插电后也不会自己回去），会被当成"屏幕坏了"。
        # 亮度不存在这个问题：亮度按 AC/DC 分开存，插电时走 AC 侧的值。
        def _disp():
            if not m.disp.restore():
                _log_exit("!! 刷新率还原失败：%s（当前 %sHz）"
                          % (m.disp.err or "?", m.disp.current_hz()))
                return False
            return True

        _bounded("disp.restore", _disp, 8.0)


if __name__ == "__main__":
    # pythonw 下没有控制台，异常会静默消失（"双击了但没反应"最常见的成因）。
    # 顶层兜底：把启动期异常写进 startup_err.log，便于事后定位。
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        try:
            import traceback
            from lp import config      # 兜底路径自己不能用 main() 的局部导入名
            with open(os.path.join(config.APP_ROOT, "startup_err.log"),
                      "a", encoding="utf-8") as f:
                f.write("%s main.py 启动异常\n%s\n" % (time.strftime("%F %T"),
                                                       traceback.format_exc()))
        except Exception:
            pass
        raise SystemExit(1)
