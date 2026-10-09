# -*- coding: utf-8 -*-
"""
场景档位定义。

每个档位用「旋钮 -> 值」描述，旋钮对应 Windows 电源计划里的一项设置。
旋钮可以配一个候选键列表（别名优先、GUID 兜底），
因此程序在任何品牌笔记本上都能跳过不存在的项，不会报错。
"""

# ------------------------------------------------------------------ 旋钮表
# key: 内部名;  value: (中文名, [候选键, ...])
KNOBS = {
    "proc_min":      ("CPU 最低频率",   ["PROCTHROTTLEMIN", "893dee8e-2bef-41e0-89c6-b55d0929964c"]),
    "proc_max":      ("CPU 最高频率",   ["PROCTHROTTLEMAX", "bc5038f7-23e0-4960-96da-33abaf5935ec"]),
    "gpu_pref":      ("显卡切换模式",   ["a1662ab2-9d34-4e53-ba8b-2639b9e20857"]),
    "amd_slider":    ("AMD 性能档位",   ["7ec1751b-60ed-4588-afb5-9819d3d77d90"]),
    "pmf":           ("PMF 控制器",     ["38cab4d5-db09-449f-9db5-1c91c909b6d4"]),
    "bright":        ("屏幕亮度",       ["VIDEONORMALLEVEL", "aded5e82-b909-4619-9949-f5d71dac0bcb"]),
    "bright_dim":    ("变暗亮度",       ["f1fbfde2-a960-4165-9f88-50667911ce96"]),
    "adapt_bright":  ("自适应亮度",     ["ADAPTBRIGHT", "fbd9aa66-9553-4097-ba44-ed6e9d65eab8"]),
    "video_idle":    ("熄屏时间(秒)",   ["VIDEOIDLE", "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e"]),
    "standby":       ("睡眠时间(秒)",   ["STANDBYIDLE", "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"]),
    "hibernate":     ("休眠时间(秒)",   ["HIBERNATEIDLE", "9d7815a6-7ee4-497e-8888-515a05f02364"]),
    "hybrid_sleep":  ("允许混合睡眠",   ["HYBRIDSLEEP", "94ac6d29-73ce-41a6-809f-6363ba21b47e"]),
    "rtc_wake":      ("允许唤醒定时器", ["RTCWAKE", "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"]),
    "disk_idle":     ("硬盘停转(秒)",   ["DISKIDLE", "6738e2c4-e8a5-4a42-b16a-e040e769756e"]),
    "usb_suspend":   ("USB 选择性暂停", ["48e6b7a6-50f5-4782-a5d4-53bb8f07e226"]),
    "aspm":          ("PCIe 节能(ASPM)",["ASPM", "ee12f906-d277-404b-b6da-e5fa1a576df5"]),
    "wifi_saver":    ("无线网卡节能",   ["12bbebe6-58d6-4636-95bb-3217ef867c1a"]),
    "js_timer":      ("IE 计时器频率",  ["4c793e7d-a264-42e1-87d3-7a0d2f523ccd"]),
    # 睿频策略（GHelper「Fans + Power」里的 Turbo Boost 档）：powercfg 查不到，
    # 但 powrprof 能读能写，实测本机可写。0=禁用 1=启用 2=激进 3=高效启用 4=高效激进
    "boost":         ("处理器性能提升模式", ["PERFBOOSTMODE",
                                             "be337238-0d82-4146-a960-4f3749d470c7"]),
}

# ------------------------------------------------------------------ 档位
# gpu_pref: 0=强制省电核显  1=优化省电  2=最佳性能  3=最大化性能(独显直连)
# amd_slider: 0=省电 1=更加省电 2=较高性能 3=最佳性能
# wifi_saver: 0=最高性能 3=最高节能
# js_timer: 0=最大电源节省 1=最高性能

