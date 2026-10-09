# -*- coding: utf-8 -*-
"""lp/gpueco.py 单元测试：假 ATKACPI 通道注入，验证自动化/校验/还原逻辑。"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
time.sleep = lambda s: None        # 测试不打真瞌睡（gpueco 校验循环有 0.6s 等待）
from lp.gpueco import GpuEco

PASS = 0


def ok(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1
    print("  ok -", msg)


class FakeAtk:
    """假 ATKACPI：eco 值存内存，可配置写失败/写不生效。"""

    def __init__(self):
        self.ok = True
        self.eco = 0
        self.fail_write = False
        self.stick = False          # True = 写了也不变（服务托管模拟）
        self.writes = 0
        self.M_DSTS = 0x53545344
        self.M_DEVS = 0x53564544

    def _call(self, method, args, out_size=16):
        import struct
        dev, val = struct.unpack("<II", args)
        if method == 0x53545344:    # DSTS 读
            return struct.pack("<I", 0x00010000 | (self.eco & 0xFFFF)) + b"\x00" * 12
        if method == 0x53564544:    # DEVS 写
            self.writes += 1
            if self.fail_write:
                return None          # 硬错误：设备无响应
            if self.eco != val and not self.stick:
                self.eco = val
                return struct.pack("<i", 1)   # 真翻转 → ret=1
            return struct.pack("<i", 0)       # 已达成/不生效 → ret=0（no-op）
        return None


def test_basic():
    print("== 基本读写 ==")
    f = FakeAtk()
    g = GpuEco(atk=f)
    g.enabled = True
    ok(g.read() == 0, "初始 eco=0")
    ok(g.set(1) is True, "set(1) 成功")
    ok(f.eco == 1, "底层 eco=1")
    ok(g.read() == 1, "回读=1")
    # 幂等：再 set(1) 不应产生新写入
    n = f.writes
    g.set(1)
    ok(f.writes == n, "幂等：已到位不再写")
    # set(0) 还原
    ok(g.set(0) is True, "set(0) 成功")
    ok(f.eco == 0, "底层 eco=0")


def test_tick_auto():
    print("== tick 自动化 ==")
    f = FakeAtk()
    g = GpuEco(atk=f)
    g.enabled = True
    g.tick(True, "office")           # 插电
    ok(f.eco == 0 and g._applied == 0, "插电 → 独显保持可用")
    g.tick(False, "office")          # 离电办公
    ok(f.eco == 1, "离电 → 独显断电")
    g.tick(False, "battery")
    ok(f.eco == 1, "离电续航档 → 保持断电")
    # 离电 + 游戏档 → 还原
    g.tick(False, "gaming")
    ok(f.eco == 0, "离电 + 游戏档 → 不断电")
    g.tick(False, "gaming")          # 幂等
    n = f.writes
    g.tick(False, "gaming")
    ok(f.writes == n, "游戏档幂等零写入")
    # 回到插电
    g.tick(True, "gaming")
    ok(f.eco == 0, "插电还原")


def test_disabled_restore():
    print("== 关闭开关自动还原 ==")
    f = FakeAtk()
    g = GpuEco(atk=f)
    g.enabled = True
    g.tick(False, "office")
    ok(f.eco == 1, "先离电断电")
    g.enabled = False
    g.tick(False, "office")
    ok(f.eco == 0, "功能关闭 → 自动还原独显")
    # 关闭状态离电不再断电
    n = f.writes
    g.tick(False, "office")
    ok(f.writes == n, "关闭后离电不再写")


def test_balanced_noop():
    print("== 系统平衡档不干预 ==")
    f = FakeAtk()
    g = GpuEco(atk=f)
    g.enabled = True
    g.tick(False, "balanced")
    ok(f.eco == 0 and g._applied is None, "平衡档零写入零应用")
    g.tick(True, "balanced")
    ok(f.writes == 0, "平衡档全程零 DEVS")


def test_stuck_write():
    print("== 写不生效（服务托管模拟）==")
    f = FakeAtk()
    f.stick = True
    g = GpuEco(atk=f)
    g.enabled = True
    ok(g.set(1) is False, "set(1) 回读未确认 → False")
    ok(not g._broken, "未达失败阈值不判死")
    g.set(1)
    ok(not g._broken, "第 2 次 set 仍不判死")
    g.set(1)
    ok(g._broken, "连续 3 轮失败标记失效")
    n = f.writes
    g.tick(False, "office")          # 失效后不再打扰
    ok(f.writes == n, "失效后零写入")


def test_fail_write():
    print("== DEVS 硬错误（设备无响应）==")
    f = FakeAtk()
    f.fail_write = True
    g = GpuEco(atk=f)
    g.enabled = True
    ok(g.set(1) is False, "第 1 轮失败（暂不判死）")
    ok(not g._broken, "单轮失败不标记失效")
    g.set(1)
    g.set(1)
    ok(g._broken, "连续失败达到阈值标记失效")


def test_restore():
    print("== 退出还原 ==")
    f = FakeAtk()
    g = GpuEco(atk=f)
    g.enabled = True
    g.tick(False, "office")
    ok(f.eco == 1, "离电断电")
    ok(g.restore() is True, "restore 成功")
    ok(f.eco == 0, "独显恢复可用")
    ok(g.restore() is False, "没断电时 restore 不动作")


def test_report():
    print("== report ==")
    f = FakeAtk()
    g = GpuEco(atk=f)
    g.enabled = True
    g.tick(False, "office")
    r = g.report()
    ok(r["eco"] == 1 and r["applied_by_us"] == 1 and not r["broken"], "report 字段齐全")
    ok(g.line(False) == "独显已 ACPI 断电", "面板文案")
    ok(g.line(True) == "", "插电文案为空")


if __name__ == "__main__":
    test_basic()
    test_tick_auto()
    test_disabled_restore()
    test_balanced_noop()
    test_stuck_write()
    test_fail_write()
    test_restore()
    test_report()
    print("\n全部通过：%d 项" % PASS)
