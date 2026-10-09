# -*- coding: utf-8 -*-
"""独显 ACPI 级断电（GPU Eco，借鉴 G-Helper 的 GPUEcoROG 通道）——2026-10-06 本机打通

=============================================================================
一、这是什么
=============================================================================
ATKACPI 设备 ID 0x00090020（G-Helper 的 GPUEcoROG）：写 1 = 独显在 ACPI 层断电，
设备从系统里消失（设备管理器里直接不见了），任何后台应用都无法再唤醒它；
写 0 = 恢复。比「Windows 自动把独显挂起」彻底得多——挂起状态某些应用
（Chrome 硬解、截图工具、NVML 自身）仍会把独显唤醒，白吃 3~8 W 空载功耗。

本机 2026-10-06 实测：普通权限 DEVS 写 eco=1 → 3 秒内 DSTS 回读=1（真生效），
还原 eco=0 → 回读=0、NVML 重新可用。这是继电源计划旋钮之后第二个可写的
华硕 ACPI 通道（风扇曲线/充电阈值/PPT 全被 AsusOptimization 托管，写不动）。

二、自动化策略（本模块）
=============================================================================
· 插电  → eco=0（独显可用，游戏/性能不受影响）
· 离电  → eco=1（独显彻底断电，走 AMD 核显）——正是用户 2026-10-04
  「离电只走 AMD 核显」要求的硬件级落实
· 例外：离电 + 游戏档 → 不断电（离电也允许手动跑游戏档）
· 系统自带「平衡」档（auto 全关）时不干预，与其它自动化同口径
· 写后必须回读校验（服务托管教训）；失效标记后不再反复打扰
· 程序退出 restore()：若独显是被本模块断电的，恢复 eco=0

三、风险与防御
=============================================================================
· NVML 句柄会随设备消失而失效：离电路径本来就不碰 NVML（跳过独显驱动），
  插电还原后 Nvml() 重新实例化即可，nvmlctl 全程防御式绑定不会崩。
· 若回读长时间不生效（BIOS 差异/服务抢管），标记 unavailable 并放弃，
  绝不反复硬写。
"""
from __future__ import annotations

from typing import Optional

ECO_DEV = 0x00090020
M_DSTS = 0x53545344     # ATKACPI 读（注意：这是模块级常量，不是 AtkAcpi 类属性）
M_DEVS = 0x53564544     # ATKACPI 写