PROFILES = {
    "gaming": {
        "label": "游戏 · 最高帧率",
        "icon": "🎮",
        "desc": "CPU 全核满载、独显最大化性能、不熄屏不睡眠，插电时全力输出",
        "ac": {
            "proc_min": 100, "proc_max": 100, "gpu_pref": 3, "amd_slider": 3, "pmf": 2,
            "boost": 2,
            "bright": 100, "video_idle": 0, "standby": 0, "hibernate": 0,
            "hybrid_sleep": 0, "rtc_wake": 0, "disk_idle": 0, "usb_suspend": 1,
            "aspm": 0, "wifi_saver": 0, "js_timer": 1,
        },
        # 拔电但仍在游戏中：保留帧率，砍掉一切待机耗电之外的开销
        "dc": {
            "proc_min": 25, "proc_max": 100, "gpu_pref": 2, "amd_slider": 2, "pmf": 2,
            "boost": 1,
            "bright": 100, "bright_dim": 60, "adapt_bright": 0,
            "video_idle": 0, "standby": 0, "hibernate": 0,
            "hybrid_sleep": 0, "rtc_wake": 0, "disk_idle": 0, "usb_suspend": 1,
            "aspm": 2, "wifi_saver": 1, "js_timer": 1,
        },
        "nvml": 1,          # NVIDIA 电源管理模式：1=首选最高性能
    },

    "battery": {
        "label": "续航 · 最长",
        "icon": "🌱",
        "desc": "CPU 限频、强制核显、降亮度、快速熄屏睡眠，功耗压到最低",
        "ac": {
            "proc_min": 5, "proc_max": 100, "gpu_pref": 2, "amd_slider": 2,
            "boost": 1,
            "bright": 100, "bright_dim": 50, "adapt_bright": 0,
            "video_idle": 600, "standby": 1800, "hibernate": 0,
            "hybrid_sleep": 1, "rtc_wake": 0, "disk_idle": 120, "usb_suspend": 1,
            "aspm": 0, "wifi_saver": 0, "js_timer": 1,
        },
        # 离电（本机走 AMD Radeon 890M 核显，独显处于休眠）：
        # CPU 上限压到 30% + 强制省电显卡 + 亮度降到 35% + 45 秒熄屏，
        # 是本机续航收益最大的一组设置（屏幕与 SoC 是仅剩的两个耗电大头）。
        "dc": {
            "proc_min": 5, "proc_max": 30, "gpu_pref": 0, "amd_slider": 0,
            # 睿频保留但走"高效启用"：关掉会明显卡顿，省的电不值当
            "boost": 3,
            "bright": 35, "bright_dim": 25, "adapt_bright": 1,
            "video_idle": 45, "standby": 240, "hibernate": 1800,
            "hybrid_sleep": 1, "rtc_wake": 0, "disk_idle": 45, "usb_suspend": 1,
            "aspm": 2, "wifi_saver": 3, "js_timer": 0,
        },
        "nvml": 0,          # 独显切到最低功耗模式（离电时本就该睡下去）
    },

    "saver": {
        "label": "极限续航",
        "icon": "🪫",
        "desc": "电量告急：CPU 只给 15%、屏幕最暗、2 分钟熄屏、15 分钟休眠，只求多撑一会儿",
        "ac": {             # 插电后不该停留在这档，这里只是兜底
            "proc_min": 5, "proc_max": 100, "gpu_pref": 2, "amd_slider": 2,
            "bright": 80, "bright_dim": 50, "adapt_bright": 0,
            "video_idle": 300, "standby": 900, "hibernate": 0,
            "hybrid_sleep": 1, "rtc_wake": 0, "disk_idle": 120, "usb_suspend": 1,
            "aspm": 0, "wifi_saver": 0, "js_timer": 1,
        },
        "dc": {
            "proc_min": 0, "proc_max": 15, "gpu_pref": 0, "amd_slider": 0,
            # 电量告急：直接关睿频（GHelper Silent 同思路），这一档只求多撑一会儿
            "boost": 0,
            "bright": 20, "bright_dim": 15, "adapt_bright": 1,
            "video_idle": 120, "standby": 600, "hibernate": 900,
            "hybrid_sleep": 1, "rtc_wake": 0, "disk_idle": 30, "usb_suspend": 1,
            "aspm": 2, "wifi_saver": 3, "js_timer": 0,
        },
        "nvml": 0,
    },

    "office": {
        "label": "办公 · Word / 浏览器",
        "icon": "📄",
        "desc": "响应必须跟手：不限频、独显按需分配、亮度适中，只做合理待机",
        "ac": {
            "proc_min": 5, "proc_max": 100, "gpu_pref": 2, "amd_slider": 2,
            "boost": 1,
            # 2026-10-08 用户反馈 90 太亮；75 在室内光下足够清晰
            "bright": 75, "video_idle": 600, "standby": 1200, "hibernate": 0,
            "hybrid_sleep": 1, "rtc_wake": 0, "disk_idle": 120, "usb_suspend": 1,
            "aspm": 0, "wifi_saver": 0, "js_timer": 1,
        },
        "dc": {
            "proc_min": 5, "proc_max": 70, "gpu_pref": 1, "amd_slider": 0,
            "boost": 3,
            "bright": 60, "bright_dim": 40, "adapt_bright": 0,
            "video_idle": 120, "standby": 420, "hibernate": 0,
            "hybrid_sleep": 1, "rtc_wake": 0, "disk_idle": 90, "usb_suspend": 1,
            "aspm": 2, "wifi_saver": 2, "js_timer": 0,
        },
        "nvml": None,      # 办公不强行改 NVIDIA 电源模式
    },

    "balanced": {
        "label": "系统平衡",
        "icon": "⚖️",
        "desc": "什么都不改，切回 Windows 自带的「平衡」电源计划",
        "ac": {}, "dc": {}, "nvml": None,
    },
}

