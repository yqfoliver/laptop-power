# -*- coding: utf-8 -*-
r"""散热 / 噪音策略 —— 「风扇调节」在软件层能做的全部

=============================================================================
一、硬件事实（2026-10-05 用 G-Helper 的 ATKACPI 协议逐条实测标定）
=============================================================================
旧结论「本机风扇完全不可控」只对了一半，必须拆成三层看：

  A) **ATKACPI 通道本身完全可用**（之前判定失败是因为漏了 DeviceInit() 握手）。
     普通权限即可读：CPU/GPU 风扇档位(0~120%)、CPU 温度、GPU TGP 基准/加值、
     ASUS 性能模式、三条内置风扇曲线。⇒ 见 lp/atkacpi.py

  B) **写入被 ASUS 服务托管**：往 DEVS 写 性能模式 / 风扇曲线，接口一律返回 1(成功)，
     但**回读立刻变回原值**（多档位、手动模式前置都试过）。原因与充电阈值同源——
     `AsusOptimization` / `ASUS System Control Interface` 服务持有这些设置并周期性
     重新下发。⇒ 只有只读价值；真要改得在 MyASUS / Armoury Crate 里改。
     实测：连写 4 档性能模式，风扇档位只在 28~43% 之间抖动（温度噪声量级），
           风扇曲线写入后回读 100% 是原曲线。

  C) **真正能写的是电源计划里的隐藏旋钮**（powrprof 直调，见 lp/powrprof.py）：
     `powercfg /q` 的文本解析读不到它们，但 C API 一直都在，且写入立即生效：
       · SYSCOOLPOL  系统散热方式：0=被动（先降频再提速风扇）1=主动（先提速风扇）
                     ——「被动」是**操作系统层面最直接的降噪开关**
       · PERFBOOSTMODE 睿频提升策略：0=禁用 1=启用 2=激进 3=高效启用 4=高效激进
                     —— 关掉/收敛睿频突跳能显著削掉风扇转速的**阶跃**（主观噪音主因）
       · PERFEPP     能源性能首选项：0=最偏性能 … 100=最偏能效，越大频率越平滑
       · PROCTHROTTLEMAX 最大处理器状态（原有）
     HP/Dell/联想官方支持文档一致口径：**限制最大处理器状态 + 抑制睿频突跳**
     是软件层最有效的降噪手段（消除功耗/温度瞬态尖峰 → 风扇不突变）。

二、三档策略（既写软杠杆，也写上面 C 组的硬旋钮）
=============================================================================
  auto  自动：不额外干预，并把之前写过的硬件旋钮**还原成接管前的值**
  quiet 静音优先：散热方式=被动 + 睿频=高效启用 + EPP 偏能效 + CPU 上限封顶
  perf  性能优先：散热方式=主动 + 睿频=激进 + EPP 偏性能，温度线放宽

三、安全边界
=============================================================================
  · 所有硬件写入**只落在自建计划 GUID 上**，系统自带「平衡」一个字节不碰（硬约束）。
  · 接管前先把原值存进 `_hw_saved`，切回 auto / 程序退出时逐项还原。
  · ATKACPI 的硬件写入默认关闭（被服务托管，写了也会被还原），仅作只读监视。
"""
from __future__ import annotations

from typing import Dict, Optional

from . import powrprof as pp

try:
    from . import atkacpi as _atk
except Exception:                       # pragma: no cover
    _atk = None

ORDER = ("auto", "quiet", "perf")

LABEL = {
    "auto": "自动",
    "quiet": "静音优先",
    "perf": "性能优先",
}

DESC = {
    "auto": "不额外干预，各档位按默认参数与温度线运行（接管过的硬件旋钮会还原）",
    "quiet": "散热方式转被动、抑制睿频突跳、CPU 上限封顶：风扇更低更稳（代价是峰值性能）",
    "perf": "散热方式转主动、睿频放开、温度线放宽：散热全开保性能（噪音相应更高）",
}

# 硬件旋钮的目标值（quiet / perf）；auto 不出现在表里 = 写回原值
HW_TARGET: Dict[str, Dict[str, int]] = {
    "quiet": {
        "SYSCOOLPOL": 0,        # 被动：优先降频而不是提风扇
        "PERFBOOSTMODE": 3,     # 高效启用：允许睿频但收敛突跳
        "PERFEPP": 60,          # 偏能效 → 频率曲线更平滑
    },
    "perf": {
        "SYSCOOLPOL": 1,        # 主动
        "PERFBOOSTMODE": 2,     # 激进
        "PERFEPP": 0,           # 偏性能
    },
}


