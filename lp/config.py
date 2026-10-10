# -*- coding: utf-8 -*-
"""配置读写（config.json，原子写 + 备份）"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Optional

from .profiles import default_lists

if getattr(sys, "frozen", False):
    # PyInstaller 打包模式：数据文件（config.json 等）跟着 exe 走，
    # 不落到 _MEIPASS 临时解包目录（那里每次退出会被删）
    APP_ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_ROOT, "config.json")

DEFAULT_CONFIG = {
    "version": 2,
    "first_run_done": False,
    "original_scheme": "",          # 系统自带「平衡」计划的 GUID
    "custom_scheme": "",             # 本程序自建的计划 GUID
    "custom_scheme_name": "笔记本自适应电源",
    "auto_mode": True,               # 自动判定开关
    "poll_seconds": 5,
    "low_battery_percent": 20,       # 低于该电量，游戏中也降级
    "manual_override": None,         # 手动锁定的档位名（null=跟随自动）
    "system_whitelist": [
        "explorer.exe", "dwm.exe", "shellexperiencehost.exe", "searchhost.exe",
        "startmenuexperiencehost.exe", "textinputhost.exe", "ctfmon.exe",
        "applicationframehost.exe", "lockapp.exe", "systemsettings.exe",
        "snmode.exe", "widgetservice.exe", "searchapp.exe", "runtimebroker.exe",
        "audiodg.exe", "conhost.exe", "shellexperience.exe", "searchmousehost.exe",
        "rtkauduservice64.exe", "rtkvhdlservice.exe", "asusoptimization.exe",
        "asuscertificationservice.exe", "armourysocketserver.exe",
        "windowsterminal.exe", "workbuddy.exe", "powershell.exe", "cmd.exe",
    ],
    "game_list": default_lists()["game_list"],
    "office_list": default_lists()["office_list"],
    "notify": False,                 # 静默优先：默认不弹任何气泡（用户 2026-10-04 要求）
    "minimize_to_tray": True,
    "autostart": False,
    "autostart_notified": True,      # 已取消"首次运行提示自启"的气泡，视为已提示过
    "learn_enabled": True,            # 自学习总开关
    "hotkey": "Ctrl+Alt+P",
    "last_profile": "balanced",

    # ---------------- 游戏档 CPU/GPU 功耗分配（2026-10-04 新增） ----------------
    # 本机实测：独显功耗墙不可软件设定，唯一控制杆是 CPU 侧；
    # 压低 CPU 上限 -> Dynamic Boost 把省下的瓦数转给独显 -> 帧数上升。
    "alloc_enabled": True,
    "power_envelope_watts": 110,      # 整机 CPU+GPU 持续功耗上限（ASUS Turbo 模式口径）
    # 独显最大总功耗：2026-10-06 按本机实测重新标定（原写 100W，与硬件不符）
    #   ATKACPI gpu_power_base = 55W，Dynamic Boost 实测再加 ~14W；
    #   power_alloc.csv 228 条真实游戏采样：峰值 68.8W / 均值 59.8W。
    #   原值 100W 会让「已到顶停止让渡」(0.97*100=97W) 永远触发不了。
    "gpu_tgp_max_watts": 70,          # 独显实际功耗上限（实测峰值 68.8W + 余量）
    # 能效甜点（借鉴极客湾 Geekerwan 功耗-性能曲线 + 超能网实测）：
    #   4060 移动版在 ~80W 之后性能收益曲线变得非常平缓
    #   （80W→100W 约 +6%，100W→110W 仅 +1%）。超过甜点后继续让瓦换不到帧数。
    "gpu_sweet_watts": 80,            # 让渡停止点 = min(物理上限, 甜点)
    "board_overhead_watts": 12,       # 主板/屏幕/风扇等固定开销
    "cpu_temp_limit_c": 96,           # CPU 温度上限（实测满载平台期 ~93℃）
    "gpu_temp_limit_c": 88,           # 独显温度上限（驱动 slowdown 阈值实测 91℃）
    "alloc_min_cpu_pct": 45,          # 让渡 CPU 时「最大处理器状态」的下限
    "alloc_step_pct": 5,              # 每次调整步长（%）
    "alloc_cooldown_seconds": 30,     # 两次调整最小间隔（秒）
    "alloc_gain_exponent": 0.35,      # 功耗→帧数换算指数（GPU boost 区间，保守）
    "alloc_log_csv": True,            # 游戏档采样写入 power_alloc.csv

    # ---------------- 离电续航（2026-10-04 新增） ----------------
    # 本机离电时跑的是 AMD Radeon 890M 核显，独显处于休眠，
    # 剩下的耗电大头只有两个：SoC（CPU+核显）与屏幕。所以离电策略围绕这两件事做。
    "battery_eco_percent": 35,        # 【当前无效】低电量进续航档阈值；离电一律续航档已完全覆盖它，代码中未读取
    "battery_saver_percent": 15,      # 拔电且低于此电量：进「极限续航」档
    "refresh_on_battery": True,       # 拔电时降低屏幕刷新率（本机 165Hz -> 60Hz）
    "dc_refresh_hz": 60,              # 拔电时用的刷新率（0 = 不改）
    "battery_eta": True,              # 面板显示实时放电功率与预计剩余时间
    "dc_brightness_cap": 45,          # 拔电时亮度封顶（%）。屏幕是离电第一耗电大户
                                      # （占 20~50%，100→50 可省 20~30% 续航）；0 = 不封顶
    "idle_dim_enabled": True,         # 拔电空闲渐进调暗（无键鼠输入后；全屏/游戏时不干预）
    "idle_dim_level": 20,             # 空闲调暗目标亮度（%）
    "idle_dim_after_s": 90,           # 无输入多久后调暗（秒）

    # ---------------- 电池充放电管理（2026-10-05 新增，文献：40-80 法则） ----------------
    # 锂电三大应力：满充搁置（100% 常驻老化约 2×）、高温（每 +10℃ 老化翻倍）、
    # 深放（<20% 阳极析锂）。本机未装 MyASUS/Armoury Crate，且 ATKACPI 全部失败
    # ⇒ 无法软件设充电阈值，只能统计行为 + 定量建议 + 充电高热时主动降温。
    "battery_care_enabled": True,        # 充放电行为统计与养护建议
    "care_full_soc_pct": 95,             # 视为「满充搁置」的电量门槛（%）
    "care_deep_soc_pct": 20,             # 视为「深度放电」的电量门槛（%）
    "care_advise_charge_limit_pct": 80,  # 建议用户在厂商工具里设的充电上限（%）
    "care_hot_charge_c": 88,             # 充电中机身热负荷 ≥ 此值视为「高温充电」
                                         # （电池温度本机不可读，用热区/独显温度代理）
    "care_heat_relief": True,            # 充电高热时临时压低 AC 侧 CPU 上限降温
    "care_heat_relief_cpu_pct": 70,      # 降温期间的 AC CPU 上限（%）
    "care_heat_relief_clear_c": 82,      # 温度回落到此值以下即还原

    # ---------------- 合盖不休眠 / 常驻瘦身（2026-10-06 新增，对标 G-Helper） ----------------
    # 笔记本接外接显示器当台式机用时，Windows 默认合盖即睡眠。开启后：检测到
    # 外接显示器且（已接电源 / 允许电池）时，把**自建计划**的合盖动作临时改为
    # 「不采取任何操作」，条件消失即还原（原值持久化在 clamshell.json）。
    # 默认关闭 —— 合盖行为是强预期，改成不动作容易让人以为机器坏了。
    "clamshell_enabled": False,
    "clamshell_on_battery": False,
    "gpu_eco_auto": True,             # 离电时独显 ACPI 断电（GHelper GPU Eco 通道，插电还原）       # 是否允许离电时也保持不休眠（默认只在插电时）
    # --- 核显优先调度（2026-10-10 新增）：核显跑得动的程序就不唤醒独显 ---
    # 走的是 Windows 自带的「图形首选项」通道（HKCU\...\UserGpuPreferences），
    # 普通权限可写、不需要驱动，且优先于厂商控制面板的程序设置。
    # 注意：偏好只对**下次启动该程序**生效，运行中的程序不受影响。
    "gpupick_enabled": True,
    # 本机核显能力档位：None=首次启动自动探测（见 gpupick.detect_igpu_tier）
    # 1=入门核显（只敢跑 tier1）2=中端（780M/Iris Xe 级）3=高性能（890M/Arc 级）
    "gpupick_igpu_tier": None,
    "gc_trim": True,                     # 常驻进程每 15 分钟压缩一次工作集（GHelper MemoryHelper 思路）
    # 面板引擎：auto = GDI 原生面板优先（稳定），失败再退浏览器；
    #            gdi = 只用 GDI；browser = 只用浏览器；
    #            webview2 = 显式启用 WebView2 独立窗口（微软电脑管家同款形态，
    #            手写 COM 初始化有崩溃风险，故不作为默认，需用户主动选择）
    "panel_engine": "auto",
    # 面板字体比例（0.85~1.3），网页用 CSS zoom 应用；窗口大小不随它变
    "panel_font_scale": 1.0,
    # --- 弱电源（USB-C PD）零补电保护：插着电却仍在放电 => 电源供不上，
    #     逐级压制到电池不再出力，避免电池高温大电流放电加速老化
    "pd_guard_enabled": True,
    "pd_margin_w": 1.5,        # 放电超过此值才算「电池在补电」
    # 12s（原 25s）：实测 Type-C 下整机 82W、电源上限约 89W，一旦超供电就是
    # 10~20W 级的大电流放电，晚 25 秒反应 = 多放 0.1Wh 高温电。12 秒既能滤掉
    # 切场景的瞬时尖峰，又不会让电池长时间补电。
    "pd_hold_s": 12.0,
    "pd_release_s": 45.0,      # 达标持续这么久才回退一级（原 90s，回退可以更快）
    "pd_low_hz": True,         # S1：降刷限帧（大头在压低 GPU 负载）
    "pd_low_hz_value": 60,
    "pd_dim_level": 50,        # S2：亮度封顶
    "pd_cpu_step": 10,         # S3：每级压低 CPU 上限的幅度
    # 60（原 40）：压 CPU 主要是「让瓦给独显」而非省总量，压到 40% 会明显
    # 拖长帧生成时间（卡顿比掉几帧更难受），60% 是实测的体验地板
    "pd_cpu_min": 60,
    # --- 预测式供电预算（2026-10-07 新增）：不等电池开始放电才动手 ----------
    # 原理：整机功耗 machine_w 与电池 rate 联立即可反推电源上限
    #   放电时：电源已满载 ⇒ 上限 = machine_w − rate（精确上界）
    #   充电时：上限 ≥ machine_w + |rate|（下界）
    # 学到上限后，一旦 machine_w 逼近上限就先限帧，把「补电」消灭在发生之前。
    "pd_predict_enabled": True,
    "pd_pre_margin_w": 8.0,    # 距离电源上限还差这么多瓦就预判为「快供不上了」
    "pd_pre_hold_s": 8.0,      # 预判成立持续多久才动手（比放电后反应更快的短确认）
    "pd_overhead_w": 12.0,     # SoC+独显之外的平台开销（屏幕/风扇/VRM 损耗）
    "pd_supply_w": None,       # 学到的电源可持续上限（程序自动写入，勿手改）
    # 大电流放电快通道：切到重场景时整机会瞬间 +20W，等满 hold 太伤电池
    "pd_fast_w": 10.0,         # 放电超过这个值就走快通道
    "pd_fast_hold_s": 3.0,     # 快通道的确认时间
    # 预判限帧后的还原门槛：负载离上限还有这么多瓦才放（防止 60/165Hz 反复跳）
    "pd_restore_margin_w": 25.0,
    # 比例门槛：整机低于「上限 × ratio」才算真正退出重负载。
    # 两个门槛取更严格的那个 —— 只有绝对余量会振荡（降刷本身就能省 20~28W，
    # 省完就"达标"→ 还原 → 又顶到上限 → 再限帧，屏幕反复黑一下）。
    "pd_restore_ratio": 0.62,
}


def _atomic_write(path: str, data: str) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cfg", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except Exception:
        pass
    # 名单字段补齐
    for key in ("game_list", "office_list"):
        if not isinstance(cfg.get(key), list) or not cfg[key]:
            cfg[key] = default_lists()[key]
    for k, v in default_lists().items():
        if not cfg.get(k):
            cfg[k] = v
    _migrate(cfg)
    return cfg


def _migrate(cfg: dict) -> None:
    """一次性标定迁移：把与本机硬件不符的历史默认值纠正过来。

    背景（2026-10-06）：gpu_tgp_max_watts 早期按文档口径写死 100W
    （"75W 基础 + 25W Dynamic Boost"），但本机实测（power_alloc.csv
    228 条真实游戏采样）峰值只有 68.8W —— 100W 会让分配器里
    「已到顶就停止让渡」(0.97*100=97W) 永远触发不了，导致一直白压 CPU。

    只在值**恰好等于旧默认值 100** 时纠正（用户若手设过其它值则保留）。
    该键没有面板入口，所以 100 一定是历史残留而非用户选择。
    """
    try:
        v = float(cfg.get("gpu_tgp_max_watts", 70))
    except Exception:
        v = 70.0
    if abs(v - 100.0) < 0.5:
        cfg["gpu_tgp_max_watts"] = DEFAULT_CONFIG["gpu_tgp_max_watts"]

    # 清理从未被读取的废弃键（写错名字或调试时误建的残留），留着只会误导排查
    for dead in ("manual",):
        if dead in cfg and dead not in DEFAULT_CONFIG:
            cfg.pop(dead, None)

    # 游戏/办公名单：新增的进程名要能补进已存在的名单（并集，不删用户自定义项）
    try:
        from .profiles import default_lists
        for key, defaults in default_lists().items():
            cur = cfg.get(key)
            if not isinstance(cur, list):
                continue
            have = {str(x).strip().lower() for x in cur}
            for name in defaults:
                if name.lower() not in have:
                    cur.append(name)
                    have.add(name.lower())
    except Exception:
        pass

    # PD 保护响应提速（2026-10-07）：只在值恰好等于旧默认值时才纠正，
    # 用户若手设过其它值一律保留。这三个键都没有面板入口。
    for key, old in (("pd_hold_s", 25.0), ("pd_release_s", 90.0),
                     ("pd_cpu_min", 40.0)):
        try:
            v = float(cfg.get(key, old))
        except Exception:
            v = old
        if abs(v - old) < 0.01:
            cfg[key] = DEFAULT_CONFIG[key]


def save(cfg: dict) -> None:
    try:
        cfg = dict(cfg)
        cfg["version"] = DEFAULT_CONFIG["version"]
        cfg["_saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------- 开机自启
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAPP = "LaptopPowerAuto"


def python_candidates() -> list:
    """候选解释器（按优先级）。APP_ROOT/_runtime 是本程序自带的运行时，排第一。"""
    cands = [
        os.path.join(APP_ROOT, "_runtime", "python", "python.exe"),
        sys.executable,
    ]
    # 托管解释器（WorkBuddy 内建）—— 必须按当前用户展开，不能写死用户名
    for base in (os.path.join(os.path.expanduser("~"),
                              ".workbuddy", "binaries", "python", "versions"),):
        try:
            if os.path.isdir(base):
                vs = sorted(os.listdir(base), reverse=True)
                if vs:
                    cands.append(os.path.join(base, vs[0], "python.exe"))
        except Exception:
            pass
    # 常见系统安装位置
    for pat in (os.path.join(p, "Python*", "python.exe")
                for p in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python"),
                          r"C:\Python", r"C:\Program Files",
                          r"C:\Program Files (x86)")):
        try:
            for p in sorted(glob.glob(pat), reverse=True):
                cands.append(p)
        except Exception:
            pass
    cands.append("python.exe")
    seen, out = set(), []
    for p in cands:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def python_exe() -> str:
    """第一个真实存在的解释器；都不存在时退回当前进程用的那个"""
    if getattr(sys, "frozen", False):
        return sys.executable
    for p in python_candidates():
        if os.sep in p:
            if os.path.isfile(p):
                return p
        elif shutil.which(p):
            return shutil.which(p)
    return sys.executable


def launcher() -> str:
    """返回启动用的命令（优先无窗口的 pythonw，其次带窗口的 python）"""
    exe = python_exe()
    if os.path.isabs(exe):
        base = os.path.splitext(exe)[0]
        pw = base + "w.exe"
        if os.path.isfile(pw):
            return '"%s"' % pw
    return '"%s"' % exe


VBS_NAME = "静默启动.vbs"
EXE_NAME = "笔记本电源自适应.exe"


def exe_path() -> Optional[str]:
    """
    本程序打包后的 exe 路径；不存在返回 None。

    **不能用 sys.frozen 判断**（2026-10-08 修）：源码跑一次就会把
    `LaptopPowerAuto` Run 键改写成 `wscript.exe //B 静默启动.vbs`，
    把上一轮刚改好的「直启 exe」覆盖掉 —— 表现就是开机自启悄悄失效、
    而注册表看起来"明明有这一项"。判据必须是**exe 这个文件在不在**，
    与当前进程是 exe 还是 python 无关。
    """
    if getattr(sys, "frozen", False):
        return sys.executable
    p = os.path.join(APP_ROOT, EXE_NAME)
    return p if os.path.isfile(p) else None


def _pyw_candidates() -> list:
    """候选解释器：优先无窗口版 pythonw，其次 python.exe"""
    out = []
    for p in python_candidates():
        if not os.sep in p:
            continue                      # 裸名字交给 vbs 走 PATH，效果一样
        base = os.path.splitext(p)[0]
        pw = base + "w.exe"
        if os.path.isfile(pw):
            out.append(pw)
        elif os.path.isfile(p):
            out.append(p)
    return out


def autostart_cmd() -> str:
    """
    开机只挂一个键。

    **exe 在就直接指 exe 本体**（无论当前是不是打包版在跑），不再经
    wscript //B 跑 vbs（2026-10-07 实测：开机没起来，boot.log 连一行都没有
    —— wscript/vbs 这一环没把 exe 拉起来。exe 是 --noconsole 的，本身不弹窗，
    去掉中间层反而更稳：少一个进程、少一个被安全软件拦的面）。

    只有 exe 不在（纯源码开发、还没打包）时才退回 vbs。
    """
    exe = exe_path()
    if exe:
        return '"%s" --autostart' % exe
    path = os.path.join(APP_ROOT, VBS_NAME)
    return 'wscript.exe //B "%s"' % path


def _write_vbs() -> Optional[str]:
    """生成/刷新 vbs：单行 Array（VBScript 换行必须用 " _" 续行，多行会 800A03EA 语法错误）；
    找不到解释器时绝不弹窗（wscript 的 Echo 会弹框打扰用户），只写一行日志留痕。
    exe 模式（frozen）直接拉起 exe 本体，不需要挑解释器。"""
    path = os.path.join(APP_ROOT, VBS_NAME)
    exe = exe_path()
    if exe:
        target = exe
        run_line = ('sh.Run """" & app & """", 0, False\r\n'
                    'WScript.Quit 0\r\n')
    else:
        target = os.path.join(APP_ROOT, "main.py")
        if not os.path.isfile(target):
            return None
        cands = _pyw_candidates() or ["pythonw.exe", "python.exe"]
        arr = ",".join('"%s"' % p for p in cands)
        run_line = ('cands = Array(%s)\r\n'
                    'For i = 0 To UBound(cands)\r\n'
                    '    py = cands(i)\r\n'
                    '    If fso.FileExists(py) Then\r\n'
                    '        sh.CurrentDirectory = fso.GetParentFolderName(app)\r\n'
                    '        sh.Run """" & py & """ """ & app & """", 0, False\r\n'
                    '        WScript.Quit 0\r\n'
                    '    End If\r\n'
                    'Next\r\n') % arr
    try:
        vbs = (
            'Option Explicit\r\n'
            'Dim py, fso, sh, app, ts\r\n'
            'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
            'Set sh = CreateObject("Wscript.Shell")\r\n'
            'app = "%s"\r\n'
            'sh.CurrentDirectory = fso.GetParentFolderName(app)\r\n'
            '%s'
            'On Error Resume Next\r\n'
            'Set ts = fso.OpenTextFile(fso.GetParentFolderName(app) & "\\autostart_fail.log", 8, True)\r\n'
            'ts.WriteLine Now & " launch failed"\r\n'
            'ts.Close\r\n'
        ) % (target, run_line)
        # wscript 按系统 ANSI 代码页读取脚本，不能用 utf-8 / ascii
        with open(path, "w", encoding="mbcs", newline="\r\n") as f:
            f.write(vbs)
        return path
    except Exception:
        return None