class GpuEco:
    """独显 Eco 自动化。atk 可注入（测试用假通道），缺省用 lp.atkacpi 单例。"""

    def __init__(self, atk=None) -> None:
        self.atk = atk
        self.enabled = True          # 自动开关（config gpu_eco_auto）
        self._applied: Optional[int] = None   # 本模块最后一次成功写入的目标值
        self._broken = False         # 通道失效（写不生效）后不再打扰
        self._fails = 0              # 连续失败计数（按 set 轮次）
        self._last_note = ""
        self._dbg = ""               # 最近一次读失败的原因（诊断用）

    # -------------------------------------------------- 通道
    def _a(self):
        if self.atk is not None:
            return self.atk if self.atk.ok else None
        try:
            from . import atkacpi
            return atkacpi.get()
        except Exception:
            return None

    def read(self) -> Optional[int]:
        a = self._a()
        if a is None:
            self._dbg = "no handle"
            return None
        try:
            raw = a._call(M_DSTS, (ECO_DEV & 0xFFFFFFFF).to_bytes(4, "little")
                          + (0).to_bytes(4, "little"), 16)
            if raw is None:
                self._dbg = "dsts err=%d" % getattr(a, "last_error", -1)
                return None
            u = int.from_bytes(raw[:4], "little")
            if not (u & 0x10000):
                self._dbg = "dsts noflag 0x%08X" % u
                return None
            return u & 0xFFFF
        except Exception as e:
            self._dbg = "exc %r" % e
            return None

    def _write_raw(self, value: int) -> bool:
        """DeviceIoControl 本身是否成功。

        注意本机实测（2026-10-06）：eco 的 DEVS 写在「目标状态已达成」时返回
        ret=0（视为 no-op），只有真正翻转状态才返回 ret=1 ⇒ 不能拿 ret 当
        成败依据，**以回读为准**；这里只区分「设备响应了」和「硬错误」。
        """
        a = self._a()
        if a is None:
            return False
        try:
            raw = a._call(M_DEVS, (ECO_DEV & 0xFFFFFFFF).to_bytes(4, "little")
                          + (value & 0xFFFFFFFF).to_bytes(4, "little"), 16)
            return raw is not None
        except Exception:
            return False

    # -------------------------------------------------- 应用（幂等 + 校验）
    def set(self, target: int) -> bool:
        """把独显 Eco 设到目标值（0=开独显 1=断电）。已到位则零写入。"""
        target = 1 if target else 0
        if self._broken:
            return False
        cur = self.read()
        if cur is not None and cur == target:
            self._applied = target
            self._fails = 0
            self._last_note = ""
            return True
        ok = False
        for _attempt in range(2):
            if not self._write_raw(target):
                self._last_note = "DEVS 无响应"
                continue
            # 回读校验（设备枚举要 1~3 秒）。留到 6 拍（3.6s）：
            # 实测生效时间就在 3 秒附近，5 拍 * 0.6s 卡得太死，容易误判失败。
            import time
            for _ in range(6):
                time.sleep(0.6)
                cur = self.read()
                if cur == target:
                    ok = True
                    break
            if ok:
                break
        if ok:
            self._applied = target
            self._fails = 0
            self._last_note = ""
        else:
            self._fails += 1        # 按 set() 轮次计失败，连续 3 轮判死
            self._last_note = "写后回读未确认（目标 %d）" % target
            if self._fails >= 3:
                self._broken = True
                self._last_note += "，通道已放弃"
        return ok

    # -------------------------------------------------- 自动化 tick
    def tick(self, on_ac: bool, active_profile: Optional[str]) -> None:
        """由 manager 巡检调用。幂等：目标没变就只读一次。"""
        if not self.enabled:
            if self._applied == 1:
                self.set(0)          # 用户关了功能 → 还原独显
            return
        if active_profile == "balanced":
            return                    # 系统平衡档：一切自动化都不干预
        if on_ac:
            # 插电：独显必须可用。不管 eco 是谁关的（GHelper/上一次会话），
            # 只要当前是断电状态就还原；初始状态记一笔"已确认可用"。
            if self.read() == 1:
                self.set(0)
            elif self._applied is None:
                self._applied = 0
            return
        # 离电：游戏档不断电（离电也可手动跑游戏）
        want = 0 if active_profile == "gaming" else 1
        cur = self.read()
        if cur is not None and cur == want:
            self._applied = want
            return
        self.set(want)

    # -------------------------------------------------- 还原 / 状态
    def restore(self) -> bool:
        """程序退出时调用：只还原本模块断的电。

        退出是「一次性、没人再看结果」的场合，所以这里比 tick() 更啰嗦：
        给三轮机会，并把失败原因留在 _last_note 里，绝不静默。
        离电状态下固件有可能拒绝给独显上电（尚未证实），那种情况下
        独显会保持断电到下次插电 —— tick(on_ac=True) 会把它们拉回来。
        """
        if self._applied != 1:
            return False
        for _ in range(3):
            if self.set(0):
                return True
            self._fails = 0      # 退出路径不累计失败，避免下次启动被判死
        self._last_note = ("退出还原失败：独显可能仍处于断电状态，"
                           "插电后 tick() 会自动恢复")
        return False

    def report(self) -> dict:
        cur = self.read()
        return {
            "enabled": self.enabled,
            "eco": cur,                          # 1=断电 0=活动 None=读不到
            "applied_by_us": self._applied,
            "broken": self._broken,
            "note": self._last_note,
            "dbg": self._dbg,
        }

    def line(self, on_ac: bool) -> str:
        """面板一句话状态。"""
        if self._broken:
            return "独显 Eco 通道不可用"
        if on_ac:
            return ""
        r = self.report()
        if r["eco"] == 1:
            return "独显已 ACPI 断电"
        return "独显待断电" if self.enabled else ""