class ThermalPolicy:
    """把「散热/噪音倾向」翻译成本程序真正能写的旋钮。

    软件杠杆（写进各档位的 proc_max 上限 / alloc 温度线）：
      cpu_cap_ac      非游戏档插电时的 CPU 上限封顶（%）
      relief_trigger  插电高温保护触发线（℃）
      relief_cap_cpu  高温保护时压到的 CPU 上限（%）
      alloc_cpu_temp  游戏档 CPU 温度线（℃）
      alloc_gpu_temp  游戏档 GPU 温度线（℃）
    硬件杠杆（powrprof 直写自建计划，仅 quiet / perf 生效）：
      hw_values       {SYSCOOLPOL: n, PERFBOOSTMODE: n, PERFEPP: n}
      atk_mode        ATK 性能模式目标（默认 None = 不干预）
    """

    def __init__(self, cfg: Optional[Dict] = None, log=None):
        self.cfg = cfg if cfg is not None else {}
        self.log = log
        self.scheme: Optional[str] = None      # 由 manager 注入自建计划 GUID
        self.mode = str(self.cfg.get("thermal_mode") or "auto").lower()
        if self.mode not in LABEL:
            self.mode = "auto"
        self._hw_saved: Dict[str, int] = {}
        self._hw_applied: Dict[str, int] = {}
        self._hw_err = ""
        self._atk_note = ""
        # 硬件通道句柄（实例属性方便测试注入假通道；正常就是 lp.powrprof 模块）
        self.pp = pp
        self._load()

    # ------------------------------------------------------------ 参数
    def _load(self) -> None:
        c = self.cfg
        self.cpu_cap_ac: Optional[int] = None
        self.relief_trigger: Optional[float] = None
        self.relief_cap_cpu: Optional[int] = None
        self.alloc_cpu_temp: Optional[float] = None
        self.alloc_gpu_temp: Optional[float] = None

        if self.mode == "quiet":
            self.cpu_cap_ac = max(30, min(100, int(c.get("thermal_quiet_cpu_pct", 75))))
            self.relief_trigger = float(c.get("thermal_quiet_relief_c", 88))
            self.relief_cap_cpu = max(30, min(100, int(c.get("thermal_quiet_relief_pct", 70))))
            self.alloc_cpu_temp = float(c.get("thermal_quiet_gpu_cpu_temp_c", 90))
            self.alloc_gpu_temp = float(c.get("thermal_quiet_gpu_temp_c", 86))
        elif self.mode == "perf":
            self.relief_trigger = float(c.get("thermal_perf_relief_c", 98))
            self.relief_cap_cpu = max(30, min(100, int(c.get("thermal_perf_relief_pct", 85))))
            self.alloc_cpu_temp = float(c.get("thermal_perf_gpu_cpu_temp_c", 98))
            self.alloc_gpu_temp = float(c.get("thermal_perf_gpu_temp_c", 92))

        # 硬件杠杆（可被 config 覆盖，方便面板做高级设置）
        base = HW_TARGET.get(self.mode) or {}
        self.hw_values: Dict[str, int] = {}
        for k in ("SYSCOOLPOL", "PERFBOOSTMODE", "PERFEPP"):
            if k in base:
                try:
                    self.hw_values[k] = int(c.get("thermal_%s_%s" % (self.mode, k.lower()),
                                                 base[k]))
                except Exception:
                    self.hw_values[k] = base[k]
        # ATK 性能模式：默认 None = 不碰（被 ASUS 服务托管，写了会被还原）
        raw = c.get("thermal_%s_atk_mode" % self.mode)
        if raw in (0, 1, 2, 3, 4, "0", "1", "2", "3", "4"):
            self.atk_mode: Optional[int] = int(raw)
        elif self.mode == "quiet" and c.get("thermal_atk_enabled"):
            self.atk_mode = 2
        elif self.mode == "perf" and c.get("thermal_atk_enabled"):
            self.atk_mode = 1
        else:
            self.atk_mode = None

    # ------------------------------------------------------------ 切换
    def set_mode(self, mode: str) -> bool:
        m = str(mode or "").lower()
        if m not in LABEL:
            return False
        self.mode = m
        self.cfg["thermal_mode"] = m
        self._load()
        return True

    def next_mode(self) -> str:
        try:
            i = ORDER.index(self.mode)
        except ValueError:
            i = 0
        return ORDER[(i + 1) % len(ORDER)]

    def reload_cfg(self, cfg: Optional[Dict] = None) -> None:
        if cfg is not None:
            self.cfg = cfg
        self._load()

    # ------------------------------------------------------------ 硬件杠杆：应用 / 还原
    def hw_apply(self, scheme: Optional[str] = None) -> str:
        """按当前策略写硬件旋钮。幂等：只在目标值变化时才写。返回一行说明。"""
        if scheme:
            self.scheme = scheme
        if not scheme and not self.scheme:
            return ""
        if not self.pp.available():
            self._hw_err = "powrprof 不可用，硬件旋钮跳过"
            return self._hw_err

        want = dict(self.hw_values)          # auto 时为 {} → 走还原分支
        # 目标与已应用一致 → 不重复写（主循环每 5 秒调一次）
        if want == self._hw_applied:
            return ""
        if not want:
            self.hw_restore()
            return ""

        done = []
        for attr, val in want.items():
            cur = self.pp.read_attr(self.scheme, attr)
            if cur is None:
                continue
            if attr not in self._hw_saved:
                self._hw_saved[attr] = cur          # 记下接管前的原值
            if cur == val:
                self._hw_applied[attr] = val
                continue
            if self.pp.write_range(self.scheme, attr, val):
                self._hw_applied[attr] = val
                done.append("%s %s→%s" % (attr, cur, val))
        self._hw_applied = {k: v for k, v in self._hw_applied.items() if k in want}
        if done and self._is_active():
            self.pp.set_active(self.scheme)              # 写索引后激活一次才生效
        self._hw_err = ""
        if self.atk_mode is not None:
            note = self._apply_atk(self.atk_mode)
            if note:
                done.append(note)
        return "散热策略：%s（%s）" % (self.label, "、".join(done)) if done else ""

    def _apply_atk(self, mode: int) -> str:
        if _atk is None:
            return ""
        a = _atk.get()
        if a is None:
            return ""
        cur = a.perf_mode()
        if cur == mode:
            return ""
        if not a.write_int("perf_mode", int(mode)):
            self._atk_note = "ASUS 性能模式写入被拒"
            return ""
        back = a.perf_mode()
        if back != mode:
            self._atk_note = ("ASUS 性能模式被其服务还原（%s→%s），本机该通道只读"
                              % (mode, back))
            return ""
        self._atk_note = ""
        return "ASUS 性能模式→%s" % mode

    def hw_restore(self) -> bool:
        """把接管过的硬件旋钮还原成接管前的值。"""
        if not self._hw_saved or not self.pp.available():
            self._hw_applied = {}
            return False
        changed = False
        for attr, val in list(self._hw_saved.items()):
            if self.pp.write_range(self.scheme, attr, val):
                changed = True
            self._hw_saved.pop(attr, None)
        if changed and self._is_active():
            self.pp.set_active(self.scheme)
        self._hw_applied = {}
        self._hw_err = ""
        return changed

    def _is_active(self) -> bool:
        """只在自建计划**当前就是活动计划**时才激活它。

        否则（程序正切回系统「平衡」档）还原写入后激活会把用户的系统计划又顶掉，
        违反「切到平衡后系统计划保持活动」的语义。
        """
        try:
            return bool(self.scheme) and self.pp.active_scheme() == self.scheme
        except Exception:
            return False

    # ------------------------------------------------------------ 输出
    @property
    def label(self) -> str:
        return LABEL.get(self.mode, "自动")

    def summary(self) -> dict:
        """给 status()/面板/网页用的一行状态。"""
        return {
            "mode": self.mode,
            "label": self.label,
            "desc": DESC.get(self.mode, ""),
            "cpu_cap_ac": self.cpu_cap_ac,
            "relief_trigger": self.relief_trigger,
            "alloc_cpu_temp": self.alloc_cpu_temp,
            "alloc_gpu_temp": self.alloc_gpu_temp,
            "hw_values": dict(self.hw_values),
            "hw_applied": dict(self._hw_applied),
            "atk_mode": self.atk_mode,
            "atk_note": self._atk_note,
            "hw_err": self._hw_err,
            "hw_available": self.pp.available(),
        }

    def line(self) -> str:
        """面板用短文本。"""
        if self.mode == "quiet":
            return "散热策略：静音优先（CPU 上限 %d%%·散热转被动）" % (self.cpu_cap_ac or 100)
        if self.mode == "perf":
            return "散热策略：性能优先（散热转主动·温度线放宽）"
        return "散热策略：自动（不额外干预）"

    def hw_line(self) -> str:
        """硬件旋钮现状（给面板/网页显示）。"""
        if not self.hw_values:
            return "硬件旋钮：未接管"
        parts = []
        for k, v in self.hw_values.items():
            cur = self.pp.read_attr(self.scheme, k) if (self.pp.available() and self.scheme) else None
            if k == "SYSCOOLPOL":
                parts.append("散热方式=%s" % ("被动" if v == 0 else "主动"))
            elif k == "PERFBOOSTMODE":
                parts.append("睿频=%s" % self.pp.BOOST_LABEL.get(v, v))
            elif k == "PERFEPP":
                parts.append("能效偏好=%d" % v)
            if cur is not None and cur != v:
                parts[-1] += "（实际 %s）" % cur
        return "硬件旋钮：" + "、".join(parts)

    # ------------------------------------------------------------ 档位写入钩子
    def apply_to_knobs(self, name: str, on_ac: bool, knob: str, val: int) -> int:
        """档位写入时的封顶钩子：非游戏档 + 插电 + proc_max -> 封顶到策略上限。

        游戏档不做上限封顶（帧率优先），改由 alloc 的温度线间接影响散热。
        """
        if (self.cpu_cap_ac and on_ac and knob == "proc_max"
                and name not in ("gaming", "balanced")):
            try:
                return min(int(val), int(self.cpu_cap_ac))
            except Exception:
                return val
        return val


if __name__ == "__main__":      # pragma: no cover
    for m in ORDER:
        t = ThermalPolicy({"thermal_mode": m})
        t.scheme = "684f7fa1-32d5-41ed-b743-f71f619fb2d0"
        print("%-6s %s" % (m, t.line()))
        print("       hw=%s atk=%s" % (t.hw_values, t.atk_mode))
