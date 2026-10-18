# -*- coding: utf-8 -*-
r"""电池充放电管理（锂电老化三大应力：满充搁置 / 高温 / 深放）

================================================================ 文献依据
（2026-10 检索多源交叉：Battery University 数据、ECS 温度-老化关系、
 Apple/Dell/联想/华硕厂商充电限制策略、若干实测长测报告）

1) 40-80 法则：锂电在 20~80% 区间应力最小。
   · 100% 常驻（插电不拔）时阴极处于高电压不稳定态，25 ℃ 下老化速率
     约为同电芯维持在 60% 时的 **2 倍**；保持 80% 上限可把循环寿命
     从 ~500 次推到 800~1000 次。
   · 25~75% 区间循环，全生命周期可多传递 **4~5 倍** 总电量。
2) 温度：**每升高 10 ℃，老化速率约翻倍**（Electrochemical Society）。
   · 45~60 ℃ 区间退化速率是常温的 4~8 倍。
   · 「充电中 + 高温 + 高电量」是三者叠加的最坏组合 —— 边充边跑重负载
     的游戏本一年掉 20% 健康度就是这个原因。
   · BMS 一般在电池 >35 ℃ 时自动降低充电电流。
3) 深放：长期低于 20% 会在阳极析锂，永久损失容量。
4) 厂商充电限制：华硕 MyASUS / Armoury Crate 可设 60/80/100% 上限
   （联想 Conservation Mode ≈60%，戴尔/苹果同类）。
   **本机现状（2026-10-05 标定）**：用户已安装 MyASUS 4.0.77 +
   ASUS System Control Interface v3，充电上限会镜像到注册表
   `HKLM\SOFTWARE\ASUS\ASUS System Control Interface\AsusOptimization\
   ASUS Keyboard Hotkeys\ChargingRate`（实测当前 = 100），
   程序**只读**该值（0.03 ms，普通权限）用于展示与建议。
   写入侧三条通道实测全部不通：ATKACPI 裸 IOCTL err=1；
   root\WMI 的 AsusAtkWmi_WMNB 有 0 个实例（调用是空操作）；
   非管理员写 HKLM 被拒。⇒ 设定动作必须由用户在 MyASUS 里完成一次，
   程序负责「读到 → 展示 → 不再重复唠叨」。

================================================================ 为什么不能「永远 80%」
（2026-10-11 用户反馈：养护模式就只能充到 80% 吗，不能全充满，不合理）

80% 上限是有**适用场景**的，不是一条普适真理。它只在一种情形下划算：
「长期插电不拔、很少用到电池」——此时满电搁置是唯一的老化源，压到 60~80%
能显著延长寿命。反过来，如果你**经常带出门**，80% 上限等于白砍掉 20% 续航，
换来的寿命收益远小于每天少 1/5 电量的代价。

另外两点被一刀切 80% 忽略的事实：
1) **电量计需要满充校准**。BMS 的库仑计靠满充/满放端点修正 SOC 漂移，长期
   不充满会出现「显示 30% 突然关机」「一直停在 99%」。厂商普遍建议每 1~2 个
   月做一次完整充放循环。所以「有目的的满充」不是坏习惯，**无意义的满充搁置**
   才是。
2) **耗时的时机**。满电后立刻拔掉使用，与满电后插着放三天，两者老化差一个量级。

⇒ 本模块的立场从「永远建议 80%」改为**按场景**：
   · desk（长期插电）    → 建议 60/80%
   · mobile（常带出门）  → 100% 是合理的，只在长期插电那几天临时压低
   · 校准到期（默认 30 天没做过满充）→ 主动建议做一次满充校准
并且引入「本次充满」意图：意图激活期间的满充**不计入扣分**（有目的的满充
不是坏习惯），带出去用过之后意图自动结束并提示切回养护上限。

================================================================ 本机能拿到什么
· 电量 %、充/放电状态、充放电功率、健康度、循环数 —— ACPI 电池接口（已有）
· 电池温度 —— **本机不支持**（IOCTL_BATTERY_QUERY_INFORMATION level=2 失败，
  实测 2026-10-05）⇒ 用「机身热区温度（PDH Thermal Zone，CPU 热区）」
  与 SoC 功耗做热负荷代理，阈值按平台特性定得保守（默认 88 ℃）。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

if __package__:
    from . import powercfgctl as pc
    from . import hw as _hw
else:                                     # 直接运行本文件自测
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lp import powercfgctl as pc
    from lp import hw as _hw

GUID_SUB_PROC = "54533251-82be-4824-96c1-47b60b740d00"
GUID_THROTTLE_MAX = "bc5038f7-23e0-4960-96da-33abaf5935ec"     # PROCTHROTTLEMAX
_IDX_RE = re.compile(r"0x([0-9a-fA-F]+)")
# 只匹配「当前交流/直流电源设置索引」行（powercfg /q 里还有最小/最大可能值等 0x 数字）
_CUR_RE = re.compile(r"当前(交流|直流)电源设置索引:\s*(0x[0-9a-fA-F]+)")


class BatteryCare:
    """充放电行为统计 + 充电高温缓解。绝不抛异常，失败只留默认值。

    feed() 每轮检测调用一次（开销 ≈ 一次字典累加）；落盘按需（默认 120 s）。
    """

    SAVE_EVERY = 120.0        # 落盘节流（写 JSON 一次 ≈ 0.1 ms，但没必要每轮）
    KEEP_DAYS = 30
    CHG_EVERY = 60.0          # 华硕充电上限读取节流（单次 0.03 ms，足够便宜但无需每轮）

    def __init__(self, cfg: Optional[Dict] = None, data_path: Optional[str] = None,
                 scheme_guid: Optional[str] = None, chg_reader=None):
        self.cfg = cfg or {}
        self.data_path = data_path
        # 只允许改「自建计划」——当前处于系统「平衡」档时绝不能动手
        # （硬性约束：系统自带计划一个字节不改）。传 None 时不校验（仅测试用）。
        self.scheme_guid = scheme_guid or (cfg or {}).get("custom_scheme") or None
        self._scheme_ok_at = 0.0
        self._scheme_ok_val = False
        self._load_params()
        self._data = self._load()
        # 「本次充满」意图 + 上次满充校准日期（跨重启保留，否则每次重启都失忆）
        self._intent = self._data.get("intent") or None
        self._last_calib = self._data.get("last_calib") or None
        self._lim_writer = None          # 测试注入用；None = 走 hw.set_asus_charge_limit
        # 上限「已下发 / 待生效 / 未生效」：写入成功 ≠ EC 已生效（实测滞后 ~13min）
        self._pending = self._data.get("limit_pending") or None
        self._limit_state = self._data.get("limit_state") or None
        self._last_save = 0.0
        self._last_feed = 0.0
        self._relief_on = False
        self._relief_base_ac: Optional[int] = None
        self._relief_reason = ""
        self._relief_cap_used: Optional[int] = None   # 本次降温实际写入的上限
        self._relief_charge = False          # 本次降温由「充电高热」触发
        self._relief_thermal = False         # 本次降温由「插电高温」触发
        self._advice: List[str] = []
        self._score = None
        self._today_key = self._day_key()
        # 华硕「电池健康充电」上限（只读镜像值）。测试用 chg_reader 注入以保持隔离。
        self._chg_reader = chg_reader
        self._chg_at = 0.0
        self._chg = {"supported": False, "value": None, "mode": None,
                     "mode_cn": None, "label": "不可用", "myasus": False}
        # 先把充电上限读到手（0.03 ms），让启动后第一次 report 就是准的
        try:
            self._chg_tick(force=True)
        except Exception:
            pass
        # 重启后立刻能出评分/建议（不等下一轮 feed）
        try:
            self._recompute({})
        except Exception:
            pass

    # ------------------------------------------------------ 充电上限（只读）
    def _chg_tick(self, force: bool = False):
        now = time.time()
        if not force and self._chg and (now - self._chg_at) < self.CHG_EVERY:
            return self._chg
        self._chg_at = now
        try:
            if self._chg_reader is not None:
                self._chg = dict(self._chg_reader() or {})
            else:
                self._chg = _hw.asus_charge_limit()
        except Exception:
            pass
        return self._chg

    # ------------------------------------------------------------ 参数
    def _load_params(self):
        c = self.cfg
        self.enabled = bool(c.get("battery_care_enabled", True))
        self.full_soc = float(c.get("care_full_soc_pct", 95))
        self.deep_soc = float(c.get("care_deep_soc_pct", 20))
        self.advise_limit = int(c.get("care_advise_charge_limit_pct", 80))
        self.hot_charge_c = float(c.get("care_hot_charge_c", 88))
        self.relief_enabled = bool(c.get("care_heat_relief", True))
        self.relief_cap = int(c.get("care_heat_relief_cpu_pct", 70))
        self.relief_clear_c = float(c.get("care_heat_relief_clear_c", 82))
        # 插电高温保护（2026-10-05 新增）：不止充电中——已充满插电跑重负载
        # 时 CPU 热区也会到 92℃+（NVML slowdown 线 91℃），长期贴着温度墙跑
        # 既掉频又伤机器。触发后临时压 AC CPU 上限，回落再还原。
        self.thermal_relief = bool(c.get("care_thermal_relief", True))
        self.hot_cpu_c = float(c.get("care_hot_cpu_c", 95))
        self.thermal_clear_c = float(c.get("care_thermal_clear_c", 89))
        self.thermal_cap = int(c.get("care_thermal_cpu_pct", 80))
        # ---- 按场景的充电上限（2026-10-11）----
        self.target_limit = int(c.get("care_target_limit_pct", 80))    # 养护上限
        self.full_limit = int(c.get("care_full_limit_pct", 100))       # 充满
        self.calib_days = int(c.get("care_calibrate_days", 30))        # 校准周期
        self.intent_h = float(c.get("care_full_intent_h", 24.0))       # 意图有效期
        self.mobile_dc_h = float(c.get("care_mobile_dc_h_day", 1.0))   # 日均离电≥此值=常带出门
        self.scene_days = int(c.get("care_scene_days", 7))             # 场景回看天数

    def reload_cfg(self, cfg: Optional[Dict] = None):
        if cfg is not None:
            self.cfg = cfg
        self._load_params()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> Dict:
        d = {"days": {}, "totals": {}}
        if not self.data_path:
            return d
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict) and isinstance(obj.get("days"), dict):
                return obj
        except Exception:
            pass
        return d

    def _save(self, force: bool = False):
        if not self.data_path:
            return
        now = time.time()
        if not force and (now - self._last_save) < self.SAVE_EVERY:
            return
        self._last_save = now
        try:
            # 只留最近 KEEP_DAYS 天
            days = self._data.get("days", {})
            if len(days) > self.KEEP_DAYS:
                for k in sorted(days.keys())[:-self.KEEP_DAYS]:
                    days.pop(k, None)
            tmp = self.data_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.data_path)
        except Exception:
            pass

    @staticmethod
    def _day_key(ts: Optional[float] = None) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))

    def _bucket(self) -> Dict:
        key = self._day_key()
        if key != self._today_key:
            self._today_key = key
            self._save(force=True)
        days = self._data.setdefault("days", {})
        b = days.get(key)
        if not isinstance(b, dict):
            b = {"full_soc_s": 0.0, "hot_charge_s": 0.0, "deep_s": 0.0,
                 "charge_wh": 0.0, "discharge_wh": 0.0, "sample_s": 0.0,
                 "relief_s": 0.0, "max_heat_c": 0.0,
                 # 插电补电放电（Type-C 供不上时电池出力）：不计入 discharge_wh
                 # 会漏掉整段弱电源游戏放电，但它真实参与循环老化，必须单独有桶
                 "ac_drain_wh": 0.0,
                 # 场景判定要区分插电/离电时长；intent_full_s 是「有目的的满充」
                 "ac_s": 0.0, "dc_s": 0.0, "intent_full_s": 0.0}
            days[key] = b
        return b

    # ------------------------------------------------------------ 主入口
    def feed(self, batt: Optional[Dict], heat_c: Optional[float],
             dt: Optional[float] = None) -> Dict:
        """喂入一轮电池/热负荷采样。返回本次的养护状态摘要。

        batt   : BatteryMonitor.sample() 的结果（可为 None）
        heat_c : 机身热负荷代理温度（CPU 热区，℃；拿不到给 None）
        dt     : 距上一轮秒数；不传则按墙钟自动算（首次按 0 计）
        """
        now = time.time()
        if dt is None:
            dt = max(0.0, now - self._last_feed) if self._last_feed else 0.0
        self._last_feed = now
        dt = min(max(dt, 0.0), 300.0)          # 机器休眠/长挂起后不虚增统计

        if not self.enabled:
            self.relief_off(reason="电池养护已关闭，还原 CPU 上限")
            self._advice = []
            self._score = None
            return self.report()

        batt = batt or {}
        b = self._bucket()
        b["sample_s"] = b.get("sample_s", 0.0) + dt
        pct = batt.get("percent")
        ac = bool(batt.get("ac"))
        # 插电/离电时长：场景判定（desk / mobile）的唯一依据
        b["ac_s" if ac else "dc_s"] = b.get("ac_s" if ac else "dc_s", 0.0) + dt
        # 意图过期 / 已带出去用过 → 自动结束（不等用户回来手动清）
        self._intent_tick(ac, pct)
        charging = bool(batt.get("charging"))
        discharging = bool(batt.get("discharging"))
        rate = batt.get("rate_w")
        if heat_c:
            b["max_heat_c"] = max(b.get("max_heat_c", 0.0), float(heat_c))

        # 1) 满充搁置：插电 + 电量 ≥ full_soc（最伤电池的一条）
        #    「本次充满」意图激活期间**不扣分**——有目的的满充不是坏习惯，
        #    坏的是无意义的满充搁置。单独计到 intent_full_s 便于事后核对。
        if ac and pct is not None and pct >= self.full_soc:
            key = "intent_full_s" if self.intent_active() else "full_soc_s"
            b[key] = b.get(key, 0.0) + dt

        # 2) 充电高热：充电中 + 机身热负荷高（电池温度本机不可读，用热区代理）
        hot = False
        if charging and heat_c is not None:
            th = (self.relief_clear_c if (self._relief_on and self._relief_charge)
                  else self.hot_charge_c)
            hot = float(heat_c) >= th
            if hot:
                b["hot_charge_s"] = b.get("hot_charge_s", 0.0) + dt

        # 3) 深度放电
        if (not ac) and pct is not None and pct <= self.deep_soc:
            b["deep_s"] = b.get("deep_s", 0.0) + dt

        # 4) 能量吞吐（用于估算等效循环数）
        if rate:
            wh = abs(float(rate)) * dt / 3600.0
            if wh < 200.0:                      # 坏值保护
                if charging:
                    b["charge_wh"] = b.get("charge_wh", 0.0) + wh
                elif discharging:
                    b["discharge_wh"] = b.get("discharge_wh", 0.0) + wh
                elif ac and float(rate) > 0.15:
                    # 插电补电放电（2026-10-18）：Type-C 供不上时电池在替电源
                    # 出力，ACPI 两标志皆假、discharging 也是假 —— 旧口径整段
                    # 漏计（10-18 实际放了 ~78Wh，discharge_wh 却记 0.00）。
                    # 这种放电还是大电流+高温，对循环数的贡献必须计入。
                    b["ac_drain_wh"] = b.get("ac_drain_wh", 0.0) + wh

        # 5) 充电高热缓解动作（插电 / 非游戏 / 可还原）
        self._relief_tick(charging, heat_c, batt, dt)
        if self._relief_on:
            b["relief_s"] = b.get("relief_s", 0.0) + dt

        # 6) 顺带刷新华硕充电上限（60 s 节流，单次 0.03 ms）
        self._chg_tick()
        # 7) 复查刚下发的上限（EC 有 ~13min 延迟，且可能被厂商服务回滚）
        self._pending_tick(ac, pct)

        self._recompute(batt)
        self._save()
        return self.report()

    # ------------------------------------------------------------ 缓解动作
    def _read_ac_proc_max(self) -> Optional[int]:
        """读「当前交流电源设置索引」。

        **必须只认「当前交流电源设置索引」这一行**：powercfg /q 的输出里
        还夹着「最小/最大可能的设置」「可能的设置增量」三个 0x 数值，
        早前按 `vals[0]` 取值读到的是「最小可能的设置 = 0」，
        会让降温逻辑把 AC CPU 上限误写成 0%（机器卡死级故障）。
        """
        try:
            out = pc.run_powercfg(["/q", "SCHEME_CURRENT", GUID_SUB_PROC,
                                   GUID_THROTTLE_MAX])
            if not out:
                return None
            for kind, hexv in _CUR_RE.findall(out):
                if kind == "交流":
                    v = int(hexv, 16)
                    return v if 0 <= v <= 100 else None
        except Exception:
            pass
        return None

    def _write_ac_proc_max(self, val: int) -> bool:
        """写 AC CPU 上限。**只在自建计划处于活动状态时才允许写**
        （硬性约束：系统自带计划一个字节不改）。写入极少发生（仅状态切换），
        所以这里强制走一次实时校验，不吃 30 s 缓存。"""
        if not self._scheme_ok(force=True):
            return False
        try:
            v = max(0, min(100, int(val)))
            pc.run_powercfg(["/setacvalueindex", "SCHEME_CURRENT",
                             GUID_SUB_PROC, GUID_THROTTLE_MAX, str(v)])
            pc.run_powercfg(["/setactive", "SCHEME_CURRENT"])
            return True
        except Exception:
            return False

    def _scheme_ok(self, force: bool = False) -> bool:
        """当前活动电源计划是不是我们的自建计划？（30 s 缓存，避免频繁起子进程）"""
        if not self.scheme_guid:
            return True                       # 未指定则不校验（离线测试场景）
        now = time.time()
        if not force and (now - self._scheme_ok_at) < 30.0:
            return self._scheme_ok_val
        ok = False
        try:
            out = pc.run_powercfg(["/getactivescheme"]) or ""
            ok = self.scheme_guid.lower() in out.lower()
        except Exception:
            ok = False
        self._scheme_ok_at = now
        self._scheme_ok_val = ok
        return ok

    def forget_scheme(self) -> None:
        """切档后调用：立即作废活动计划缓存（否则切到系统计划后 30 s 内
        仍会被判成「自建计划」，还原动作可能写进系统计划）。"""
        self._scheme_ok_at = 0.0
        self._scheme_ok_val = False

    def _relief_tick(self, charging: bool, heat_c: Optional[float],
                     batt: Dict, dt: float) -> None:
        """高热 -> 临时压低 AC 侧 CPU 上限（减少发热），回落还原。

        两条独立的触发线（共享同一套写入/还原机构，同时触发取更严的上限）：
          1) 充电高热：charging 且 ≥ hot_charge_c（默认 88℃）→ 压到 70%
          2) 插电高温：插电（含已充满）且 ≥ hot_cpu_c（默认 95℃）→ 压到 80%
        滞回：触发后分别等到 ≤ relief_clear_c(82) / ≤ thermal_clear_c(89) 才解除。
        """
        if not self.relief_enabled or not self.thermal_relief \
                or not self.charging_allowed(batt):
            self.relief_off(reason="当前不适合做降温动作")
            return
        if heat_c is None:
            self.relief_off()
            return
        if not self._scheme_ok():
            # 当前是系统自带计划（如「系统平衡」档）：绝不写入
            self.relief_off(reason="当前不是自建电源计划，跳过降温")
            return
        heat = float(heat_c)
        # 已触发中的种类用「清除阈值」做滞回，未触发的用「进入阈值」
        want_charge = (charging and heat >= (self.relief_clear_c
                                             if self._relief_charge else self.hot_charge_c))
        want_thermal = heat >= (self.thermal_clear_c
                                if self._relief_thermal else self.hot_cpu_c)

        if not self._relief_on:
            if not (want_charge or want_thermal):
                return
            base = self._read_ac_proc_max()
            if base is None:
                return                   # 读不到就别乱写
            cap = self.relief_cap if want_charge else self.thermal_cap
            if want_charge and want_thermal:
                cap = min(self.relief_cap, self.thermal_cap)
            cap = max(20, min(100, cap))
            if base <= cap:
                # 本来就不高于目标值：不写计划、不置 relief_on
                # （否则退出时的「还原」会做一次无意义的写入）
                self._relief_reason = ("机身热负荷 %.0f℃ 已超阈值，"
                                       "CPU 上限已是 %d%%，无需降温"
                                       % (heat, base))
                return
            if self._write_ac_proc_max(cap):
                self._relief_base_ac = base
                self._relief_on = True
                self._relief_cap_used = cap
                self._relief_charge = want_charge
                self._relief_thermal = want_thermal
                if want_charge and want_thermal:
                    why = "充电高热+插电高温 %.0f℃" % heat
                elif want_charge:
                    why = "充电中机身热 %.0f℃ ≥ %.0f℃" % (heat, self.hot_charge_c)
                else:
                    why = "插电高温 %.0f℃ ≥ %.0f℃" % (heat, self.hot_cpu_c)
                self._relief_reason = ("%s：CPU 上限暂降到 %d%%（原 %d%%）以减少发热"
                                       % (why, cap, base))
        else:
            # 降温过程中新触发的种类记进来（退出时两条滞回都要满足）
            if want_charge:
                self._relief_charge = True
            if want_thermal:
                self._relief_thermal = True
            if not (want_charge or want_thermal):
                self.relief_off(reason="温度回落，已还原 CPU 上限")

    def charging_allowed(self, batt: Dict) -> bool:
        """降温动作的适用性闸门：只在插电、非游戏档时由 manager 放行。
        这里默认按「插电 + 未离电」判断，游戏档由 manager 通过 allow 参数屏蔽。"""
        return bool(batt.get("ac")) and not getattr(self, "_gaming", False)

    def set_context(self, gaming: bool = False):
        """manager 每轮告知当前是否游戏档（游戏档不干预 CPU 上限）"""
        self._gaming = bool(gaming)

    def relief_off(self, reason: str = ""):
        if not self._relief_on:
            return
        base = self._relief_base_ac
        self._relief_on = False
        self._relief_cap_used = None
        self._relief_charge = False
        self._relief_thermal = False
        if base is not None:
            # 写成功才清基准：失败留着，下次还能还原（清掉=永久留在降压值上）
            if self._write_ac_proc_max(base):
                self._relief_base_ac = None
        self._relief_reason = reason or "降温结束，已还原 CPU 上限"

    def reset(self):
        """切档 / 电源切换时调用：无条件还原，避免把上限留在降压值上。
        注意：manager 应在**切换活动计划之前**调用本方法，这样还原写入
        仍落在自建计划上；调用后再作废活动计划缓存。"""
        self.relief_off(reason="切档/电源变化，还原 CPU 上限")
        self.forget_scheme()

    # ------------------------------------------- 「本次充满」意图（2026-10-11）
    # 养护不该是一刀切的 80%：要带出门 / 该做满充校准时，充满是对的。这里把
    # 「充满」做成一次**有期限、会自动结束**的意图，而不是一个永久开关 ——
    # 永久开关等于把养护关掉，那才是真的没保护了。
    def intent_active(self) -> bool:
        it = self._intent
        if not isinstance(it, dict):
            return False
        try:
            return float(it.get("expire") or 0) > time.time()
        except Exception:
            return False

    def _intent_tick(self, ac: bool, pct) -> None:
        """意图的自动结束：过期，或「已经带出去用过了」（离电且电量掉到 90% 以下）。

        不留给用户手动清——忘了清就永远停在 100%，养护等于白设。
        """
        if not isinstance(self._intent, dict):
            return
        if not self.intent_active():
            self.clear_intent("充满意图到期，已自动结束")
            return
        if (not ac) and pct is not None and float(pct) <= 90.0:
            if self._intent.get("reason") == "calibrate":
                # 校准的关键动作是「完整用到低电量再充满」，走到这一步才算完成
                self._last_calib = self._day_key()
                self._data["last_calib"] = self._last_calib
            self.clear_intent("已拔电使用，本次充满结束")

    def set_full_intent(self, reason: str = "manual",
                        hours: Optional[float] = None) -> Dict:
        """声明「本次要充满」，并立刻去改充电上限。

        reason: manual（手动）/ travel（要出门）/ calibrate（做满充校准）
        返回 {ok, intent, write}；write.need_admin=True 表示需要提请权。
        """
        h = self.intent_h if hours is None else float(hours)
        cur = self._chg.get("value") if isinstance(self._chg, dict) else None
        self._intent = {"reason": reason, "created": time.time(),
                        "expire": time.time() + max(0.5, h) * 3600.0,
                        "prev_limit": cur, "target": self.full_limit}
        self._data["intent"] = self._intent
        self._save(force=True)
        res = self.apply_limit(self.full_limit)
        return {"ok": True, "intent": dict(self._intent), "write": res}

    def clear_intent(self, reason: str = "") -> None:
        if not isinstance(self._intent, dict):
            return
        self._intent = None
        self._data["intent"] = None
        self._save(force=True)
        self._intent_reason = reason or "充满意图已结束"

    def apply_limit(self, value: int, track: bool = True) -> Dict:
        """真正去写厂商充电上限。权限不够时返回 need_admin=True，由上层提请权。

        写完**不算成功**：2026-10-11 实测 EC 滞后约 13 分钟才改变充电行为，
        期间还被厂商服务回滚过一次。所以写完只置为「待生效」，由
        `_pending_tick` 在确认窗口内复查（电量是否越线 / 值是否被改回去）。
        """
        try:
            if self._lim_writer is not None:
                res = dict(self._lim_writer(int(value)) or {})
            else:
                res = dict(_hw.set_asus_charge_limit(int(value)) or {})
        except Exception as e:
            return {"ok": False, "need_admin": False,
                    "reason": "写入通道异常: %r" % (e,)}
        if track:
            if res.get("need_admin"):
                self._set_limit_state(int(value), "need_admin",
                                      "需要管理员权限才能改（面板会提请权）")
            elif res.get("ok"):
                # 写进去之后注册表立刻就是新值，强制刷一次 —— 否则 charge_line
                # 还按旧值渲染，会跟刚置的「待生效」状态对不上（文案自相矛盾）。
                try:
                    self._chg_tick(force=True)
                except Exception:
                    pass
                self._start_pending(int(value))
            else:
                self._set_limit_state(int(value), "failed",
                                      res.get("reason") or "写入被拒绝")
        return res

    # ------------------------------------------- 上限下发后的确认（2026-10-11）
    def _start_pending(self, value: int) -> None:
        self._pending = {"value": int(value), "retry": 0, "rollback": False,
                         "deadline": time.time() + float(_hw.EC_CONFIRM_S)}
        self._data["limit_pending"] = self._pending
        self._set_limit_state(int(value), "pending", "已下发，等待 EC 生效")
        self._save(force=True)

    def _set_limit_state(self, value: int, state: str, note: str = "") -> None:
        self._limit_state = {"value": int(value), "state": state, "note": note,
                             "at": time.time()}
        self._data["limit_state"] = self._limit_state

    def _pending_tick(self, ac: bool, pct) -> None:
        """确认窗口内的复查：被回滚就重写一次；到点还没生效就如实说出来。"""
        p = self._pending
        if not isinstance(p, dict):
            return
        target = int(p.get("value") or 0)
        # 1) 行为确认：电量越过了目标上限 —— EC 真的接受了（最有说服力的证据）
        try:
            if pct is not None and float(pct) >= float(target) - 0.5:
                self._limit_done(target, "synced", "已生效（电量已到 %d%% 以上）" % target)
                return
        except Exception:
            pass
        # 2) 被厂商服务改回去了：重试一次。只重试一次 —— 和厂商服务对打
        #    没完没了，且用户可能本来就是在 MyASUS 里手动改的。
        cur = self._chg_tick(force=True).get("value")
        if isinstance(cur, int) and cur != target:
            if int(p.get("retry") or 0) < 1:
                p["retry"] = int(p.get("retry") or 0) + 1
                p["rollback"] = True
                self._data["limit_pending"] = p
                try:
                    self.apply_limit(target, track=False)
                except Exception:
                    pass
                return
            self._limit_done(target, "rolled_back",
                             "厂商服务把上限改回 %d%%，改不动" % cur)
            return
        # 3) 窗口到了还没越线：值保住了，但不敢说 EC 一定吃了
        if time.time() > float(p.get("deadline") or 0):
            self._limit_done(target, "unconfirmed",
                             "已下发但 %d 分钟内没看到电量变化，EC 可能没跟上"
                             % int(_hw.EC_CONFIRM_S // 60))

    def _limit_done(self, value: int, state: str, note: str) -> None:
        self._pending = None
        self._data["limit_pending"] = None
        self._set_limit_state(value, state, note)
        self._save(force=True)

    def restore_limit(self) -> Dict:
        """回到养护上限，并结束意图。"""
        res = self.apply_limit(self.target_limit)
        self.clear_intent("已回到养护上限 %d%%" % self.target_limit)
        return {"write": res}

    def calib_days_since(self) -> Optional[int]:
        """距上次满充校准多少天；从未校准返回 None。"""
        if not self._last_calib:
            return None
        try:
            import datetime
            y, m, d = [int(x) for x in str(self._last_calib).split("-")[:3]]
            return (datetime.date.today() - datetime.date(y, m, d)).days
        except Exception:
            return None

    def calib_due(self) -> bool:
        n = self.calib_days_since()
        return True if n is None else (n >= self.calib_days)

    def dc_h_day(self) -> float:
        """近 scene_days 天的日均离电小时数；样本不足返回 -1.0（调用方须先判）。"""
        days = self._data.get("days", {})
        keys = sorted(days.keys())[-self.scene_days:]
        dc_s = sum(float((days[k] or {}).get("dc_s") or 0) for k in keys)
        sample = sum(float((days[k] or {}).get("sample_s") or 0) for k in keys)
        if sample < 3600.0:                     # 累计不到 1 小时，不妄下结论
            return -1.0
        return (dc_s / 3600.0) / max(1, len(keys))

    def scene(self) -> str:
        """desk（长期插电）/ mobile（常带出门）/ unknown（样本不够）。"""
        v = self.dc_h_day()
        if v < 0:
            return "unknown"
        return "mobile" if v >= self.mobile_dc_h else "desk"

    # ------------------------------------------------------------ 评估
    @staticmethod
    def _h(sec: float) -> float:
        return (sec or 0.0) / 3600.0

    def _recompute(self, batt: Dict):
        days = self._data.get("days", {})
        keys = sorted(days.keys())[-7:]
        full_s = sum(float((days[k] or {}).get("full_soc_s") or 0) for k in keys)
        hot_s = sum(float((days[k] or {}).get("hot_charge_s") or 0) for k in keys)
        deep_s = sum(float((days[k] or {}).get("deep_s") or 0) for k in keys)
        ch_wh = sum(float((days[k] or {}).get("charge_wh") or 0) for k in keys)
        dis_wh = sum(float((days[k] or {}).get("discharge_wh") or 0) for k in keys)
        # 插电补电放电单列：口径上和「离电用电池」分开，但它是真实循环
        acd_wh = sum(float((days[k] or {}).get("ac_drain_wh") or 0) for k in keys)

        full_h, hot_h, deep_h = self._h(full_s), self._h(hot_s), self._h(deep_s)
        span_days = max(1, len(keys))
        per_day = lambda h: h / span_days
        # 评分：从 100 分里扣，扣分项按「日均时长」线性折算并封顶
        penalty_full = min(30.0, per_day(full_h) * 6.0)      # 满充搁置 5h/天 -> 30 分
        penalty_hot = min(30.0, per_day(hot_h) * 12.0)       # 高温充电 2.5h/天 -> 30 分
        penalty_deep = min(20.0, per_day(deep_h) * 10.0)     # 深放 2h/天 -> 20 分
        self._score = int(round(max(0.0, 100.0 - penalty_full - penalty_hot - penalty_deep)))

        full_mwh = batt.get("full_mwh")
        cyc = None
        if full_mwh:
            # 等效循环按「总放电能量」折算：插电补电放电也是电池的循环，
            # 漏掉它会把 Type-C 弱电源游戏的真实代价藏起来（10-18 实例）。
            cyc = round((dis_wh + acd_wh) / max(0.001, full_mwh / 1000.0), 2)

        chg = dict(getattr(self, "_chg", None) or {})
        chg_v = chg.get("value")
        limited = isinstance(chg_v, int) and chg_v <= self.advise_limit
        sc = self.scene()
        dc_h = self.dc_h_day()
        intent_on = self.intent_active()

        adv: List[str] = []
        # 满充搁置的唠叨只在「长期插电 + 没有充满意图」时才说 —— 有目的的满充不是坏习惯
        if per_day(full_h) >= 1.0 and not limited and sc != "mobile" and not intent_on:
            if chg_v == 100:
                tip = ("当前充电上限 100%（满容量）→ 去 MyASUS → 设备设置 → 电源和电池 → "
                       "「电池健康充电」改成「平衡模式」(80%) 或「长效使用模式」(60%)")
            elif chg_v is not None:
                tip = "当前充电上限 %d%%，建议压到 %d%% 以下" % (chg_v, self.advise_limit)
            elif chg.get("myasus"):
                tip = ("没读到充电上限，先去 MyASUS → 设备设置 → 电池健康充电 设一次 80%")
            else:
                tip = ("建议装 MyASUS（微软商店免费）后把充电上限设为 %d%%；"
                       "临时办法是充满后拔掉适配器" % self.advise_limit)
            adv.append("近 %d 天满充搁置日均 %.1fh（≥%d%%）→ 高电压+常温老化约 2×。%s"
                       % (span_days, per_day(full_h), int(self.full_soc), tip))
        if per_day(hot_h) >= 0.25:
            adv.append("近 %d 天高温充电日均 %.1fh → 温度每 +10℃ 老化约翻倍，"
                       "充电时尽量避免重负载/保证进风口不被遮挡" % (span_days, per_day(hot_h)))
        if per_day(deep_h) >= 0.25:
            adv.append("近 %d 天低于 %d%% 的深放日均 %.1fh → 阳极析锂风险，建议 30~40%% 就补电"
                       % (span_days, int(self.deep_soc), per_day(deep_h)))
        # 插电补电放电：弱电源打游戏的代价，必须让人看见（2026-10-18 实例：
        # 一天放了 ~78Wh 而 discharge_wh 记 0.00，补电放电被整段藏起来了）
        if acd_wh >= 1.0:
            adv.append("近 %d 天插电补电放电 %.1fWh（日均 %.1fWh，已计入等效循环）→ "
                       "Type-C 供不上时游戏是大电流+高温放电，最伤电池；"
                       "换 100W 以上电源、或让 PD 保护限帧/断独显把整机压到供电内"
                       % (span_days, acd_wh, per_day(acd_wh)))
        if cyc is not None and len(keys) >= 2:
            adv.append("近 %d 天等效循环 %.2f 次（按放电能量折算）" % (len(keys), cyc))
        if not adv:
            adv.append("充放电习惯良好：无明显满充搁置 / 高温充电 / 深放")
        # ---- 充电上限：按场景给结论，而不是一律「建议 80%」 ----
        if intent_on:
            why = {"manual": "手动", "travel": "要出门", "calibrate": "满充校准"}.get(
                self._intent.get("reason"), "手动")
            adv.append("本次允许充满（%s）：目标上限 %d%%，拔电用到 90%% 以下会自动结束"
                       "并提示切回 %d%%" % (why, self.full_limit, self.target_limit))
        elif sc == "desk" and chg_v is not None:
            if chg_v >= 100:
                adv.append("你基本长期插电（近 %d 天日均离电 %.1fh）→ 充电上限建议压到 %d%%："
                           "满电搁置的老化速率约为维持 60%% 时的 2 倍。%s"
                           % (min(self.scene_days, len(keys)), dc_h, self.target_limit,
                              self._how_to_set()))
            elif chg_v > self.target_limit:
                adv.append("长期插电场景：当前上限 %d%%，可再压到 %d%%（%s）"
                           % (chg_v, self.target_limit, self._how_to_set()))
            else:
                adv.append("充电上限 %d%%（%s）：长期插电场景下这是最合适的档位，保持即可"
                           % (chg_v, chg.get("mode_cn") or "自定义"))
        elif sc == "mobile" and chg_v is not None:
            if chg_v < 100:
                adv.append("你常带出门（日均离电 %.1fh）但上限只到 %d%% → 白砍 %d%% 续航，"
                           "换来的寿命收益不划算。建议改回 100%%，只在明确要长期插电那几天"
                           "用「本次充满 / 回养护」临时压低。%s"
                           % (dc_h, chg_v, 100 - chg_v, self._how_to_set()))
            else:
                adv.append("你常带出门（日均离电 %.1fh）：100%% 是合理的；"
                           "若某几天长期插电，可临时切到 %d%%" % (dc_h, self.target_limit))
        elif chg_v is not None:
            # 场景还没判出来（样本不足）：只陈述现状 + 说清「再观察几天」，
            # 既不念叨 80%，也不装作已经有结论。
            if chg_v >= 100:
                adv.append("充电上限目前是 100%%（满容量）：插电不拔时建议压到 %d%%。%s"
                           % (self.target_limit, self._how_to_set()))
            else:
                adv.append("充电上限已设 %d%%（%s）：再用几天，程序会按你的插电/离电"
                           "习惯判断该继续压低还是改回充满"
                           % (chg_v, chg.get("mode_cn") or "自定义"))

        # ---- 下发失败要如实说，不能假装已经生效 ----
        st = (self._limit_state or {}).get("state")
        if st in ("unconfirmed", "rolled_back", "failed", "need_admin"):
            note = (self._limit_state or {}).get("note") or ""
            v = (self._limit_state or {}).get("value")
            if st == "rolled_back":
                adv.append("上次改充电上限被厂商服务改回去了（%s）→ 本程序不再重试，"
                           "请在 MyASUS 里改；两边同时改会打架" % note)
            elif st == "need_admin":
                adv.append("改充电上限要管理员权限（%s）→ 面板上点按钮会弹一次 UAC，"
                           "确认后即可" % note)
            elif st == "failed":
                adv.append("上次改充电上限失败：%s" % (note or "写入被拒绝"))
            else:
                adv.append("上次下发的上限 %s%% 还没看到生效（%s）→ 厂商服务可能要更久，"
                           "也可直接在 MyASUS 里确认" % (v, note))

        # ---- 满充校准（电量计需要端点修正，长期不充满会漂移）----
        if self.calib_due() and not intent_on:
            n = self.calib_days_since()
            if n is None:
                adv.append("还没做过满充校准 → 建议每 %d 天做一次完整充放（用到 %d%% 以下再充满），"
                           "否则电量计会漂移（表现为「显示 30%% 突然关机」「一直停在 99%%」）"
                           % (self.calib_days, int(self.deep_soc)))
            else:
                adv.append("距上次满充校准已 %d 天（周期 %d 天）→ 该做一次完整充放来修正 SOC 漂移"
                           % (n, self.calib_days))
        self._advice = adv
        self._stat = {
            "days": len(keys), "full_h": round(full_h, 2), "hot_h": round(hot_h, 2),
            "deep_h": round(deep_h, 2), "full_h_day": round(per_day(full_h), 2),
            "hot_h_day": round(per_day(hot_h), 2), "deep_h_day": round(per_day(deep_h), 2),
            "charge_wh": round(ch_wh, 1), "discharge_wh": round(dis_wh, 1),
            "ac_drain_wh": round(acd_wh, 1),
            "cycles_est": cyc,
        }

    def report(self) -> Dict:
        chg = dict(getattr(self, "_chg", None) or {})
        return {
            "enabled": self.enabled,
            "score": self._score,
            "advice": list(self._advice),
            "stat": dict(getattr(self, "_stat", {}) or {}),
            "relief_on": self._relief_on,
            "relief_reason": self._relief_reason,
            "relief_cap": self._relief_cap_used,
            "hot_charge_c": self.hot_charge_c,
            "hot_cpu_c": self.hot_cpu_c,
            "thermal_cap": self.thermal_cap,
            # 华硕充电上限（只读镜像值）：{supported,value,mode,mode_cn,label,myasus}
            "charge_limit": chg,
            "charge_line": self.charge_line(),
            # 下发后的生效状态：pending(待生效~15min) / synced / unconfirmed /
            # rolled_back(被厂商服务改回) / need_admin / failed
            "limit_state": dict(self._limit_state) if self._limit_state else None,
            "limit_pending": bool(self._pending),
            # 按场景的充电上限策略（2026-10-11）
            "scene": self.scene(),
            "dc_h_day": (round(self.dc_h_day(), 2) if self.dc_h_day() >= 0 else None),
            "intent": dict(self._intent) if isinstance(self._intent, dict) else None,
            "intent_active": self.intent_active(),
            "intent_reason": getattr(self, "_intent_reason", ""),
            "target_limit": self.target_limit,
            "full_limit": self.full_limit,
            "calib_due": self.calib_due(),
            "calib_days_since": self.calib_days_since(),
            "today": dict(self._data.get("days", {}).get(self._today_key) or {}),
        }

    def _how_to_set(self) -> str:
        """「怎么改充电上限」的一句话（有没有 MyASUS 决定措辞）。"""
        chg = getattr(self, "_chg", None) or {}
        if chg.get("supported"):
            return ("去 MyASUS → 设备设置 → 电池健康充电 切换，"
                    "或用面板上的「本次充满 / 回养护」（首次需确认一次 UAC）")
        if chg.get("myasus"):
            return "MyASUS 里设一次「电池健康充电」，之后本程序就能读到并跟随"
        return "本机型读不到厂商充电上限接口，只能用「充满后拔掉适配器」这种土办法"

    def charge_line(self) -> str:
        """充电上限的一行短文本（面板/网页共用）"""
        chg = getattr(self, "_chg", None) or {}
        v = chg.get("value")
        if v is None:
            return "充电上限：未读到" + ("（MyASUS 已装，去设一次）" if chg.get("myasus") else "")
        # 刚下发还没被 EC 确认：不能当成既成事实报给用户（实测滞后 ~13 分钟）
        st = (self._limit_state or {}).get("state")
        if st == "pending" and int(v) == int((self._limit_state or {}).get("value") or -1):
            return "充电上限：%d%%（已下发，待生效）" % int(v)
        if v >= 100:
            return "充电上限：100%（满容量 · 无保护）"
        return "充电上限：%d%%（%s）" % (v, chg.get("mode_cn") or "自定义")

    def summary_line(self) -> str:
        """给面板用的一行短文本"""
        if not self.enabled:
            return "电池养护：未启用"
        s = getattr(self, "_stat", {}) or {}
        score = "—" if self._score is None else str(self._score)
        extra = " · 降温中" if self._relief_on else ""
        return "电池养护 %s 分%s（满充搁置 %.1fh/日 · 高温充电 %.1fh/日）" % (
            score, extra, s.get("full_h_day", 0.0), s.get("hot_h_day", 0.0))


if __name__ == "__main__":  # pragma: no cover
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "care_demo.json")
    try:
        os.remove(p)
    except Exception:
        pass
    care = BatteryCare({"care_heat_relief": False}, p)   # 演示不做任何 powercfg 写入
    seq = [
        (dict(ac=True, charging=True, discharging=False, percent=97,
              rate_w=-40.0, full_mwh=50000), 90.0, 3600.0),        # 满充+充电高热
        (dict(ac=True, charging=False, discharging=False, percent=100,
              rate_w=None, full_mwh=50000), 78.0, 7200.0),         # 满充搁置
        (dict(ac=False, charging=False, discharging=True, percent=12,
              rate_w=21.0, full_mwh=50000), 60.0, 1800.0),         # 深放
    ]
    for batt, heat, dt in seq:
        r = care.feed(batt, heat, dt)
        print("%s | score=%s relief=%s" % (r["today"], r["score"], r["relief_on"]))
    for a in care.report()["advice"]:
        print("  -", a)
    print(care.summary_line())
