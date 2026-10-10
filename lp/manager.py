# -*- coding: utf-8 -*-
"""
核心调度：探测场景 -> 套用档位 -> 还原

档位依赖一个「自建电源计划」承载所有写入值，
系统自带的「平衡」计划一个字节都不动，随时可一键回到原厂状态。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

import os
import sys

from . import algo, config, hw, powercfgctl as pc
from .algo import LoadTracker, SwitchGovernor, build_reason
from .alloc import ACTION_CN, VERDICT_CN, PowerAllocator
from .battery import BatteryMonitor
from .batterycare import BatteryCare
from .brightness import BrightnessCtl
from .clamshell import ClamshellCtl
from .display import DisplayCtl
from .learner import Learner
from .nvmlctl import Nvml, PM_MAXIMUM, PM_MINIMUM
from .pdbudget import PdBudget
from .pdsource import PdSource
from .power import PowerMonitor
from .profiles import KNOBS, PROFILES, norm_list
from .thermal import ThermalPolicy

APP_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def fmt_minutes(m) -> str:
    """把分钟数写成「3 小时 12 分」这种人话"""
    try:
        m = int(m)
    except Exception:
        return "—"
    if m <= 0:
        return "—"
    if m < 60:
        return "%d 分钟" % m
    return "%d 小时 %02d 分" % (m // 60, m % 60)


class Manager:
    def __init__(self, cfg: dict, on_change: Optional[Callable[[dict], None]] = None,
                 on_notify: Optional[Callable[[str, str], None]] = None):
        self.cfg = cfg
        self.on_change = on_change
        self.on_notify = on_notify
        self.current = None          # 首次 apply 前不假设任何档位，避免漏掉初始化
        self.manual = cfg.get("manual_override") or None
        # 静默底线下的可诊断性：吞咽的异常进 error.log（只写文件，不弹任何东西）
        try:
            from . import errlog
            errlog.set_path(os.path.join(APP_ROOT, "error.log"))
            errlog.set_enabled(bool(cfg.get("error_log", True)))
        except Exception:
            pass
        self._lock = threading.RLock()
        self._plan_cache: Dict[str, pc.PowerPlan] = {}
        self._nv_prev: Optional[int] = None
        self.nvml = Nvml()
        self.last_reason = ""
        self.last_changed: List[str] = []
        self.learner = Learner()
        self._applied_at = 0.0
        self._learn_cache: dict = {}
        self._learn_cache_at = 0.0
        self._cand = None
        self._cand_n = 0

        # 算法层：负载追踪（EWMA）+ 切换闸门（滞回/冷却/抢占）
        self.tracker = LoadTracker()
        self.governor = SwitchGovernor()
        self.last_summary: dict = {}
        # 场景「粘性」：刚进过某档的短时间内，不因瞬时波动退回
        self._scene_until = 0.0

        # 功耗层：实时温度/功耗监测（PDH+NVML）+ CPU/GPU 功耗分配器
        self.pmon = PowerMonitor(self.nvml)
        self._wire_temp_backup()
        self.last_power: dict = {}
        self._power_at = 0.0           # last_power 的采样时刻（判断新鲜度用）
        self.alloc = PowerAllocator(cfg, log_path=os.path.join(APP_ROOT, "power_alloc.csv"))
        self._alloc_applied: Optional[int] = None
        # 弱电源（100W PD）零补电保护：插电却在放电 => 逐级压制，电池退出供电
        self.pd = PdBudget(cfg)
        self._pd_at = 0.0
        self._pd_hz = 0                # 本模块降到的刷新率（0=未降）
        self._pd_dim = False           # 本模块是否压着亮度
        # 供电能力辨识（2026-10-10）：供电能力是**当前这个充电器**的属性，
        # 不是机器的属性。换充电器必然经历一次拔电 → 新会话 → 作废旧值重新现测。
        self.pdsrc = None
        try:
            self.pdsrc = PdSource()
        except Exception:
            self.pdsrc = None

        # 离电层：电池遥测（ACPI）+ 屏幕刷新率控制 + 亮度优化
        self.bat = BatteryMonitor()
        self.disp = DisplayCtl()
        self.bright = BrightnessCtl()
        # 硬件自适应档案（2026-10-10）：源码里不带任何一台机器的标定数字，
        # 第一次启动（或档案超过 7 天）在**本机**现测一次 —— 独显有无/功耗上限/
        # 温度线/屏幕刷新率档位/厂商通道，并把结果填进 config 的自动位（None）。
        # 游戏档还会按实测到的独显功耗峰值持续修正「让渡停止点」。
        self.hwprof = None
        try:
            from . import hwprofile
            self.hwprof = hwprofile.ensure(cfg, nvml=self.nvml, display=self.disp)
            if getattr(self.hwprof, "last_filled", None):
                try:
                    config.save(cfg)
                except Exception:
                    pass
            if self.hwprof is not None:
                self.alloc.set_hw_tgp(self.hwprof.gpu_tgp_max())
                # 高端机的功耗真值别被通用钳制当成坏值丢掉
                self.pmon.set_watt_limits(self.hwprof.watt_limits())
        except Exception:
            pass
        # 充放电管理：满充搁置/高温充电/深放统计 + 充电高热时临时降温
        self.care = BatteryCare(cfg, data_path=os.path.join(APP_ROOT, "battery_care.json"),
                                scheme_guid=cfg.get("custom_scheme") or None)
        # 散热/噪音策略：软件杠杆（CPU 上限/温度线）+ 硬件旋钮
        # （SYSCOOLPOL 散热方式 / PERFBOOSTMODE 睿频策略 / PERFEPP 能效偏好，
        #   经 powrprof 直写**自建计划**，系统「平衡」绝不碰）
        self.thermal = ThermalPolicy(cfg)
        self.thermal.scheme = cfg.get("custom_scheme") or None
        self._thermal_at = 0.0
        self._thermal_note = ""
        # 合盖不休眠（对标 GHelper ClamshellModeControl）：外接屏 + 插电时
        # 把自建计划的 LIDACTION 临时改成 0（不动作），条件不满足即还原。
        self.clamshell = ClamshellCtl(cfg, cfg.get("custom_scheme") or None,
                                      data_path=os.path.join(APP_ROOT, "clamshell.json"))
        self._clam_at = 0.0
        # 独显 ACPI 断电（借鉴 GHelper GPU Eco，2026-10-06 本机打通可写）：
        # 离电时把 RTX 4060 在 ACPI 层彻底断电，插电还原；游戏档例外。
        from .gpueco import GpuEco
        self.gpu_eco = GpuEco()
        self.gpu_eco.enabled = bool(cfg.get("gpu_eco_auto", True))
        # 核显优先调度（2026-10-10 新增）：核显跑得动的程序不唤醒独显。
        # 本机核显档位首次运行自动探测一次并写回 config，之后以配置为准。
        from .gpupick import GpuPick, detect_igpu_tier
        self.gpupick = GpuPick()
        if cfg.get("gpupick_igpu_tier") is None:
            cfg["gpupick_igpu_tier"] = detect_igpu_tier()
            try:
                from . import config as _cfgmod
                _cfgmod.save(cfg)
            except Exception:
                pass
        self.gpupick.reload_cfg(cfg)
        # 常驻进程内存瘦身（对标 GHelper MemoryHelper.SetProcessWorkingSetSize）
        self._trim_at = 0.0
        self._trim_last_mb = 0.0
        self._care_at = 0.0
        self._env = {"items": [], "hints": []}
        self._env_at = 0.0
        self.last_battery: dict = {}
        self.last_display: dict = {}
        self._top_procs: list = []
        self._disp_fail_at = 0.0
        self._bat_n = 0
        # 亮度控制状态：applied=我们最后写入/确认的 DC 亮度；base=调暗前基准；
        # user_cap=用户手动改亮度后本次离电会话尊重其值
        self._br_applied = None
        self._br_base = None
        self._br_dimmed = False
        self._br_user_cap = None
        self._br_was_ac = None
        self._br_n = 0
        self._br_fail_at = 0.0
        # 亮度/还原动作走 SCHEME_CURRENT：必须确认活动计划是自建计划
        self._sch_ok_at = 0.0
        self._sch_ok_val = False
        # 原因文本刷新计时（拔电/插电但档位没变时，原因也要跟着变）
        self._reason_at = 0.0
        # 最近一次「非本程序」的前台窗口（面板当前台时，投票/记忆仍归真正的程序）
        self._last_fg: Optional[dict] = None
        # 名单外窗口化游戏记忆（如《信長之野望》这类窗口模式进程）：
        # 独显高负载 + 未知前台进程时识别，进程退出前一直按游戏档
        self._auto_game: Optional[str] = None
        # —— CPU 限频卡死看门狗（2026-10-06 事故固化）——
        # 症状：内核「已生效」的处理器上限被卡在 30%（600MHz），计划数据库里
        # 读出来却是 100，光切换计划刷不掉；必须「写值 + 重新激活」才恢复。
        # 实测恢复后 8.2M/s -> 68M/s（8 倍）。
        self._stall_n = 0          # 连续「高占用+低性能」采样数
        self._ppm_stage = 0        # 已用到的修复档位（0=无 1=重激活 2=借道刷新）
        self._ppm_last_at = 0.0
        self._ppm_note = ""
        self._ppm_fixes = 0

    def _confirm_needed(self, target: str) -> int:
        """确认次数：学习器给基线，算法层按场景类型加权。
        升档（进游戏/离开省电）要快 —— 玩家不想等；
        降档（回平衡/省电）慢一点 —— 避免切完窗口立刻掉性能。"""
        base = 1
        try:
            base = int(self.learner.confirm_needed(target or "balanced"))
        except Exception:
            base = 1
        if target == "gaming":
            return max(1, base - 1)          # 抢占式，优先保证帧率
        if target in ("battery", "saver", "balanced"):
            return max(2, base + 1)          # 稳一点再降
        return max(1, base)

    # ------------------------------------------------------------ 计划准备
    def ensure_scheme(self) -> str:
        """创建/复用自建电源计划（以当前系统平衡计划为模板）"""
        cfg = self.cfg
        guid = cfg.get("custom_scheme") or ""
        if guid and pc.list_schemes().get(guid):
            return guid

        base = cfg.get("original_scheme") or pc.active_scheme()
        if not base:
            schemes = pc.list_schemes()
            base = next(iter(schemes), "")
        if not base:
            base = "381b4222-f694-41f0-9685-ff5bb260df2e"

        cfg["original_scheme"] = base
        new = pc.duplicate_scheme(base)
        if not new:
            raise RuntimeError("创建电源计划失败，请确认以管理员身份运行")
        pc.rename_scheme(new, cfg.get("custom_scheme_name", "笔记本自适应电源"))
        cfg["custom_scheme"] = new
        config.save(cfg)
        self._plan_cache.pop(base, None)
        self._plan_cache.pop(new, None)
        return new

    def _plan(self, scheme: str) -> pc.PowerPlan:
        if scheme not in self._plan_cache:
            self._plan_cache[scheme] = pc.load_plan(scheme)
            if len(self._plan_cache) > 4:
                # 先进先出：淘汰最久未重新加载的那个（dict.popitem 是 LIFO，
                # 会把刚放进来的这个弹出去，等于缓存永远失效）
                self._plan_cache.pop(next(iter(self._plan_cache)), None)
        return self._plan_cache[scheme]

    # ------------------------------------------------------------ 场景判定
    def detect(self, summary: Optional[dict] = None,
               fg: Optional[dict] = None) -> str:
        """返回自动判定出的档位名。

        summary 为 algo.LoadTracker 的负载摘要（可为 None，退化为纯场景判定）。
        fg 为调用方采好的前台窗口信息（不传则现采一次；主循环每轮只采一次，
        避免 5 秒内多次 GetForegroundWindow 结果互相矛盾）。
        判定顺序： 游戏 → 办公 → 拔电续航 → 平衡，中间插一层「重负载保护」。
        """
        if fg is None:
            fg = hw.foreground()
        if fg.get("process"):
            self._last_fg = dict(fg)
        st = hw.power_status()
        on_ac = bool(st.get("ac"))
        procs = hw.list_processes()
        fexe = (fg.get("process") or "").lower()
        fullscreen = bool(fg.get("fullscreen"))

        games = norm_list(self.cfg.get("game_list"))
        offices = norm_list(self.cfg.get("office_list"))
        system = norm_list(self.cfg.get("system_whitelist") or MANUAL_SYSTEM_WHITELIST)

        is_game = False
        if fexe in games:
            is_game = True
        elif fullscreen and fexe and fexe not in offices and fexe not in system:
            # 全屏独占窗口且不是办公/系统程序 -> 视为游戏
            is_game = True
        else:
            # 即使没有前台窗口，游戏进程在跑也算（如后台启动器 / 全屏游戏失去前台）
            for exe in games:
                if exe in procs and exe not in ("steam.exe", "gameoverlayui.exe",
                                                "epicgameslauncher.exe", "battle.net.exe"):
                    is_game = True
                    break

        # 名单外窗口化游戏识别（仅插电）：前台是未知进程 + 独显持续高负载。
        # 典型场景：窗口模式的《信長之野望》既不在游戏名单、又不是全屏，
        # 会被当成「重负载 → 办公档」，功耗分配器（CPU↔GPU 让渡）完全不工作。
        # 浏览器/办公程序在 office/system 名单里不会被误判；4K 视频播放的
        # 独显占用一般 <30%，50% 门槛足够安全。阈值可配 auto_game_gpu_pct。
        gpu_smooth = (summary or {}).get("gpu")
        auto_gpu = float(self.cfg.get("auto_game_gpu_pct", 50))
        if (on_ac and not is_game and fexe and fexe not in offices
                and fexe not in system and gpu_smooth is not None
                and float(gpu_smooth) >= auto_gpu):
            is_game = True
            self._auto_game = fexe
        elif self._auto_game:
            # 记忆中的名单外游戏：进程还在就维持游戏档（Alt-tab 查攻略不退档）
            if self._auto_game in procs and on_ac and (
                    fexe == self._auto_game
                    or (gpu_smooth is not None and float(gpu_smooth) >= 20.0)):
                is_game = True
            else:
                self._auto_game = None

        is_office = bool(fexe and fexe in offices)
        if not is_office and not fexe:
            # 没有前台窗口（在桌面/锁屏）但办公程序在跑 -> 按办公处理
            is_office = bool(offices & set(procs.keys()))

        # 负载信号（算法层）——用于「重负载保护」，解决"前台是浏览器但在导出视频"这类误判
        heavy = bool(summary and summary.get("heavy"))

        pct = st.get("battery_percent")
        # 注：battery_eco_percent（低电量进续航档的阈值）目前**不参与判定** ——
        # 离电一律续航档是更严格的规则，已完全覆盖它。配置项保留仅供将来参考，
        # 这里不再读取（2026-10-07 清理：读了不用的死变量会让维护者误以为生效）。
        saver_pct = int(self.cfg.get("battery_saver_percent", 15))

        if is_game:
            # 游戏档不再分低电量档：离电也走游戏档（写入项本身就有 DC 版参数）
            target = "gaming"
        elif (not on_ac) and pct is not None and pct <= saver_pct:
            # 电量告急：除了打游戏，其余一律先保电量（亮度/CPU/刷新率全压到底）
            target = "saver"
        elif not on_ac:
            # 离电铁律：除游戏外一律续航档（办公/重负载也不例外——
            # 重负载想全速可以插电，或手动锁定档位）
            target = "battery"
        elif heavy:
            # 持续重负载（剪辑/编译/渲染）：插电时不降频、不激进省电
            target = "office"
        elif is_office:
            target = "office"
        else:
            target = "balanced"

        # 自学习：这个程序以前被你安排过别的档位，照你的来
        if self.cfg.get("learn_enabled", True) and fexe:
            r = self.learner.rule_for(fexe, bool(st.get("ac")))
            if r and r in PROFILES and r != target:
                # 游戏/极限省电优先级最高，学习结果不覆盖；
                # 离电时学习结果若是耗电档（office/balanced）也压回续航
                if is_game or target == "saver":
                    pass
                elif not on_ac and r != "gaming":
                    target = "battery"
                else:
                    target = r
        return target

    # ------------------------------------------------------------ 档位套用
    def apply(self, name: str, reason: str = "", dry: bool = False,
              fg: Optional[dict] = None) -> dict:
        """套用档位；dry=True 时只做预览，不真正写入。返回预览/结果信息"""
        name = name if name in PROFILES else "balanced"
        with self._lock:
            prof = PROFILES[name]
            st = hw.power_status()
            on_battery = not st.get("ac")
            changed: List[str] = []
            result = {"profile": name, "on_battery": on_battery, "changed": [],
                      "nvml": prof.get("nvml")}

            # 先把上一档位「待了多久」交给学习器，用来判断判定是不是太急
            if self.current and self.current != name and self._applied_at:
                self.learner.feed(self.current, time.time() - self._applied_at)
            # 离开游戏档时清掉功耗分配器压下的上限记录。留着它会让
            # _expected_proc_max() 在后续档位继续取 min(…, 60)，看门狗误判为
            # 「本来就该 60%」而不再武装 —— 限频卡死自愈失效。
            if name != "gaming":
                self._alloc_applied = None
            self._applied_at = time.time()

            if name == "balanced":
                base = self.cfg.get("original_scheme") or pc.active_scheme()
                if dry:
                    result["note"] = "预览：切回系统自带计划 %s" % (base[:8] or "?")
                    return result
                # 切走之前先把「临时压过的 CPU 上限 / 亮度」还原 —— 此时活动计划
                # 还是自建计划，还原写入落点正确；否则切到系统计划后再还原，
                # 会把值写进系统「平衡」计划（违反硬性约束）。
                try:
                    self.care.reset()
                except Exception:
                    pass
                # 散热策略接管的硬件旋钮（散热方式/睿频策略/能效偏好）也要还原
                try:
                    self.thermal.hw_restore()
                except Exception:
                    pass
                # 合盖动作同理：切回系统计划后写入不再生效，先把自建计划里的值还原
                try:
                    self.clamshell.reset(reason="已切回系统平衡，还原合盖动作")
                except Exception:
                    pass
                self._br_reset()
                if base and base != self.cfg.get("custom_scheme"):
                    pc.set_active(base)
                else:
                    # original_scheme 配置异常（等于自建计划或已被删除）：
                    # 回退到当前活动计划，绝不把自建计划当"系统计划"
                    base = pc.active_scheme()
                    if base and base != self.cfg.get("custom_scheme"):
                        pc.set_active(base)
                self._restore_nvml()
                self.current = "balanced"
                self.last_reason = reason or "回到系统平衡"
                self.last_changed = []
                self.forget_scheme_ok()       # 现在活动的是系统计划
                self._emit()
                return result

            scheme = self.ensure_scheme()
            result["scheme"] = scheme

            # 1) 写入电源计划（AC / DC 两行都写，来回插拔时取值始终一致）
            for flag in ("dc", "ac"):
                on_bat = (flag == "dc")
                vals = prof.get("dc") if on_bat else prof.get("ac", {})
                for knob, val in vals.items():
                    meta = KNOBS.get(knob)
                    if not meta:
                        continue
                    # 睿频策略与散热策略写的是同一个旋钮（PERFBOOSTMODE）。
                    # 散热 quiet/perf 是用户的显式选择，优先级更高：此时档位不再写
                    # boost，否则两条链路每轮互相覆盖，值永远在抖。
                    if (knob == "boost"
                            and getattr(self.thermal, "mode", "auto") != "auto"):
                        continue
                    # 自愈：反复写不进去的项直接跳过，别每次轮询都白试
                    if self.learner.is_disabled(name, knob):
                        continue
                    # 自学习：你手动定过的值优先
                    if self.cfg.get("learn_enabled", True):
                        val = self.learner.knob_value(name, knob, val)
                    # 散热策略封顶：静音档把非游戏档的 CPU 上限压到策略值
                    val = self.thermal.apply_to_knobs(name, not on_bat, knob, val)
                    for key in meta[1]:
                        s = self._plan(scheme).get(key)
                        if s is None:
                            continue
                        if s.current(on_bat) == val:
                            break
                        if s.vmax and val > s.vmax:
                            val = s.vmax
                        if s.vmin and val < s.vmin:
                            val = s.vmin
                        result["changed"].append("%s:%s→%s" % (meta[0], s.current(on_bat), val))
                        if dry:
                            # 预览：只报告，不写、不改缓存（否则预览一次就把
                            # 缓存填成目标值，用户真正套用时全被判为"已设置"而跳过）
                            break
                        if pc.set_value(scheme, s, val, on_bat):
                            changed.append(knob)
                            # 只有真写成功才同步缓存，避免失败项被永久跳过
                            if on_bat:
                                s.dc = val
                            else:
                                s.ac = val
                        else:
                            self.learner.note_fail(name, knob)
                            result.setdefault("failed", []).append(meta[0])
                        break

            if dry:
                result["note"] = "预览：%d 项待写入，随后切到自建计划" % len(result["changed"])
                return result

            # 2) 切到自建计划（亮度 / 显卡切换在这里一起生效）
            pc.set_active(scheme)
            self._sch_ok_at = time.time()     # 刚刚切到自建计划，缓存直接置真
            self._sch_ok_val = True

            # 3) NVIDIA 电源管理模式（**只在插电时碰独显**：离电走 AMD 核显，
            #    读/写 NVML 会把独显驱动唤醒，既违约束又毁续航）
            if prof.get("nvml") is not None and self.nvml.ready and not on_battery:
                prev = self.nvml.mode()
                # 只在首次记录用户原始模式：重复 apply 同一档时读到的已是
                # 我们自己写进去的值，再记就永远还原不回去了
                if prev is not None and self._nv_prev is None:
                    self._nv_prev = prev
                if self.nvml.set_mode(prof["nvml"]):
                    changed.append("nvml")

            if self.cfg.get("learn_enabled", True):
                if fg is None:
                    fg = hw.foreground()
                self.learner.note_switch((fg.get("process") or "").lower(), name,
                                         bool(st.get("ac")))
            self.cfg["last_profile"] = name
            config.save(self.cfg)
            self.current = name
            self.last_reason = reason or name
            self.last_changed = changed
            # 切档会重写亮度 -> 亮度控制重新基线；充电降温的上限也要还原
            self._br_reset()
            try:
                self.care.reset()
            except Exception:
                pass
            self._emit()
            return result

    def _restore_nvml(self) -> None:
        # 离电时不碰独显（唤醒驱动会毁续航）；留着待下次插电再还原
        try:
            if self._nv_prev is not None and not hw.power_status().get("ac"):
                return
        except Exception:
            pass
        if self._nv_prev is not None and self.nvml.ready:
            ok = False
            try:
                ok = bool(self.nvml.set_mode(self._nv_prev))
            except Exception:
                ok = False
            # 还原失败时**不能**清掉记录：那是用户原本的 NVIDIA 电源管理模式，
            # 抹掉就再也不会还原，机器会被永久留在我们写入的模式上。
            if ok:
                self._nv_prev = None

    def _emit(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.status())
            except Exception:
                pass
        if self.on_notify and self.cfg.get("notify", False):
            try:
                cur = self.current if self.current in PROFILES else "balanced"
                self.on_notify(PROFILES[cur]["label"], self.status()["reason"])
            except Exception:
                pass

    # ------------------------------------------------------------ 手动锁定
    def set_manual(self, name: Optional[str]) -> None:
        """手动选档 = 给当前前台程序投一票，攒够了就记住"""
        self.manual = name
        self.cfg["manual_override"] = name
        config.save(self.cfg)

        reason = "手动指定" if name else "取消锁定"
        learned = ""
        if name and self.cfg.get("learn_enabled", True):
            # 用户此刻可能正开着本程序的面板点按钮（前台=自己，已被排除）——
            # 投票记到「最近一次真实前台程序」头上才符合意图
            fg = hw.foreground()
            if not fg.get("process"):
                fg = self._last_fg or fg
            exe = (fg.get("process") or "").lower()
            if exe and name in PROFILES:
                got = self.learner.vote(exe, name, bool(hw.power_status().get("ac")))
                if got:
                    learned = "，已记住 %s" % exe
                    reason = "手动指定%s" % learned
        self.apply(name or self.detect(), reason=reason)

    def set_auto(self) -> None:
        self.manual = None
        self.cfg["manual_override"] = None
        config.save(self.cfg)
        self.apply(self.detect(), reason="切换到自动")

    # ------------------------------------------------------------ 自学习接口
    def learn_report(self) -> dict:
        if time.time() - self._learn_cache_at > 20:
            try:
                self._learn_cache = self.learner.report(bool(hw.power_status().get("ac")))
            except Exception:
                self._learn_cache = {}
            self._learn_cache_at = time.time()
        return self._learn_cache

    def learn_reset(self) -> None:
        try:
            self.learner.reset()
        except Exception:
            pass
        self.current = None
        self.apply(self.detect(), reason="学习数据已清空")

    def learn_set_enabled(self, on: bool) -> None:
        self.cfg["learn_enabled"] = bool(on)
        config.save(self.cfg)

    def learn_forget(self, exe: str) -> None:
        self.learner.unlearn(exe)

    def learn_revive(self) -> None:
        self.learner.revive_all()

    def set_knob_pref(self, mode: str, knob: str, value: int) -> dict:
        """手动改某个旋钮并记进偏好（下次同一档位沿用）"""
        meta = KNOBS.get(knob)
        if not meta or mode not in PROFILES:
            return {"error": "bad knob"}
        cur = self.status()["knobs"].get(knob, {}).get("value")
        self.learner.set_pref(mode, knob, int(value),
                              int(cur) if cur not in (None, "—") else None)
        self.apply(mode, reason="按你的偏好调整")
        return {"mode": mode, "knob": knob, "value": int(value)}

    def clear_knob_pref(self, mode: str, knob: str) -> dict:
        """取消某个档位某个旋钮的「记忆」，回到该档位出厂参数（面板 ⟲ 按钮）。

        旧实现是「拿当前值再记一遍」，等于什么都没取消 —— 这里真正清掉偏好，
        然后重新 apply 该档位，让它回落到 profiles 里的默认值。
        """
        if knob not in KNOBS or mode not in PROFILES:
            return {"error": "bad knob"}
        try:
            self.learner.clear_knob_pref(mode, knob)
        except Exception:
            pass
        # 清完偏好后重新套用，让计划值回到档位默认
        try:
            self._plan_cache.pop(self.cfg.get("custom_scheme") or "", None)
        except Exception:
            pass
        if self.current == mode:
            self.apply(mode, reason="已取消该旋钮的记忆，恢复默认")
        return {"mode": mode, "knob": knob, "cleared": True}

    def flush_learn(self) -> None:
        try:
            self.learner.flush()
        except Exception:
            pass

    # ------------------------------------------------------------ 状态
    def status(self) -> dict:
        st = hw.power_status()
        fg = hw.foreground()
        scheme = pc.active_scheme()
        plan = self._plan(scheme) if scheme else pc.PowerPlan("")
        on_battery = not st.get("ac")
        knobs = {}
        for k, meta in KNOBS.items():
            s = plan.get(meta[1][0])
            if s is None:
                s = plan.get(meta[1][1])
            knobs[k] = {"name": meta[0], "value": s.current(on_battery) if s else None,
                        "options": dict(s.options) if s else {}}
        cur = self.current and self.current in PROFILES and self.current or "balanced"
        return {
            "mode": cur,
            "mode_label": PROFILES[cur]["label"],
            "reason": self.last_reason,
            "ac": st.get("ac"),
            "battery": st.get("battery_percent"),
            "charging": st.get("charging"),
            "foreground": fg.get("process", ""),
            "title": fg.get("title", ""),
            "scheme": scheme,
            "scheme_name": self.cfg.get("custom_scheme") == scheme and self.cfg.get("custom_scheme_name") or "",
            "nvml_ready": self.nvml.ready,
            "nvml_mode": self.nvml.mode(),
            "knobs": knobs,
            "auto": bool(self.cfg.get("auto_mode", True)) and not self.manual,
            "changed": self.last_changed,
            "learn_on": bool(self.cfg.get("learn_enabled", True)),
            "learn_rules": len(self.learner.data.get("rules", {})),
            "learn_hours": round(int(self.learner.data.get("uptime_seconds", 0)) / 3600.0, 2),
            "learn_switches": int(self.learner.data.get("switches", 0)),
            "autostart": bool(config.autostart_enabled()),
            "load": self.last_summary or {},
            "load_state": (self.last_summary or {}).get("state", ""),
            "busy": (self.last_summary or {}).get("busy", 0),
            "heavy": bool((self.last_summary or {}).get("heavy")),
            "cooldown_left": max(0, round(self.governor.cooldown
                                          - (time.time() - self.governor.last_switch)))
            if self.governor.last_switch else 0,
            "power": self.last_power or {},
            "alloc": self.alloc_report(),
            "battery_info": self.battery_report(),
            "thermal": self.thermal.summary(),
            "thermal_line": self.thermal.line(),
            "thermal_hw_line": self.thermal.hw_line(),
            "thermal_note": self._thermal_note,
            "fan": hw.fan_status(),
            "hw": self.hw_report(),
            "clamshell": self.clamshell_report(),
            "pd": self.pd_report(),
            "pd_line": self.pd.line(),
            "gpu_eco": self.gpu_eco.report(),
            "gpupick": self.gpupick_report(),
            "ppm_note": self._ppm_note,
            "ppm_fixes": self._ppm_fixes,
            # 面板字体比例（config.panel_font_scale，网页端用 CSS zoom 应用）
            "panel_font_scale": float(self.cfg.get("panel_font_scale", 1.0) or 1.0),
        }

    def _wire_temp_backup(self) -> None:
        """给 CPU 温度挂第二个数据源（华硕 ATKACPI）。

        主源是 PDH 的 Thermal Zone，但它一旦失效，上层会拿到 None —— 所有跟温度
        有关的决策（散热档、游戏档分配、PPM 自愈）会一起瞎掉，而且**不会报错**。

        本机实测两条通道差值恒定 0.1℃（tools/probe_temp_sources.py，2026-10-07），
        所以 Failover 是安全的。

        两个约束：
        - 复用 lp.atkacpi 的**全局单例**，绝不新建句柄。ATKACPI 是独占内核句柄，
          多一个就多一条死锁路径（2026-10-07 的退出卡死就是这么来的）。
        - 惰性：只有主源读不到时才真的去调它，正常情况零额外开销。
        """
        try:
            from . import atkacpi as _atk
        except Exception:
            return

        def _backup():
            try:
                a = _atk.get()
            except Exception:
                return None
            return a.cpu_temp() if a else None

        try:
            # 每 3 分钟比对一次两条通道，用来发现"某个源在悄悄漂移"
            self.pmon.set_temp_backup(_backup, cross_check_every=180.0)
        except Exception:
            pass

    # ------------------------------------------------------------ 循环
    def start(self) -> None:
        self._stop = False
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        threading.Thread(target=self._top_proc_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop = True

    def pump_tick(self) -> None:
        """主线程空闲时调用：刷新托盘提示（离电时降频到 6s 一次）"""
        period = 6 if not (self.last_battery or {}).get("ac", True) else 2
        if int(time.time()) % period != 0:
            return
        try:
            st = self.status()
            tip = "%s · %s" % (st["mode_label"], st["reason"])
            al = st.get("alloc") or {}
            pw = st.get("power") or {}
            bi = st.get("battery_info") or {}
            if (not st.get("ac")) and bi.get("discharging"):
                # 离电：最有用的信息是「还能撑多久」
                bits = []
                w = bi.get("rate_avg_w") or bi.get("rate_w")
                if w:
                    bits.append("放电 %.1fW" % w)
                if bi.get("percent") is not None:
                    bits.append("%d%%" % bi["percent"])
                if bi.get("eta_minutes"):
                    bits.append("还剩 %s" % fmt_minutes(bi["eta_minutes"]))
                tip = "%s · %s" % (st["mode_label"], " · ".join(bits) or st["reason"])
            elif al.get("verdict_cn") and al.get("verdict") != "unknown":
                bits = []
                if pw.get("cpu_w") is not None:
                    bits.append("CPU %.0fW" % pw["cpu_w"])
                if pw.get("gpu_w") is not None:
                    bits.append("GPU %.0fW" % pw["gpu_w"])
                if pw.get("gpu_temp") is not None:
                    bits.append("GPU %.0f℃" % pw["gpu_temp"])
                elif pw.get("cpu_temp") is not None:
                    bits.append("CPU %.0f℃" % pw["cpu_temp"])
                tip = "%s · %s｜%s%s" % (
                    st["mode_label"], " / ".join(bits) or st["reason"],
                    al["verdict_cn"],
                    ("（预计 +%.0f%%）" % al["est_gain_pct"]) if al.get("est_gain_pct") else "")
            # 弱电源：电池正在补电是最该被看见的信息
            pdinfo = st.get("pd") or {}
            if pdinfo.get("active"):
                tip = "%s｜%s" % (tip, self.pd.line())
            if getattr(self, "_tray", None):
                self._tray.set_tooltip(tip[:127])
        except Exception:
            pass

    # ------------------------------------------------------------ 功耗分配
    def set_proc_max(self, pct: int) -> bool:
        """只改「最大处理器状态」这一项（AC/DC 都写）。功耗分配器的唯一执行动作。"""
        scheme = self.cfg.get("custom_scheme") or ""
        if not scheme or not pc.list_schemes().get(scheme):
            return False
        try:
            plan = pc.load_plan(scheme)
        except Exception:
            return False
        s = plan.get("PROCTHROTTLEMAX") or plan.get("bc5038f7-23e0-4960-96da-33abaf5935ec")
        if s is None:
            return False
        v = int(pct)
        if s.vmax:
            v = min(v, int(s.vmax))
        if s.vmin:
            v = max(v, int(s.vmin))
        done = False
        for on_bat in (False, True):
            if s.current(on_bat) == v:
                done = True
                continue
            if pc.set_value(scheme, s, v, on_bat):
                done = True
        # 写失败绝不能记成已生效：限频看门狗靠 _alloc_applied 判断「期望值」，
        # 记成生效会让自愈永久失效，面板显示的百分比也是假的。
        self._alloc_applied = v if done else None
        self._plan_cache.pop(scheme, None)
        return done

    def _effective_proc_max(self) -> int:
        """该游戏档「基准」CPU 上限（含用户手动/自学习覆盖），分配器以此为天花板"""
        prof = PROFILES.get(self.current or "", {})
        st = hw.power_status()
        table = prof.get("dc") if not st.get("ac") else prof.get("ac", {})
        base = int((table or {}).get("proc_max", 100))
        if self.cfg.get("learn_enabled", True):
            try:
                base = int(self.learner.knob_value(self.current or "gaming", "proc_max", base))
            except Exception:
                pass
        base = max(10, min(100, base))
        # 弱电源保护的天花板（100 = 不限制）
        try:
            cap = int(getattr(self.pd, "cpu_cap", 100))
            if cap < 100:
                base = min(base, cap)
        except Exception:
            pass
        return base

    def _igpu_available(self) -> bool:
        """本机有没有核显可退 —— 弱电源「强制核显」的前置条件。

        无核显机型（部分 HX 独显本、BIOS 里关掉 iGPU 的机器）上，把独显
        ACPI 断电 = 直接黑屏。所以只有在**明确测到**核显不存在时才拒绝；
        探测失败/没探测过一律返回 True，不改变既有行为（宁可不特殊照顾，
        也不能因为探测失败就废掉弱电源策略）。
        """
        try:
            p = getattr(self.hwprof, "profile", None) or {}
        except Exception:
            p = {}
        if not p:
            return True
        names = p.get("gpu_names") or []
        if not names:                 # 连显卡都没枚举出来：不敢下结论
            return True
        return bool(p.get("igpu_name"))

    def _err(self, scope: str, exc: BaseException) -> None:
        """静默地记一笔异常到 error.log（去重/限长，绝不抛、绝不弹窗）。"""
        try:
            from . import errlog
            errlog.log(scope, exc)
        except Exception:
            pass

    def _alloc_tick(self, st: dict, procs: dict, raw: dict, summary: dict) -> None:
        """游戏档下：读温度/功耗 -> 判定瓶颈 -> 必要时让渡 CPU 预算给独显

        注意：**只在插电时生效**。离电时本机跑的是 AMD 核显（独显休眠），
        既没有 Dynamic Boost 可让，也不该去唤醒独显驱动，所以直接跳过。
        """
        on_ac = bool(st.get("ac"))
        try:
            snap = self.pmon.sample(want_gpu=on_ac)
        except Exception:
            return
        self.last_power = snap
        self._power_at = time.time()
        # 硬件自适应：游戏档插电时把独显实测功耗喂给峰值学习器。
        # 这样「让渡停止点」不用猜 —— 第一局游戏内就能从通用兜底 60W 抬到真值。
        try:
            gw = snap.get("gpu_w")
            if gw and on_ac and self.current == "gaming" and self.hwprof is not None:
                self.hwprof.learn_gpu_peak(gw)
                self.alloc.set_hw_tgp(self.hwprof.gpu_tgp_max())
        except Exception:
            pass
        # 离电时把电池放电功率喂给分配器当整机真值（面板「整机」与电池卡一致）；
        # 插电时没有真值通道，仍用 soc+独显+固定开销
        if not on_ac:
            try:
                b = self.last_battery or {}
                r = b.get("rate_avg_w") or b.get("rate_w")
                self.alloc.dc_true_w = float(r) if r and r > 0 else None
            except Exception:
                self.alloc.dc_true_w = None
        else:
            self.alloc.dc_true_w = None
        gaming = self.current == "gaming"
        base = self._effective_proc_max() if gaming else None
        # 游戏调度环境（HAGS/游戏模式/GameDVR）：只读检测，5 分钟缓存一次
        if gaming and (time.time() - self._env_at) > 300:
            self._env_at = time.time()
            try:
                self._env = hw.gaming_env()
                self.alloc.set_env(self._env)
            except Exception:
                pass
        try:
            req = self.alloc.feed(snap, base, active=(gaming and on_ac), on_ac=on_ac)
        except Exception:
            return
        if req and req.get("proc_max") is not None:
            if self.set_proc_max(req["proc_max"]):
                self.last_reason = "%s · %s" % (
                    PROFILES.get(self.current or "gaming", {}).get("label", ""),
                    req.get("reason") or "功耗分配调整")
                self._emit()
        # 把独显功耗/温度并进负载摘要，面板与自学习都能用
        if summary is not None:
            summary["power"] = {
                "cpu_w": snap.get("cpu_w"), "gpu_w": snap.get("gpu_w"),
                "cpu_temp": snap.get("cpu_temp"), "gpu_temp": snap.get("gpu_temp"),
                "gpu_util": snap.get("gpu_util"), "gpu_clock": snap.get("gpu_clock"),
            }

    def reload_alloc_cfg(self) -> None:
        """面板改完功耗分配参数后热重载（无需重启）"""
        try:
            self.alloc.reload_cfg(self.cfg)
        except Exception:
            pass
        try:
            self.care.reload_cfg(self.cfg)
            self.care.scheme_guid = self.cfg.get("custom_scheme") or None
            if not getattr(self.care, "enabled", True):
                self.care.relief_off(reason="电池养护已关闭，还原 CPU 上限")
        except Exception:
            pass
        try:
            self.thermal.reload_cfg(self.cfg)
            self.thermal.scheme = self.cfg.get("custom_scheme") or None
        except Exception:
            pass
        try:
            self.clamshell.reload_cfg(self.cfg)
            if not self.clamshell.enabled and self.clamshell.engaged:
                self.clamshell.reset(reason="合盖不休眠已关闭，还原合盖动作")
            self.gpupick.reload_cfg(self.cfg)
        except Exception:
            pass
        # 面板改完参数后，把「自动位」（用户没填的键）按硬件档案补一遍
        try:
            if self.hwprof is not None:
                self.hwprof.apply(self.cfg)
                self.alloc.set_hw_tgp(self.hwprof.gpu_tgp_max())
        except Exception:
            pass

    def hw_report(self) -> dict:
        """硬件自适应档案摘要：让人一眼看出「这些判据是按我的机器测出来的」"""
        try:
            if self.hwprof is not None:
                return self.hwprof.summary()
        except Exception:
            pass
        return {}

    def alloc_report(self) -> dict:
        r = self.alloc.report or {}
        out = dict(r)
        out.setdefault("verdict_cn", VERDICT_CN.get(out.get("verdict", "unknown"), "判定中"))
        out.setdefault("action_cn", ACTION_CN.get(out.get("action", "none"), "—"))
        out["enabled"] = bool(self.alloc.enabled)
        out["applied_pct"] = self._alloc_applied
        out["events"] = list(self.alloc.events[-8:])
        out["ok"] = self.pmon.available
        out["err"] = self.pmon._err
        return out

    # ------------------------------------------------------------ 离电续航
    def _refresh_target_hz(self, st: Optional[dict] = None):
        """离电时该用多低的刷新率；返回 None 表示「不动 / 还原」"""
        if not self.cfg.get("refresh_on_battery", True):
            return None
        st = st or hw.power_status()
        if st.get("ac"):
            # 弱电源保护正在降刷 —— 这里必须维持，否则每轮都会被当成「该还原」刷回去
            if getattr(self, "_pd_hz", 0):
                return self._pd_hz
            return None                      # 插电 -> 还原到原刷新率
        if self.current == "gaming":
            return None                      # 离电也打游戏：帧率优先，别压刷新率
        hz = int(self.cfg.get("dc_refresh_hz", 60) or 0)
        return hz or None

    def _refresh_tick(self, st: Optional[dict] = None) -> None:
        """把屏幕刷新率跟着电源状态来回切（失败退避，不反复折腾显示子系统）"""
        tgt = self._refresh_target_hz(st)
        now = time.time()
        try:
            if tgt is None:
                # 还原同样要退避：DisplayCtl 内部自己吞异常、只返回 False，
                # 外层 except 永远进不去 —— 不退避就会每 5 秒去切一次显示模式，
                # 打游戏时表现为周期性黑屏闪一下。
                if self.disp.changed and now >= self._disp_fail_at:
                    if self.disp.restore():
                        self._disp_fail_at = 0.0
                    else:
                        self._disp_fail_at = now + 60.0
            elif now >= self._disp_fail_at and self.disp.current_hz() != tgt:
                if self.disp.apply(tgt):
                    self._disp_fail_at = 0.0
                else:
                    self._disp_fail_at = now + 60.0     # 面板不支持就一分钟内别再来
        except Exception:
            self._disp_fail_at = now + 60.0
        self.last_display = self.disp.sample()

    def battery_tick(self, st: Optional[dict] = None) -> dict:
        """读一次电池 + 校准刷新率。每轮检测调一次，开销 ≈ 0.3 ms。"""
        st = st or hw.power_status()
        self._bat_n += 1
        # 插电时放电功率没意义（但健康度/充电状态要留），降到每 6 轮读一次
        if st.get("ac") and (self._bat_n % 6) and self.last_battery:
            info = self.last_battery
        else:
            try:
                info = self.bat.sample()
            except Exception:
                info = self.last_battery or {}
            self.last_battery = info
        self._refresh_tick(st)
        # 自身省电：离电时把本进程切 EcoQoS + 低优先级，插电还原
        eco = bool(self.cfg.get("self_eco_on_battery", True)) and not st.get("ac")
        if eco != getattr(self, "_eco_on", None):
            self._eco_on = eco
            hw.self_power_saver(eco)
        self._brightness_tick(st)
        self._care_tick(st, info)
        self._pd_tick(st, info)
        self._thermal_tick()
        self._clamshell_tick(st)
        self._gpu_eco_tick(st)
        self._gpupick_tick(st)
        self._trim_tick()
        self._ppm_watchdog(st)
        return info

    # ------------------------------------- 弱电源（100W PD）零补电保护
    def _pd_tick(self, st: dict, info: dict) -> None:
        """插着电却仍在放电 ⇒ 电源供不上（100W PD 打游戏），逐级压制到电池不再出力。

        判据直接用「电池充放电功率」，不去猜适配器多少瓦 —— 这是唯一可靠的信号，
        也不需要标定 ChargerMode 之类的厂商私有 ID。
        """
        try:
            ac = bool(st.get("ac"))
            # ---- 供电会话跟踪：换充电器必须作废上一个充电器学到的上限
            try:
                if self.pdsrc is not None:
                    chg = self.pdsrc.note_ac(ac)
                    if chg == "new":
                        adopted = self.pdsrc.adopt()
                        self.pd.reset_learning()
                        if adopted is not None:
                            # firm 跟着沿用：上次若被放电精确标定过，这个值就能
                            # 直接用于提前限帧；只是充电下界则不行（见 pdbudget
                            # ._over_expected 里的说明）。
                            self.pd.set_supply(adopted.get("watts"),
                                               firm=bool(adopted.get("firm")))
                        self._pd_supply_saved = None   # 新会话重新计写入节流
            except Exception:
                pass
            if not (self.pd.enabled and ac):
                if self.pd.active or self._pd_hz or self._pd_dim:
                    self._pd_apply({"reset": True})
                    try:
                        self.pd.feed(False, None, 0.0)
                    except Exception:
                        pass
                self._pd_at = time.time()
                return
            # 插电时 battery_tick 每 6 轮才采一次电池（省电策略），这里强制刷新：
            # 一次 ACPI 查询 ≈ 0.2ms，可忽略，但拿到的 rate 才是当前值
            try:
                b = self.bat.sample(force=True)
            except Exception:
                b = info or {}
            rate = b.get("rate_avg_w")
            if rate is None:
                rate = b.get("rate_w")
            now = time.time()
            dt = (now - self._pd_at) if self._pd_at else 5.0
            self._pd_at = now
            req = self.pd.feed(True, rate, dt, self._machine_w())
            if req:
                self._pd_apply(req)
            self._pd_save_supply(now)
            # 供电档位变了（换了充电器 / 刚测出能力）→ 更新分配器的独显预算，
            # 让「让渡停止点」跟着这根线的能力走，而不是只认硬件上限。
            try:
                self.alloc.set_supply_gpu_budget(
                    self.pd_tier_policy().get("gpu_budget_w"))
            except Exception as _e:
                self._err("pd.alloc_budget", _e)
        except Exception as _e:
            self._err("pd", _e)

    def _power_snap(self, max_age: float = 20.0,
                    want_gpu: Optional[bool] = None) -> dict:
        """拿一份不太旧的功耗快照。

        平时 `_alloc_tick` 每轮都会刷新 `last_power`；但**手动档**（auto_mode
        关闭）下 `_alloc_tick` 根本不跑，`last_power` 永远是空的 —— 那样
        `_machine_w()` 恒为 None（预测式 PD 保护退化成"等电池放电 12 秒"），
        限频看门狗和电池养护拿不到温度/占用也全部失效。所以按新鲜度兜底自采。

        want_gpu 省略时按当前是否插电决定：离电绝不唤醒 NVIDIA 驱动。
        """
        now = time.time()
        p = self.last_power or {}
        if p.get("soc_w") is not None and (now - (self._power_at or 0.0)) <= max_age:
            return p
        if want_gpu is None:
            try:
                want_gpu = bool((self.last_battery or hw.power_status()).get("ac"))
            except Exception:
                want_gpu = False
        try:
            snap = self.pmon.sample(want_gpu=want_gpu)
            if snap:
                self.last_power = snap
                self._power_at = now
                return snap
        except Exception:
            pass
        return p

    def _machine_w(self) -> Optional[float]:
        """整机功耗估算 = SoC + 独显 + 平台开销（PD 供电预算用）。

        system_w 这个计数器实测只是 APU 侧估算，**不含 NVIDIA 独显**
        （2026-10-07 实测：游戏中 system_w 20W，而 SoC 28W + 独显 41.5W），
        所以这里必须自己把独显加回来，否则会严重低估整机功耗。
        """
        try:
            p = self._power_snap()
            soc = p.get("soc_w")
            gpu = p.get("gpu_w") or 0.0
            if soc is None:
                return None
            oh = float(self.cfg.get("pd_overhead_w", 12.0) or 12.0)
            return float(soc) + float(gpu) + oh
        except Exception:
            return None

    def _pd_save_supply(self, now: Optional[float] = None) -> None:
        """把本次会话学到的供电能力存档（限频：变化 >3W 且最少间隔 5 分钟）。

        不再写 config.pd_supply_w：那个键是**全局**的，换充电器时不会作废，
        冷启动会被 reload_cfg 当成真值读回来 —— 正是「200W 上学到 130W、
        带到 100W Type-C 上继续当 130W 用」的成因。现在改存 pd_supply.json，
        由 PdSource 按供电会话管理。
        """
        try:
            v = getattr(self.pd, "supply_w", None)
            if not v:
                return
            now = now or time.time()
            last = getattr(self, "_pd_supply_saved_at", 0.0)
            saved = getattr(self, "_pd_supply_saved", None)
            if saved is not None and (abs(v - saved) < 3.0 or now - last < 300):
                return
            self._pd_supply_saved = v
            self._pd_supply_saved_at = now
            if self.pdsrc is not None:
                self.pdsrc.save(v, firm=bool(getattr(self.pd, "supply_firm", False)))
        except Exception:
            pass

    def _pd_apply(self, req: dict) -> None:
        """执行 PD 保护动作（刷新率 / 亮度 / CPU 上限）。全部可逆，失败静默。"""
        try:
            if req.get("reset"):
                if self._pd_hz:
                    self._pd_hz = 0
                    try:
                        self.disp.restore()
                    except Exception:
                        pass
                if self._pd_dim:
                    self._pd_dim = False
                    self._pd_brightness_restore()
                self._pd_write_proc_max()
                return
            if req.get("refresh"):
                try:
                    want = self.disp.lowest_hz() or int(req["refresh"])
                    if self.disp.apply(want):
                        self._pd_hz = want
                except Exception:
                    pass
            if req.get("refresh_off"):
                self._pd_hz = 0
                try:
                    self.disp.restore()
                except Exception:
                    pass
            if req.get("brightness"):
                # 亮度写的是**当前活动计划**（BrightnessCtl 内部按 active_scheme 写）。
                # 自建计划没生效时写就会污染系统「平衡」计划，必须先过这道闸。
                if self._scheme_active_ok():
                    try:
                        if self.bright.apply(ac=int(req["brightness"])):
                            self._pd_dim = True
                    except Exception:
                        pass
            if req.get("brightness_off"):
                self._pd_dim = False
                if self._scheme_active_ok():
                    self._pd_brightness_restore()
            if req.get("cpu_cap") is not None:
                self._pd_write_proc_max()
        except Exception:
            pass

    def _pd_brightness_restore(self) -> None:
        """把亮度还原成当前档位的插电亮度"""
        try:
            prof = PROFILES.get(self.current or "balanced", {})
            want = int((prof.get("ac") or {}).get("bright") or 0)
            if want:
                self.bright.apply(ac=want)
        except Exception:
            pass

    def _pd_write_proc_max(self) -> None:
        """把 CPU 上限写成 min(档位基准, PD 天花板)"""
        try:
            self.set_proc_max(self._effective_proc_max())
        except Exception:
            pass

    def pd_report(self) -> dict:
        r = self.pd.report()
        r["hz_applied"] = self._pd_hz
        r["dim_applied"] = self._pd_dim
        try:
            if self.pdsrc is not None:
                s = self.pdsrc.summary()
                r["session_s"] = s.get("session_s")
                r["history"] = s.get("history") or []
        except Exception:
            pass
        return r

    # ---------------------------------------------- CPU 限频卡死看门狗
    def _expected_proc_max(self, st: dict) -> int:
        """当前档位「本应生效」的 CPU 上限（考虑散热封顶/养护降温/功耗分配）。
        只有本应放开到 ≥90% 时，看门狗才有资格怀疑卡死 —— 续航/静音档本来就是
        故意压频率的，绝不能当故障去"修"。"""
        try:
            cur = self.current or "balanced"
            if cur == "balanced":
                return 100                 # 系统「平衡」计划默认不设上限
            prof = PROFILES.get(cur, {})
            side = prof.get("dc" if not st.get("ac") else "ac", {})
            v = int(side.get("proc_max", 100) or 100)
            v2 = self.thermal.apply_to_knobs(cur, bool(st.get("ac")), "proc_max", v)
            if v2:
                v = min(v, int(v2))
            # 只有**游戏档**才认功耗分配器压下来的上限。离开游戏档后
            # _alloc_applied 若不清零，会把办公档的"本应生效值"也压到 60，
            # 于是看门狗永远算不出 ≥90%，限频卡死自愈从此失灵。
            if self._alloc_applied is not None and cur == "gaming":
                v = min(v, int(self._alloc_applied))
            # 弱电源压制也是「故意降频」，必须豁免，否则看门狗会把它当卡死去"修"
            cap = int(getattr(self.pd, "cpu_cap", 100))
            if cap < 100:
                v = min(v, cap)
            if getattr(self.care, "_relief_on", False):
                cap = getattr(self.care, "_relief_base_ac", None)
                _ = cap                    # 养护降温期间不武装（可能是它压的）
                return 0
            return v
        except Exception:
            return 0

    def _ppm_watchdog(self, st: dict) -> None:
        """检测并自愈「内核已生效处理器上限被卡死」。

        判据：整机占用 ≥25% 但 CPPC 性能请求仍贴着最低档（≤40%，
        本机最低性能 = 标称的 30% = 600MHz），连续 6 个采样（约 30 秒）。
        正常情况占用高时性能请求必然拉满；只有卡死时才会双低。
        误判代价极低（重写的是自建计划自己的值 + 重新激活，无副作用）。
        """
        pw = self._power_snap(want_gpu=bool(st.get("ac")))
        util = pw.get("cpu_util")
        perf = pw.get("cpu_perf_pct")
        if util is None or perf is None:
            return
        # 本应放开的档位才武装（续航/静音/养护降温是故意压频率）
        if self._expected_proc_max(st) < 90:
            self._stall_n = 0
            return
        stuck = (util >= 25.0) and (0 < perf <= 40.0)
        if not stuck:
            if self._ppm_stage:
                self._ppm_stage = 0
                self._stall_n = 0
                self._ppm_note = ""
            return
        self._stall_n += 1
        if self._stall_n < 6:
            return
        now = time.time()
        # 档位越高冷却越长（1/2 档 60s，反复失败后 10 分钟再试）
        cooldown = 60.0 if self._ppm_stage < 2 else 600.0
        if (now - self._ppm_last_at) < cooldown:
            return
        self._ppm_last_at = now
        self._ppm_heal()

    def _ppm_heal(self) -> None:
        """分档自愈：
        第 1 档：把当前活动计划原样重新激活一次（零写入，多数卡死这一步就好）；
        第 2 档：借自建计划「过一道」——重写自建计划的处理器上限并激活、
                 再切回原计划。实测这才能把卡死的内核值刷掉。
        全程不写系统「平衡」计划任何一个字节。
        """
        self._ppm_fixes += 1
        act = pc.active_scheme() or ""
        scheme = self.cfg.get("custom_scheme") or ""
        try:
            if self._ppm_stage < 1:
                if act:
                    pc.set_active(act)          # 原样重新激活 = 重新下发参数
                    self._ppm_stage = 1
                    self._ppm_note = ("检测到 CPU 被限频卡死（占用高但频率贴地），"
                                      "已重新下发电源参数，观察 1 分钟")
                    return
            # 第 2 档：借自建计划刷新
            if scheme:
                for on_bat in (False, True):
                    pc.write_fast(scheme, "PROCTHROTTLEMIN", 5, on_bat)
                    pc.write_fast(scheme, "PROCTHROTTLEMAX", 100, on_bat)
                pc.set_active(scheme)
                time.sleep(0.4)
                if act and act != scheme:
                    pc.set_active(act)          # 切回原计划（通常为系统平衡）
                self._ppm_stage = 2
                self._ppm_note = ("CPU 限频卡死已用「重写+激活」强制刷新"
                                  "（第 %d 次）；若仍卡顿建议重启一次" % self._ppm_fixes)
                self.forget_scheme_ok()
        except Exception:
            pass

    # -------------------------------------------------------- 合盖不休眠
    def _clamshell_tick(self, st: dict) -> None:
        """蛤壳模式（对标 GHelper ClamshellModeControl，默认关闭）。

        低频（30 s）—— 合盖动作不可能秒级变化，没必要每轮读；
        但**关闭开关时必须立刻还原**，所以先判开关再节流。
        """
        cl = self.clamshell
        try:
            if not cl.enabled:
                if cl.engaged:
                    cl.reset(reason="合盖不休眠已关闭，还原合盖动作")
                return
            now = time.time()
            if (now - self._clam_at) < 30.0:
                return
            self._clam_at = now
            cl.tick(bool(st.get("ac")), self._scheme_active_ok())
        except Exception:
            pass

    # -------------------------------------------------------- 独显 Eco 断电
    def _gpu_eco_tick(self, st: dict) -> None:
        """离电时把独显 ACPI 断电（借鉴 GHelper GPU Eco），插电还原。

        写后回读校验在 gpueco 内部做；这里只管开关与档位例外：
        · 系统自带「平衡」档 → 一切自动化不干预
        · 离电 + 游戏档 → 不断电（离电也允许跑游戏）
        开关由 config gpu_eco_auto 控制（默认开），关闭时自动还原。
        """
        ge = self.gpu_eco
        ge.enabled = bool(self.cfg.get("gpu_eco_auto", True))
        # 弱电源（如 65W Type-C）：插电也把独显断掉，改用核显渲染。
        # 详见 gpueco.tick 的 force_off 说明。
        force_off = False
        try:
            # 无核显机型不断电：断掉独显就没有输出设备了（黑屏）
            force_off = bool(self.pd_tier_policy().get("force_igpu")) \
                and bool(st.get("ac")) and self._igpu_available()
        except Exception as _e:
            self._err("gpueco.force_off", _e)
        try:
            ge.tick(bool(st.get("ac")), getattr(self, "current", None),
                    force_off=force_off)
        except Exception:
            pass

    def pd_tier_policy(self) -> dict:
        """当前供电档位的策略（整机/GPU 预算、是否强制核显）。"""
        try:
            return self.pd.tier_policy() or {}
        except Exception:
            return {}

    def _gpupick_tick(self, st: dict) -> None:
        """核显优先调度（2026-10-10 新增）。

        只做两件事：推进手动试探采样；把「名单命中 / 用户钉住」的前台程序
        写成核显偏好。名单外的程序一个字节都不动 —— 拿用户正在玩的游戏
        赌帧率是越界行为。写注册表是给**下次启动**准备的，运行中的程序不受影响。
        """
        gp = self.gpupick
        try:
            gp.reload_cfg(self.cfg)
        except Exception as _e:
            self._err("gpupick.cfg", _e)
        try:
            from . import hw as _hw
            fg = _hw.foreground()
        except Exception:
            return
        name = (fg or {}).get("process", "")
        # 试探进行中：先跟上 pid（程序重启后 pid 会变），再采一个样本
        if gp._probe and not gp._probe["done"]:
            if name == gp._probe["exe_name"] and fg.get("pid"):
                gp._probe["pid"] = fg["pid"]
            gp.probe_tick()
            return                      # 试探期间不再做常规应用，免得互相打架
        if not gp.enabled or not name:
            return
        if getattr(self, "current", None) == "balanced":
            return                      # 系统自带平衡档：一切自动化不干预
        mode, _src, _note, _tier = gp.classify(name)
        # 弱电源档（插电但供电不足）：不管内置名单怎么判，一律走核显。
        # 65W 下让独显跑 3D 必然持续从电池取电，比画质下降严重得多。
        try:
            if bool(self.pd_tier_policy().get("force_igpu")) \
                    and bool(st.get("ac")) and self._igpu_available():
                mode = "igpu"
        except Exception as _e:
            self._err("gpupick.tier", _e)
        if mode is None:
            return
        path = (fg or {}).get("path", "")
        if path:
            gp.apply_for(path, name)

    def _gp_target(self) -> dict:
        """按钮/试探要作用在哪个程序上。

        关键：用户点面板按钮时，前台窗口**就是面板自己**，而 hw.foreground()
        按设计会把自家人排除（返回空进程名）—— 于是四个按钮点了没反应，
        看起来像"按钮失效"。真实意图显然是"我刚刚在用的那个程序"，所以用
        `_last_fg`（最近一次非本程序的前台窗口）兜底。learner 的档位投票早就
        这么干了，gpupick 这层漏了。
        """
        from .gpupick import pick_target
        try:
            from . import hw as _hw
            fg = _hw.foreground() or {}
        except Exception:
            fg = {}
        try:
            return pick_target(fg, self._last_fg)
        except Exception:
            return {"process": "", "path": "", "pid": 0, "stale": False}

    def gpupick_report(self) -> dict:
        """面板用：整体状态 + 当前前台程序的判定。"""
        try:
            r = self.gpupick.report()
        except Exception:
            r = {"enabled": False}
        t = self._gp_target()
        name, path = t["process"], t["path"]
        try:
            mode, src, note, tier = self.gpupick.classify(name)
        except Exception:
            mode, src, note, tier = None, "", "", 0
        r["current"] = {
            "process": name,
            "path": path,
            "mode": mode,
            "src": src,
            "note": note,
            "tier": tier,
            "managed": bool(path) and path in self.gpupick.data.get("orig", {}),
            # 面板当前台 ⇒ 显示的是"上一个真实前台程序"，UI 要如实说清楚
            "stale": t["stale"],
        }
        return r

    def gpupick_set(self, exe_name, mode) -> dict:
        """手动钉住：igpu / dgpu / None（交回系统）。不给名字就钉当前前台程序。

        用户是在面板上点按钮的，此刻前台就是面板自己 —— 所以名字和路径都从
        `_gp_target()` 拿（它会退到"上一个真实前台程序"），否则按前台取会拿到
        空名字，按钮形同失效。
        """
        try:
            t = self._gp_target()
            name = exe_name or t["process"]
            if not name:
                self.gpupick._last_note = "没有前台程序可钉"
                return self.gpupick_report()
            self.gpupick.set_user(name, mode if mode in ("igpu", "dgpu") else None)
            path = t["path"]
            if path:
                if mode == "igpu":
                    self.gpupick.reg_set(path, 1)
                elif mode == "dgpu":
                    self.gpupick.reg_set(path, 2)
                else:
                    self.gpupick.reg_restore(path)
            else:
                self.gpupick._last_note = ("已记住 %s（拿不到完整路径，"
                                           "注册表偏好下次启动再写）" % name)
        except Exception:
            pass
        return self.gpupick_report()

    def gpupick_probe_start(self, seconds: float = 45.0) -> dict:
        """对当前前台程序发起「试试核显」试探。"""
        try:
            t = self._gp_target()
            path, name, pid = t["path"], t["process"], t["pid"]
            if not path:
                self.gpupick._last_note = "拿不到前台程序路径"
            else:
                self.gpupick.probe_start(path, name, pid, seconds)
        except Exception as e:
            self.gpupick._last_note = "试探启动失败：%r" % e
        return self.gpupick_report()

    def gpupick_probe_cancel(self) -> dict:
        try:
            self.gpupick.probe_cancel()
        except Exception:
            pass
        return self.gpupick_report()

    def gpupick_restore_all(self) -> dict:
        """一键把所有被本模块改过的条目还原成原值。"""
        try:
            n = self.gpupick.restore_all()
            self.gpupick._last_note = "已还原 %d 个条目" % n
        except Exception:
            pass
        return self.gpupick_report()

    # -------------------------------------------------------- 常驻内存瘦身
    def _trim_tick(self) -> None:
        """定期把常驻进程的工作集压回去（对标 GHelper MemoryHelper.cs）。

        pythonw 长期驻留会攒下碎片/缓存页；SetProcessWorkingSetSize(-1,-1)
        让内核把可回收页换出，工作集立刻回落。15 分钟一次，代价可忽略。
        """
        if not bool(self.cfg.get("gc_trim", True)):
            return
        now = time.time()
        if (now - self._trim_at) < 900.0:
            return
        self._trim_at = now
        try:
            import gc as _gc
            _gc.collect()
        except Exception:
            pass
        try:
            import ctypes as _ct
            k32 = _ct.WinDLL("kernel32", use_last_error=True)
            h = k32.GetCurrentProcess()
            k32.SetProcessWorkingSetSize.argtypes = [_ct.c_void_p, _ct.c_size_t, _ct.c_size_t]
            k32.SetProcessWorkingSetSize.restype = _ct.c_int
            SIZE_T_MAX = (_ct.c_size_t(-1)).value          # (SIZE_T)-1
            k32.SetProcessWorkingSetSize(h, SIZE_T_MAX, SIZE_T_MAX)
        except Exception:
            pass

    def clamshell_report(self) -> dict:
        try:
            r = dict(self.clamshell.report())
            r["line"] = self.clamshell.line()
            return r
        except Exception:
            return {}

    def set_clamshell(self, enabled=None, on_battery=None) -> dict:
        """开关合盖不休眠；关闭时立刻还原（不能等到下一轮巡检）。"""
        if enabled is not None:
            self.cfg["clamshell_enabled"] = bool(enabled)
        if on_battery is not None:
            self.cfg["clamshell_on_battery"] = bool(on_battery)
        self.clamshell.reload_cfg(self.cfg)
        try:
            config.save(self.cfg)
        except Exception:
            pass
        if not self.clamshell.enabled:
            self.clamshell.reset(reason="合盖不休眠已关闭，还原合盖动作")
        else:
            self._clam_at = 0.0                 # 立即重判一次
            try:
                self.clamshell.tick(bool(hw.power_status().get("ac")),
                                    self._scheme_active_ok())
            except Exception:
                pass
        self._emit()
        return {"ok": True, "report": self.clamshell_report()}

    # -------------------------------------------------------- 散热策略
    def _thermal_tick(self) -> None:
        """把散热策略的硬件旋钮同步到自建计划（幂等：目标没变就只读不写）。

        只在自建计划处于活动状态时动手——处于系统「平衡」档时写进去
        要么不生效、要么会把系统计划顶掉，两条都不允许。
        注意：活动计划门控要**自己刷**，不能依赖亮度 tick 先跑
        （亮度通道不可用时 brightness_tick 会提前 return，门控就永远不更新）。
        """
        if getattr(self, "current", None) == "balanced":
            return
        if not self._scheme_active_ok():
            return
        try:
            note = self.thermal.hw_apply()
            if note:
                self._thermal_note = note
        except Exception:
            pass

    def set_thermal_mode(self, mode: str) -> dict:
        """切换散热策略（面板/网页/接口统一入口）。"""
        if not self.thermal.set_mode(mode):
            return {"ok": False, "error": "未知的散热策略"}
        try:
            config.save(self.cfg)
        except Exception:
            pass
        # 切到 auto 会主动还原，切到 quiet/perf 由下一轮 tick 写入
        if self.thermal.mode == "auto":
            try:
                self.thermal.hw_restore()
            except Exception:
                pass
        # 立刻重写一遍档位，让 cpu_cap 封顶生效；硬件旋钮也立即同步
        # （不等下一轮 5 秒巡检，否则面板点了没反应）
        if self.current and self.current != "balanced":
            try:
                self.apply(self.current, reason="散热策略：%s" % self.thermal.label)
            except Exception:
                pass
            try:
                self._thermal_tick()
            except Exception:
                pass
        self._emit()
        return {"ok": True, "mode": self.thermal.mode, "label": self.thermal.label}

    # -------------------------------------------------------- 充放电管理
    def _care_tick(self, st: dict, info: dict) -> None:
        """充放电行为统计 + 充电高热降温。开销：一次字典累加，落盘有节流。

        热负荷代理：本机电池温度不通过 ACPI 暴露（level=2 实测失败），
        用「CPU 热区温度 / 独显温度」的较大值代表机身热负荷。
        """
        if not getattr(self.care, "enabled", True):
            return
        now = time.time()
        dt = (now - self._care_at) if self._care_at else 0.0
        self._care_at = now
        pw = self._power_snap(want_gpu=bool((self.last_battery or {}).get("ac", True)))
        heats = [t for t in (pw.get("cpu_temp"), pw.get("gpu_temp")) if t]
        heat = max(heats) if heats else None
        try:
            self.care.set_context(gaming=(self.current == "gaming"))
            self.care.feed(info, heat, dt)
        except Exception:
            pass

    def _br_reset(self) -> None:
        """亮度控制重新基线（电源切换/切档后调用）"""
        self._br_applied = None
        self._br_user_cap = None
        self._br_dimmed = False
        self._br_base = None

    def forget_scheme_ok(self) -> None:
        """切档/外部改计划后作废「活动计划」缓存（20 s 缓存立即失效）"""
        self._sch_ok_at = 0.0
        self._sch_ok_val = False

    def _scheme_active_ok(self, force: bool = False) -> bool:
        """当前活动电源计划是不是自建计划？

        所有通过 SCHEME_CURRENT 写入的动作（亮度封顶/空闲调暗/充电降温还原）
        都必须先过这一关 —— 否则处于系统「平衡」档时会把值写进系统自带计划，
        违反「系统计划一个字节不改」的硬性约束。
        """
        guid = (self.cfg.get("custom_scheme") or "").lower()
        if not guid:
            return False
        now = time.time()
        if not force and (now - self._sch_ok_at) < 20.0:
            return self._sch_ok_val
        try:
            ok = (pc.active_scheme() or "").lower() == guid
        except Exception:
            ok = False
        self._sch_ok_at = now
        self._sch_ok_val = ok
        return ok

    # -------------------------------------------------------- 亮度优化
    def _br_cap_target(self) -> int:
        cap = int(self.cfg.get("dc_brightness_cap", 45) or 0)
        if self._br_user_cap is not None:
            cap = self._br_user_cap      # 用户手动调过 -> 本次离电尊重用户
        return max(0, min(100, cap))

    def _brightness_tick(self, st: dict) -> None:
        """离电亮度优化（文献：屏幕是离电第一耗电大户，占 20~50%）：
        1) 封顶：DC 亮度不超过 dc_brightness_cap（实测改计划值即时生效）；
        2) 空闲调暗：无输入 idle_dim_after_s 秒后降到 idle_dim_level（全屏/游戏不干预）；
        3) 尊重用户：手动改亮度后本次离电会话以用户值为准，不再强行封顶。
        powercfg 调用只发生在状态切换/低频轮询（约 1 分钟一次），空闲查询零开销。"""
        b = self.bright
        if not getattr(b, "available", False):
            return
        ac = bool(st.get("ac"))
        was = self._br_was_ac
        self._br_was_ac = ac
        if was is not None and was != ac:
            # 电源状态切换：重新基线（apply() 切档重写亮度，这里不冲突）
            self._br_reset()

        # 只在自建计划生效时动作（亮度走 SCHEME_CURRENT 写入；
        # 处于系统「平衡」档时写进去就改了系统计划）
        if not self._scheme_active_ok():
            self._br_reset()
            return

        if ac:
            if self._br_dimmed or self._br_applied is not None:
                # 回到插电：还原当前档位的插电亮度，状态清零
                try:
                    prof = PROFILES.get(self.current or "balanced", {})
                    want = int((prof.get("ac") or {}).get("bright") or 0)
                    if want:
                        b.apply(ac=want, dc=None)
                except Exception:
                    pass
                self._br_reset()
            return

        # ---------------- 离电 ----------------
        gaming = self.current == "gaming"
        dim_enabled = bool(self.cfg.get("idle_dim_enabled", True))
        dim_level = max(0, min(100, int(self.cfg.get("idle_dim_level", 20))))
        dim_after = int(self.cfg.get("idle_dim_after_s", 90))

        fg_full = False
        try:
            fg_full = bool(hw.foreground().get("fullscreen"))
        except Exception:
            pass
        idle = b.idle_seconds()

        # 空闲调暗 / 恢复：退出条件独立判断，避免「关了开关或进了全屏视频后
        # 永远停在调暗亮度」的卡死
        can_dim = dim_enabled and not gaming and not fg_full
        if self._br_dimmed and (not can_dim or idle < 5.0 or self._br_applied is None):
            back = self._br_base if self._br_base is not None else self._br_cap_target()
            if b.apply(dc=back):
                self._br_dimmed = False
                self._br_applied = back
                self._br_base = None
            return
        if (can_dim and not self._br_dimmed and self._br_applied is not None
                and idle >= dim_after):
            self._br_base = self._br_applied
            if b.apply(dc=dim_level):
                self._br_dimmed = True
                self._br_applied = dim_level
            return

        if self._br_dimmed:
            return

        # 封顶与用户覆盖检测：需要读计划值（powercfg /q），控制频率
        self._br_n += 1
        if self._br_applied is not None and (self._br_n % 12):
            return
        if time.time() < self._br_fail_at:
            return                          # 读/写失败后退避，别每轮都起子进程
        _, cur_dc = b.read()
        if cur_dc is None:
            self._br_fail_at = time.time() + 60.0
            return
        if self._br_applied is None:
            target = self._br_cap_target()
            if target > 0:
                if cur_dc > target:
                    if b.apply(dc=target):
                        self._br_applied = target
                    else:
                        self._br_fail_at = time.time() + 60.0
                else:
                    self._br_applied = cur_dc
            else:
                self._br_applied = cur_dc
        elif cur_dc != self._br_applied:
            # 用户在控制中心/快捷键改了亮度 -> 尊重
            self._br_user_cap = cur_dc
            self._br_applied = cur_dc

    def battery_report(self) -> dict:
        b = dict(self.last_battery or {})
        d = dict(self.last_display or {})
        b["display_hz"] = d.get("current_hz")
        b["display_orig_hz"] = d.get("original_hz")
        b["display_available"] = d.get("available_hz") or []
        b["display_changed"] = bool(d.get("changed"))
        b["display_err"] = d.get("err") or ""
        b["top_procs"] = list(self._top_procs)
        b["brightness_applied"] = self._br_applied
        b["brightness_dimmed"] = self._br_dimmed
        b["brightness_cap"] = self._br_cap_target()
        b["care"] = self.care.report()
        b["care_line"] = self.care.summary_line()
        # 华硕「电池健康充电」上限（只读镜像值，装 MyASUS 后才有）
        b["charge_line"] = self.care.charge_line()
        if not self.cfg.get("battery_eta", True):
            b["eta_minutes"] = None
        return b

    # -------------------------------------------------------- 耗电排行（离电）
    def _top_proc_loop(self) -> None:
        """离电时每 60s 采样一次 CPU 占用前列进程；插电时清空并长眠。"""
        while not getattr(self, "_stop", False):
            try:
                b = self.last_battery or {}
                if b.get("discharging"):
                    self._top_procs = hw.top_cpu_procs(window=3.0, top_n=3)
                    time.sleep(60.0)
                else:
                    if self._top_procs:
                        self._top_procs = []
                    time.sleep(20.0)      # 插电时轻打盹，拔电能较快看到排行
            except Exception:
                time.sleep(20.0)

    def _loop(self) -> None:
        last_exe = None
        while not getattr(self, "_stop", False):
            try:
                self.battery_tick()          # 手动档也要跟电源状态走（刷新率/电量）
                # 离电时**绝不读独显**（NVML 会把独显驱动唤醒、破坏 D3 省电）
                on_ac_now = bool((self.last_battery or {}).get("ac", True))
                # 原因文本保鲜：拔电/插电但档位没变时，「插电中/使用电池」要跟着改，
                # 否则会出现「用着电池、原因却写着插电中」的乌龙。
                # 游戏档除外 —— 那里的原因由功耗分配器实时写，别覆盖。
                if (self.current and self.current != "gaming"
                        and time.time() - self._reason_at > 20):
                    self._reason_at = time.time()
                    self.last_reason = build_reason(self._reason_text(),
                                                    self.last_summary or {})
                if self.cfg.get("auto_mode", True) and not self.manual:
                    # 1) 先采样负载（EWMA 平滑 + 持续性判定），拿到 summary 再判定场景
                    procs = hw.list_processes()
                    fg = hw.foreground()
                    if fg.get("process"):
                        self._last_fg = dict(fg)
                    raw = hw.load_sample(procs)
                    if self.nvml.ready and on_ac_now:
                        try:
                            p = self.nvml.utilization()
                            if p is not None:
                                raw["gpu"] = float(p)
                        except Exception:
                            pass
                    summary = self.tracker.sample(raw.get("cpu"), raw.get("disk"),
                                                  raw.get("net"), raw.get("gpu"))
                    self.last_summary = summary

                    # 2) 场景判定（把负载摘要喂进去，用于重负载保护；
                    #    前台窗口本轮只采这一次，判定/学习/原因共用同一份）
                    target = self.detect(summary, fg=fg)
                    st = hw.power_status()
                    exe = (fg.get("process") or "").lower()

                    # 前台程序换人时，给它建一份画像
                    if exe != last_exe:
                        if exe:
                            self.learner.observe(exe, target or "balanced",
                                                 bool(st.get("ac")))
                        last_exe = exe

                    # 3) 切换闸门：连续命中 + 冷却期 + 游戏抢占
                    priority = target == "gaming" and target != self.current
                    ok, why = self.governor.offer(
                        target, self._confirm_needed(target or "balanced"),
                        self.current, priority=priority)
                    self._cand_n = self.governor.hits
                    if ok:
                        self.apply(target, reason=build_reason(
                            self._reason_text(fg), summary, why), fg=fg)

                    # 4) 功耗分配层：游戏档下按温度/瓶颈动态分配 CPU↔GPU 预算
                    self._alloc_tick(st, procs, raw, summary)
                else:
                    last_exe = None
                    self._cand, self._cand_n = None, 0
                    self.governor.reset()
                    self.alloc.reset()
            except Exception as _e:
                self._err("loop", _e)
            poll = float(self.cfg.get("poll_seconds", 5))
            if not (self.last_battery or {}).get("ac", True):
                poll = max(8.0, poll * 1.6)   # 离电：检测节奏放慢，自身也省电
            time.sleep(poll)

    def _reason_text(self, fg: Optional[dict] = None) -> str:
        """原因行的场景部分。2026-10-08 用户反馈文字重复：
        电量与顶部 chip/电池卡重复、窗口标题与副标题重复 —— 全部精简掉，
        只留「电源状态 · 前台程序（短名）」。"""
        if fg is None:
            fg = hw.foreground()
        st = hw.power_status()
        parts = []
        parts.append("插电中" if st.get("ac") else "使用电池")
        if fg.get("process"):
            # 去掉 .exe 后缀：workbuddy.exe -> workbuddy（面板里更清爽）
            parts.append((fg["process"] or "").rsplit(".", 1)[0])
        return " · ".join(parts)


# 启动时的系统白名单（避免把全屏的开发工具/播放器误判成游戏）
MANUAL_SYSTEM_WHITELIST = [
    "explorer.exe", "dwm.exe", "shellexperiencehost.exe", "searchhost.exe",
    "startmenuexperiencehost.exe", "textinputhost.exe", "ctfmon.exe",
    "applicationframehost.exe", "lockapp.exe", "systemsettings.exe",
    "snmode.exe", "widgetservice.exe", "searchapp.exe", "workspaceaothost.exe",
    "screenclippinghost.exe", "microsoftedgeconfigservicehost.exe",
    "runtimebroker.exe", "wlanext.exe", "audiodg.exe", "conhost.exe",
    "python.exe", "pythonw.exe",
    "windowsinternal.composedshellexperiencehost.exe",
]