# ------------------------------------------------------------------ 进程名单
DEFAULT_GAME_LIST = [
    # 游戏平台与常见游戏进程名（大小写不敏感，可自行增删）
    "steam.exe", "gameoverlayui.exe", "dota2.exe", "cs2.exe", "csgo.exe",
    "valorant.exe", "apexlegends.exe", "pubg.exe", "pubghw.exe",
    "rocketleague.exe", "overwatch.exe", "overwatch2.exe",
    "fortniteclient-win64-shipping.exe", "leagueoflegends.exe", "lol.exe",
    "genshinimpact.exe", "honkaiimpact3.exe", "bh3.exe", "ark.exe", "rust.exe",
    "deadbydaylight.exe", "thefinals.exe", "destiny2.exe", "battlefield2042.exe",
    "cod.exe", "callofduty.exe", "codlauncher.exe", "origin.exe", "uplay.exe",
    "epicgameslauncher.exe", "epicwebhelper.exe", "galaxyclient.exe",
    "goggalaxyclient.exe", "minecraft.exe", "modmanager.exe", "battle.net.exe",
    "warcraftiii.exe", "starcraft.exe", "warthunder.exe", "worldoftanks.exe",
    "worldofwarcraft.exe", "ffxiv.exe", "ffxiv_dx11.exe", "blackdesert.exe",
    "lostark.exe", "pathofexile.exe", "palworld.exe", "helldivers2.exe",
    # 2026-10-07 补：本机实机在玩 / 国内常见，但原名单漏掉的大作。
    # 巫师3 就是典型 —— 不在名单里时自动判定认不出「在打游戏」，
    # Type-C 供电下就不会走游戏档，PD 预算也就无从谈起。
    "witcher3.exe", "cyberpunk2077.exe", "eldenring.exe", "sekiro.exe",
    "darksoulsiii.exe", "nierautomata.exe", "monsterhunterworld.exe",
    "monsterhunterwilds.exe", "mhwilds.exe", "b1-win64-shipping.exe",
    "rdr2.exe", "gta5.exe", "gta6.exe", "godofwar.exe", "horizonzerodawn.exe",
    "horizonforbiddenwest.exe", "daysgone.exe", "deathstranding.exe",
    "residentevil4.exe", "re4.exe", "devilmaycry5.exe", "tekken8.exe",
    "streetfighter6.exe", "naraka.exe", "yuanshen.exe", "starrail.exe",
    "wutheringwaves.exe", "wuwa.exe", "thesims4.exe", "sims4.exe",
    "starfield.exe", "baldursgate3.exe", "bg3.exe", "helldivers.exe",
    "vgtray.exe", "igdcapm.exe", "xboxapp.exe",
    "xboxgamingservices.exe", "gamebarpresencewriter.exe", "gamesense.exe",
    "razercentral.exe", "nvidia geforce experience.exe",
]

DEFAULT_OFFICE_LIST = [
    "winword.exe", "excel.exe", "powerpnt.exe", "msaccess.exe", "outlook.exe",
    "oneNote.exe", "mspub.exe", "msedge.exe", "msedgewebview2.exe", "chrome.exe",
    "firefox.exe", "vivaldi.exe", "opera.exe", "brave.exe", "360chrome.exe",
    "qqbrowser.exe", "sogouexplorer.exe", "wangyiyun.exe", "code.exe", "devenv.exe",
    "notion.exe", "wps.exe", "et.exe", "wpscloudsvr.exe", "soffice.exe",
    "writer.exe", "masterpdf.exe", "acrobat.exe",
]

# 系统/工具进程：即使全屏也不当游戏
SYSTEM_WHITELIST = [
    "explorer.exe", "dwm.exe", "shellexperiencehost.exe", "searchhost.exe",
    "startmenuexperiencehost.exe", "textinputhost.exe", "ctfmon.exe", "zhudongfangxia.exe",
    "bingwallpaper.exe", "workbuddy.exe", "code.exe", "python.exe", "pythonw.exe",
    "powershell.exe", "cmd.exe", "windowsexplorer.exe", "applicationframehost.exe",
    "lockapp.exe", "systemsettings.exe", "snmode.exe", "widgetservice.exe", "searchapp.exe",
]


def norm_list(items):
    """把名单（可能带 .EXE 大小写）规范化成小写集合"""
    out = set()
    for x in items or []:
        s = str(x).strip().lower()
        if s:
            out.add(s if s.endswith(".exe") else s + ".exe")
    return out


def default_lists():
    return {
        "game_list": [x for x in DEFAULT_GAME_LIST],
        "office_list": [x for x in DEFAULT_OFFICE_LIST],
    }
