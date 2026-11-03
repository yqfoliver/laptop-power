# -*- coding: utf-8 -*-
"""软件风扇曲线（对标 Armoury Crate 的「手动模式」，2026-11-03 本机实测打通）

=============================================================================
一、为什么曲线要写在我们自己程序里
=============================================================================
ATKACPI 的风扇曲线设备（CPU 0x00110024 / GPU 0x00110025）**读得到、写不动**：
`write_buf` 返回 True，但把 8 个点整体 +6% 后回读仍是原值 —— 写入是空操作，
被 AsusOptimization 托管（2026-11-03 逐点实测，见 tools 探针记录）。
照着它做「曲线编辑器」就是一张点了没反应的假卡片。

真能写的只有**风扇档位本体**（CPU 0x00110013）：写一个数值，EC 进入
「手动模式」（DSTS 返回 tag=0x0002、数值即固定转速）；**写 0 释放回自动**
（tag 回到 0x0001，转速随后缓慢收敛到出厂曲线）。GPU 风扇同理，但它的
回读格式不稳定（偶发返回 0x8034 这类畸形值），所以本模块 **v1 只接管 CPU 风扇**，
GPU 继续由 EC 自动控制（GPU 反正有独立的温度闭环）。

⇒ 于是曲线存在本地：界面编辑 8 个点 → 本模块每 2 秒按 CPU 温度插值出目标
转速 → 写 cpu_fan。效果与 Armoury Crate 手动模式一致，但曲线归我们管。

二、保险（进程死了不能把风扇锁在半空）
=============================================================================
1. **退出必释放**：main.py 退出清理调用 release()（带 6 秒超时，与其它还原同款）。
2. **启动先收尾**：构造时读一次 tag，发现自己上次留下的手动模式（tag=0x0002）
   立刻写 0 交回自动 —— 强杀/断电留下的锁死状态不会带进新会话。
3. **看门狗**：接管期间后台线程每 5 秒检查心跳，超过 25 秒没刷新（巡检线程
   卡死/异常退出）自动 release。心跳由 tick() 刷新。
4. **下限钳制**：目标转速永不低于 `fan_curve_min_pct`（默认 30%），就算我们算错
   也不会把风扇压到停转。
5. **拒绝死磕**：连续 3 次写入后回读 tag 不是手动模式，判通道失效、自动停用并
   在面板写明原因（不做「写了但没生效」的假回执）。

三、已知限制
=============================================================================
· 只控 CPU 风扇；GPU 风扇仍由 EC 曲线控制（通道回读不稳，不做）。
· 接管期间 EC 的 CPU 风扇曲线不再生效，温度完全靠我们的曲线 —— 因此曲线的
  高温段必须留足（默认 90℃ 以上直接 100%）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

# 曲线点数与取值范围（与 EC 出厂曲线同构：8 点 = 8 个温度 + 8 个转速）
POINTS = 8
TEMP_MIN, TEMP_MAX = 30, 100
FAN_MIN_HARD, FAN_MAX = 5, 100

# 写入门槛：目标与上次写入差 3% 才动风扇，避免每拍都写 EC（噪声 + 磨损）
HYST = 3
# 连续多少次「写了但 tag 不是手动模式」判通道失效
FAIL_LIMIT = 3
# 看门狗：心跳超过这么多秒没刷新就自动释放
BEAT_TIMEOUT_S = 25.0

# 出厂默认曲线（读不到 EC 曲线时的兜底：宁大勿小，高温段给满）
FALLBACK_TEMPS = [35, 45, 55, 65, 75, 82, 90, 98]
FALLBACK_FANS = [30, 30, 38, 50, 65, 80, 92, 100]

FAN_DEV = "cpu_fan"


def interp(temps: List[int], fans: List[int], t: float) -> float:
    """线性插值：低于首点取首值，高于末点取末值。"""
    if not temps:
        return 0.0
    if t <= temps[0]:
        return float(fans[0])
    if t >= temps[-1]:
        return float(fans[-1])
    for i in range(len(temps) - 1):
        if temps[i] <= t <= temps[i + 1]:
            span = float(temps[i + 1] - temps[i])
            if span <= 0:
                return float(fans[i + 1])
            return fans[i] + (fans[i + 1] - fans[i]) * (t - temps[i]) / span
    return float(fans[-1])


def validate(temps, fans, min_pct: int = 30) -> Tuple[bool, str]:
    """校验一条曲线。返回 (ok, 说明)。宁严勿松：坏曲线会被写进风扇。"""
    try:
        ts = [int(x) for x in temps]
        fs = [int(x) for x in fans]
    except (TypeError, ValueError):
        return False, "曲线里有非数字"
    if len(ts) != POINTS or len(fs) != POINTS:
        return False, "需要 %d 个温度点和 %d 个转速点" % (POINTS, POINTS)
    if any(t < TEMP_MIN or t > TEMP_MAX for t in ts):
        return False, "温度点须在 %d~%d℃" % (TEMP_MIN, TEMP_MAX)
    if any(b <= a for a, b in zip(ts, ts[1:])):
        return False, "温度点必须严格递增"
    if any(f < FAN_MIN_HARD or f > FAN_MAX for f in fs):
        return False, "转速须在 %d~%d%%" % (FAN_MIN_HARD, FAN_MAX)
    if any(b < a for a, b in zip(fs, fs[1:])):
        return False, "转速不能随温度下降"
    if min(fs) < min_pct:
        return False, "最低点低于安全下限 %d%%" % min_pct
    return True, ""


class FanCurve:
    """软件风扇曲线：本地曲线 + 每拍插值写 cpu_fan + 三道自动释放保险。"""

    def __init__(self, cfg: Optional[Dict] = None, data_path: Optional[str] = None,
                 atk=None, clock=time.time, watchdog: bool = False) -> None:
        self.cfg = cfg if cfg is not None else {}
        self.data_path = data_path
        self._atk = atk                    # 测试注入
        self._clock = clock
        self.enabled = bool(self.cfg.get("fan_curve_enabled", False))
        try:
            self.min_pct = int(self.cfg.get("fan_curve_min_pct", 30))
        except (TypeError, ValueError):
            self.min_pct = 30
        self.min_pct = max(10, min(90, self.min_pct))
        self.factory: Dict[str, List[int]] = {"temps": list(FALLBACK_TEMPS),
                                              "fans": list(FALLBACK_FANS)}
        self.curve: Dict[str, List[int]] = {"temps": list(FALLBACK_TEMPS),
                                            "fans": list(FALLBACK_FANS)}
        self._manual = False               # 当前是否由我们接管（EC tag=2）
        self._last_pct: Optional[int] = None
        self._fails = 0
        self._beat = 0.0
        self._note = ""
        self._wd_stop = False
        self._wd_thread: Optional[threading.Thread] = None
        self._use_watchdog = watchdog
        self._load()
        self.startup_cleanup()

    # ------------------------------------------------------------ 通道
    def _dev(self):
        if self._atk is not None:
            return self._atk
        try:
            from . import atkacpi
            return atkacpi.get()
        except Exception:
            return None

    def _read_fan(self) -> Tuple[Optional[int], Optional[int]]:
        """读 cpu_fan 原始返回：(数值, tag)。tag=1 自动 / tag=2 手动 / None 读不到。"""
        a = self._dev()
        if a is None:
            return None, None
        try:
            buf = a.read_buf(FAN_DEV, 0, 16)
            if not buf or len(buf) < 4:
                return None, None
            return (int.from_bytes(buf[0:2], "little"),
                    int.from_bytes(buf[2:4], "little"))
        except Exception:
            return None, None

    def _write_fan(self, pct: int) -> bool:
        a = self._dev()
        if a is None:
            self._note = "ATK 通道不可用"
            return False
        try:
            return bool(a.write_int(FAN_DEV, int(pct)))
        except Exception as e:                    # noqa: BLE001
            self._note = "写风扇失败：%r" % e
            return False

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        if not self.data_path or not os.path.isfile(self.data_path):
            return
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            c = d.get("curve") or {}
            ok, _ = validate(c.get("temps"), c.get("fans"), self.min_pct)
            if ok:
                self.curve = {"temps": [int(x) for x in c["temps"]],
                              "fans": [int(x) for x in c["fans"]]}
            fct = d.get("factory") or {}
            ok2, _ = validate(fct.get("temps"), fct.get("fans"), 0)
            if ok2:
                self.factory = {"temps": [int(x) for x in fct["temps"]],
                                "fans": [int(x) for x in fct["fans"]]}
        except Exception:
            pass

    def _save(self) -> None:
        if not self.data_path:
            return
        try:
            tmp = self.data_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"curve": self.curve, "factory": self.factory},
                          f, ensure_ascii=False)
            os.replace(tmp, self.data_path)
        except Exception:
            pass

    def _read_factory(self) -> None:
        """读 EC 出厂平衡曲线，作为「恢复原厂」的基准。读不到就保留兜底值。"""
        a = self._dev()
        if a is None:
            return
        try:
            from . import atkacpi
            raw = a.get_fan_curve(atkacpi.FAN_CPU, atkacpi.CURVE_BALANCED)
            if raw and len(raw) >= 16:
                ts = list(raw[:8])
                fs = list(raw[8:16])
                ok, _ = validate(ts, fs, 0)
                if ok:
                    self.factory = {"temps": ts, "fans": fs}
        except Exception:
            pass

    # ------------------------------------------------------------ 生命周期
    def startup_cleanup(self) -> bool:
        """上次会话若被强杀，风扇可能锁在手动模式 —— 开机先交回自动。"""
        val, tag = self._read_fan()
        if tag == 2:
            self._write_fan(0)
            time.sleep(0.15)
            v2, tag2 = self._read_fan()
            self._note = ("上次退出时风扇停在手动模式（%s%%），已交回 EC 自动"
                          % (val if val is not None else "?"))
            return tag2 == 1
        return False

    def release(self, reason: str = "") -> dict:
        """交回 EC 自动（写 0），并停看门狗。"""
        ok = self._write_fan(0)
        v, tag = self._read_fan()
        self._manual = False
        self._last_pct = None
        self._fails = 0
        self._stop_watchdog()
        if reason:
            self._note = reason
        elif tag == 1:
            self._note = "已交回 EC 自动（当前 %s%%）" % v if v is not None else "已交回 EC 自动"
        return {"ok": ok, "tag": tag, "fan": v}

    def stop(self) -> None:
        self.release("程序退出：风扇交回自动")

    # ------------------------------------------------------------ 看门狗
    def _start_watchdog(self) -> None:
        if not self._use_watchdog or (self._wd_thread and self._wd_thread.is_alive()):
            return
        self._wd_stop = False
        self._beat = self._clock()

        def loop():
            while not self._wd_stop:
                time.sleep(5.0)
                if self._wd_stop:
                    return
                if self._manual and (self._clock() - self._beat) > BEAT_TIMEOUT_S:
                    try:
                        self.release("看门狗：巡检心跳中断，风扇交回自动")
                    except Exception:
                        pass
                    return
        self._wd_thread = threading.Thread(target=loop, name="fan-curve-wd", daemon=True)
        self._wd_thread.start()

    def _stop_watchdog(self) -> None:
        self._wd_stop = True

    # ------------------------------------------------------------ 控制
    def tick(self, cpu_temp: Optional[float] = None) -> dict:
        """每拍调用：按温度算目标转速并写 EC。返回本拍动作，便于测试。"""
        self._beat = self._clock()
        if not self.enabled:
            if self._manual:
                self.release("曲线已停用：风扇交回 EC 自动")
            return {"action": "off", "manual": self._manual}
        a = self._dev()
        if a is None:
            self._note = "ATK 通道不可用，无法接管风扇"
            return {"action": "nochannel"}

        t = cpu_temp
        if t is None:
            try:
                t = a.cpu_temp()
            except Exception:
                t = None
        if t is None:
            return {"action": "notemp"}

        target = int(round(interp(self.curve["temps"], self.curve["fans"], float(t))))
        target = max(self.min_pct, min(FAN_MAX, target))
        if self._manual and self._last_pct is not None and abs(target - self._last_pct) < HYST:
            return {"action": "hold", "target": target, "temp": t}

        ok = self._write_fan(target)
        _v, tag = self._read_fan()
        if ok and tag == 2:
            self._manual = True
            self._last_pct = target
            self._fails = 0
            self._note = "接管中：%d℃ → %d%%" % (int(t), target)
            self._start_watchdog()
            return {"action": "write", "target": target, "temp": t, "tag": tag}

        self._fails += 1
        if self._fails >= FAIL_LIMIT:
            self.enabled = False
            self._manual = False
            self._stop_watchdog()
            self._note = ("写入后回读不是手动模式（tag=%s），该机型风扇不可接管，"
                          "已自动停用" % tag)
            return {"action": "fail", "tag": tag, "note": self._note}
        return {"action": "retry", "tag": tag, "target": target}

    # ------------------------------------------------------------ 配置
    def set_curve(self, temps=None, fans=None) -> dict:
        if temps is not None or fans is not None:
            ok, msg = validate(temps or [], fans or [], self.min_pct)
            if not ok:
                return {"ok": False, "msg": msg}
            self.curve = {"temps": [int(x) for x in temps],
                          "fans": [int(x) for x in fans]}
            self._save()
        return {"ok": True, "msg": "曲线已保存", "curve": self.curve}

    def set_enabled(self, on: bool) -> dict:
        self.enabled = bool(on)
        self.cfg["fan_curve_enabled"] = self.enabled
        if not self.enabled:
            r = self.release("已停用：风扇交回 EC 自动")
            return {"enabled": False, "msg": "已停用", "ok": True, "rel": r}
        self._read_factory()
        return {"ok": True, "enabled": True, "msg": "已启用，下一拍开始接管"}

    def reset_curve(self) -> dict:
        self._read_factory()
        self.curve = {"temps": list(self.factory["temps"]),
                      "fans": list(self.factory["fans"])}
        self._save()
        return {"ok": True, "curve": self.curve,
                "msg": "已恢复出厂曲线（下一拍生效）"}

    def state(self) -> dict:
        val, tag = self._read_fan()
        a = self._dev()
        temp = None
        if a is not None:
            try:
                temp = a.cpu_temp()
            except Exception:
                temp = None
        return {
            "available": a is not None,
            "supported": tag is not None,
            "enabled": self.enabled,
            "manual": bool(self._manual),
            "min_pct": self.min_pct,
            "curve": dict(self.curve),
            "factory": dict(self.factory),
            "fan_now": val,
            "fan_tag": tag,
            "cpu_temp": temp,
            "last_pct": self._last_pct,
            "note": self._note,
            "gpu_note": "GPU 风扇仍由 EC 自动控制（通道回读不稳，本版不接管）",
        }