def _del_run_value(name: str) -> None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except Exception:
        pass


def set_autostart(enabled: bool) -> bool:
    """只挂一个 Run 键指向 vbs；关闭时键、老键、vbs 一起清干净"""
    # 老版本写过的副键（键名带 Vbs 后缀）顺手清掉
    _del_run_value(LAPP + "Vbs")
    if enabled:
        if not _write_vbs():
            _del_run_value(LAPP)
            return False
        _del_run_value(LAPP)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, LAPP, 0, winreg.REG_SZ, autostart_cmd())
            return True
        except Exception:
            return False

    _del_run_value(LAPP)
    # vbs 必须一并删掉，否则 autostart_enabled() 仍会读到 True
    try:
        p = os.path.join(APP_ROOT, VBS_NAME)
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass
    return True


def autostart_enabled() -> bool:
    """认 Run 键 + vbs 文件 +「启动」文件夹里的快捷方式"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            try:
                if winreg.QueryValueEx(k, LAPP)[0]:
                    return True
            except FileNotFoundError:
                pass
    except Exception:
        pass
    # vbs 兜底**只在没有 exe 时算数**（2026-10-08 修）：exe 明明在、Run 键却
    # 指向 vbs（或 Run 键压根没有）时，这里也会报 True，把"自启挂错了/挂不上"
    # 掩盖成"已开启" —— 托盘显示一切正常，实际开机一次都没起来。
    if not exe_path():
        try:
            if os.path.isfile(os.path.join(APP_ROOT, VBS_NAME)):
                return True
        except Exception:
            pass
    try:
        d = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
        return any("电源" in f or "Power" in f or "power" in f
                   for f in os.listdir(d)) if os.path.isdir(d) else False
    except Exception:
        return False
