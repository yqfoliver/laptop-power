# -*- coding: utf-8 -*-
"""软件风扇曲线（fancurve）回归测试。

全部走假 ATK 通道（不碰真实风扇），重点验：
插值 / 曲线校验 / 迟滞 / 下限钳制 / 释放 / 启动收尾 / 通道失效自停 / 持久化。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lp import fancurve  # noqa: E402
from lp.fancurve import FanCurve, interp, validate  # noqa: E402

OK = 0
FAIL = []


def ck(name, cond, extra=""):
    global OK
    if cond:
        OK += 1
    else:
        FAIL.append(name + ((" | " + str(extra)) if extra else ""))


class FakeAtk:
    """假 ATK：tag=1 自动 / tag=2 手动（与真机行为一致）。"""

    def __init__(self, temp=50.0, tag=1, value=33, refuse_manual=False):
        self.temp = temp
        self.tag = tag
        self.value = value
        self.refuse_manual = refuse_manual
        self.writes = []

    def cpu_temp(self):
        return self.temp

    def read_buf(self, dev, status=0, size=16):
        b = bytes([self.value & 0xFF, (self.value >> 8) & 0xFF,
                   self.tag & 0xFF, (self.tag >> 8) & 0xFF])
        return b + b"\x00" * 12

    def write_int(self, dev, v):
        self.writes.append(v)
        if v == 0:
            self.tag = 1
        else:
            self.value = v
            if not self.refuse_manual:
                self.tag = 2
        return True

    def get_fan_curve(self, which=0, mode=0):
        return None


def mk(temp=50.0, enabled=True, **kw):
    tmp = tempfile.mkdtemp(prefix="fancurve_")
    fake = FakeAtk(temp=temp, **kw)
    cfg = {"fan_curve_enabled": enabled, "fan_curve_min_pct": 30}
    fc = FanCurve(cfg=cfg, data_path=os.path.join(tmp, "fan_curve.json"), atk=fake)
    return fc, fake, cfg


def test_interp():
    t = [35, 45, 55, 65]
    f = [30, 30, 40, 60]
    ck("低于首点取首值", interp(t, f, 20) == 30)
    ck("正好首点为 30", interp(t, f, 35) == 30)
    ck("55℃ 精确命中 40", interp(t, f, 55) == 40)
    ck("60℃ 线性插值 50", interp(t, f, 60) == 50)
    ck("高于末点取末值", interp(t, f, 99) == 60)
    ck("空曲线不抛异常", interp([], [], 50) == 0.0)


def test_validate():
    good_t = [35, 45, 55, 65, 75, 82, 90, 98]
    good_f = [30, 30, 38, 50, 65, 80, 92, 100]
    ck("合法曲线通过", validate(good_t, good_f, 30)[0])
    ck("点数不对被拒", not validate(good_t[:7], good_f, 30)[0])
    ck("温度非递增被拒", not validate([35, 45, 45, 65, 75, 82, 90, 98], good_f, 30)[0])
    ck("转速下降被拒", not validate(good_t, [30, 30, 38, 50, 40, 80, 92, 100], 30)[0])
    ck("温度越界被拒", not validate([10, 45, 55, 65, 75, 82, 90, 98], good_f, 30)[0])
    ck("低于安全下限被拒", not validate(good_t, [10, 10, 38, 50, 65, 80, 92, 100], 30)[0])
    ck("非数字被拒", not validate(["a"] * 8, good_f, 30)[0])


def test_tick_writes():
    fc, fake, _ = mk(temp=60.0)
    r = fc.tick()
    ck("启用后第一拍写入", r["action"] == "write", r)
    ck("60℃ 目标 44%", r.get("target") == 44, r)
    ck("真的写了 44", fake.writes == [44], fake.writes)
    ck("写入后进入手动模式", fc._manual is True)
    r2 = fc.tick()
    ck("同温度第二拍不再写（迟滞）", r2["action"] == "hold" and fake.writes == [44], fake.writes)
    fake.temp = 80.0
    r3 = fc.tick()
    ck("温度大涨后重写", r3["action"] == "write" and len(fake.writes) == 2, fake.writes)
    ck("80℃ 目标 76%（75→82 段插值）", r3.get("target") == 76, r3)


def test_min_floor():
    fc, fake, _ = mk(temp=20.0, value=10)
    r = fc.tick()
    ck("低温被下限钳到 30", r.get("target") == 30, r)
    ck("下限确实写进硬件", fake.writes == [30], fake.writes)


def test_disabled_releases():
    fc, fake, cfg = mk(temp=50.0)
    fc.tick()
    ck("先进入接管", fc._manual is True)
    out = fc.set_enabled(False)
    ck("停用返回 ok", out["ok"] is True)
    ck("停用写 0 释放", fake.writes[-1] == 0, fake.writes)
    ck("释放后 tag 回自动", fake.tag == 1)
    ck("配置里已关闭", cfg.get("fan_curve_enabled") is False)
    r = fc.tick()
    ck("停用后 tick 不再写", r["action"] == "off" and fake.writes[-1] == 0, fake.writes)


def test_startup_cleanup():
    tmp = tempfile.mkdtemp(prefix="fancurve_")
    fake = FakeAtk(tag=2, value=64)          # 模拟上次被强杀留下的手动模式
    fc = FanCurve(cfg={"fan_curve_enabled": False},
                  data_path=os.path.join(tmp, "fan_curve.json"), atk=fake)
    ck("启动时发现遗留手动模式并写 0", fake.writes[:1] == [0], fake.writes)
    ck("启动收尾后 tag 回自动", fake.tag == 1)
    ck("启动收尾写入回执说明", "手动模式" in fc.state()["note"], fc.state()["note"])


def test_refuse_manual():
    fc, fake, cfg = mk(temp=60.0, refuse_manual=True)
    for _ in range(fancurve.FAIL_LIMIT):
        fc.tick()
    ck("连续失败后自动停用", fc.enabled is False, fc.enabled)
    ck("停用原因写明回读不符", "不可接管" in fc.state()["note"], fc.state()["note"])
    ck("停用后不再写硬件", len(fake.writes) == fancurve.FAIL_LIMIT, fake.writes)


def test_persist_and_reset():
    fc, fake, _ = mk(temp=50.0)
    nt = [30, 40, 50, 60, 70, 80, 88, 96]
    nf = [32, 34, 40, 50, 62, 76, 90, 100]
    out = fc.set_curve(nt, nf)
    ck("合法曲线保存成功", out["ok"] is True, out)
    fc2 = FanCurve(cfg={"fan_curve_enabled": False},
                   data_path=fc.data_path, atk=FakeAtk())
    ck("新实例读回保存的曲线", fc2.curve["temps"] == nt and fc2.curve["fans"] == nf,
       fc2.curve)
    bad = fc.set_curve([35, 30, 55, 65, 75, 82, 90, 98], nf)
    ck("坏曲线被拒绝且不覆盖", bad["ok"] is False and fc.curve["temps"] == nt, bad)
    fc.reset_curve()
    ck("恢复出厂曲线等于 factory", fc.curve == fc.factory, fc.curve)


def test_state_shape():
    fc, fake, _ = mk(temp=55.0)
    st = fc.state()
    for k in ("available", "supported", "enabled", "manual", "min_pct", "curve",
              "factory", "fan_now", "fan_tag", "cpu_temp", "note", "gpu_note"):
        ck("state 含 %s" % k, k in st)
    ck("state 的 fan_now 是真实读数", st["fan_now"] == 33, st["fan_now"])
    ck("state 明确 GPU 不接管", "EC" in st["gpu_note"], st["gpu_note"])


def main():
    test_interp()
    test_validate()
    test_tick_writes()
    test_min_floor()
    test_disabled_releases()
    test_startup_cleanup()
    test_refuse_manual()
    test_persist_and_reset()
    test_state_shape()
    print("结果: %d 项通过, %d 项失败" % (OK, len(FAIL)))
    for f in FAIL:
        print("  ✘", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
