# -*- coding: utf-8 -*-
"""健壮性回归：专治「一次异常就让功能永久失效/永久误判」这类病。

这一组用例的共同点：程序对外完全静默（不弹窗不报错），所以下面每一种失效
在用户侧都是**无声的功能消失**——只能靠测试把它们钉住。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import errlog, gpueco, learner  # noqa: E402
from lp.gpupick import GpuPick  # noqa: E402

N = [0]
FAILS = []


def ok(cond, msg, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(msg)
        print("  FAIL: %s  %s" % (msg, extra))
    return cond


# ------------------------------------------------------------ 1. 错误日志
def test_errlog():
    print("== 1. 被吞掉的异常要留痕（去重 + 限长） ==")
    d = tempfile.mkdtemp()
    errlog.set_path(os.path.join(d, "error.log"))
    errlog.set_enabled(True)
    for _ in range(20):
        errlog.log("loop", RuntimeError("boom"))
    txt = errlog.tail(200)
    ok(txt.count("boom") <= 3, "同样错误 5 分钟内不刷屏", txt.count("boom"))
    ok("loop" in txt and "RuntimeError" in txt, "位置与类型都记下来", txt[:80])
    errlog.log("pd", ValueError("其他错误"))
    ok("pd" in errlog.tail(200), "不同错误分开记")
    # 限长：塞很多不同错误后文件不应无限增长
    for i in range(300):
        errlog.log("x%d" % i, RuntimeError("e%d" % i))
    size = os.path.getsize(errlog._file())
    ok(size < 400 * 1024, "文件不会无限增长", "%d B" % size)
    errlog.set_enabled(False)
    before = os.path.getsize(errlog._file())
    errlog.log("off", RuntimeError("不写"))
    ok(os.path.getsize(errlog._file()) == before, "关闭后不再落盘")
    errlog.set_enabled(True)


# ------------------------------------------------------- 2. 通道判死保质期
def test_gpueco_ttl():
    print("== 2. 独显断电通道：判死要有保质期 ==")
    g = gpueco.GpuEco.__new__(gpueco.GpuEco)
    g.enabled = True
    g._applied = None
    g._broken = True
    g._broken_at = time.time()          # 刚判死
    g._fails = 3
    g._last_note = ""
    g.atk = None
    g.set(1)
    ok(g._broken is True, "保质期内不再打扰")
    g._broken_at = time.time() - gpueco.BROKEN_TTL_S - 1   # 过期
    g.read = lambda: 1                   # 假通道：读什么都说已到位
    g.set(1)
    ok(g._broken is False, "过期后自动再给一次机会")
    ok(g._fails == 0, "重试成功即清零失败计数", g._fails)


# ------------------------------------------------------- 3. 旋钮停用保质期
def test_learner_disable_ttl():
    print("== 3. 自学习旋钮：停用 3 天后自动解禁 ==")
    p = os.path.join(tempfile.mkdtemp(), "learn.json")
    L = learner.Learner(path=p)
    for _ in range(learner.FAIL_LIMIT):
        L.note_fail("battery", "proc_max")
    ok(L.is_disabled("battery", "proc_max") is True, "连续失败后停用")
    ok(L.is_disabled("battery", "bright") is False, "没失败的旋钮不受牵连")
    L.data["disabled"]["battery"]["proc_max"] = \
        time.time() - learner.DISABLE_TTL_S - 1
    ok(L.is_disabled("battery", "proc_max") is False, "到期自动解禁")
    ok(not (L.data["disabled"].get("battery") or {}).get("proc_max"),
       "解禁时把记录清掉，不留残影")


# --------------------------------------------------- 4. 还原失败不留残局
def test_restore_keeps_original():
    print("== 4. 还原失败必须留住原值（否则永远还原不回去） ==")
    gp = GpuPick.__new__(GpuPick)
    gp.path = os.path.join(tempfile.mkdtemp(), "gpupick.json")
    gp.data = {"orig": {r"C:\g\game.exe": "GpuPreference=2;"}, "user": {},
               "probe": None}
    gp.reg = None
    gp._last_note = ""

    def boom(*a, **k):
        raise OSError("模拟写注册表失败")

    gp._key = boom                       # 让还原必失败
    gp.reg_restore(r"C:\g\game.exe")
    ok(r"C:\g\game.exe" in gp.data["orig"],
       "失败时保留原值，下次还能再试", gp.data["orig"])
    gp._key = lambda w=True: {}          # 这次能写（用假注册表对象接住写入）
    gp.reg = {}
    gp.reg_restore(r"C:\g\game.exe")
    ok(r"C:\g\game.exe" not in gp.data["orig"], "成功后才抹掉原值")


# ------------------------------------------- 4. 配置值护栏（防静默失效）
def test_config_sanitize():
    print("== 5. 坏配置不许把巡检线程带走 ==")
    from lp import config as C

    c = dict(C.DEFAULT_CONFIG)
    c.update({"poll_seconds": "abc", "ac_settle_s": None, "auto_mode": "yes",
              "dc_brightness_cap": 999, "panel_font_scale": -1,
              "pd_cpu_min": 1, "idle_dim_after_s": 0})
    fixed = C.sanitize(c)
    ok(c["poll_seconds"] == C.DEFAULT_CONFIG["poll_seconds"],
       "字符串 poll_seconds 回默认", c["poll_seconds"])
    ok(isinstance(c["ac_settle_s"], float), "None 的 settle 回默认", c["ac_settle_s"])
    ok(c["auto_mode"] is True, "字符串 \"yes\" 不当真（bool 只认 bool）", c["auto_mode"])
    ok(c["dc_brightness_cap"] == 100, "越界亮度夹到 100", c["dc_brightness_cap"])
    ok(c["panel_font_scale"] >= 0.5, "越界字体比例被夹住", c["panel_font_scale"])
    ok(c["pd_cpu_min"] >= 10, "PD 的 CPU 地板不许低于 10%", c["pd_cpu_min"])
    ok(c["idle_dim_after_s"] >= 5, "空闲调暗延时不许为 0（否则一离电就黑屏）",
       c["idle_dim_after_s"])
    ok({"poll_seconds", "ac_settle_s", "auto_mode"} <= set(fixed),
       "返回被修正的键名清单", sorted(fixed))
    ok(C.sanitize(c) == [], "幂等：第二次不再改动")

    # 端到端：load() 读到坏文件也要活着出来
    p = os.path.join(tempfile.mkdtemp(), "config.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"poll_seconds": "abc", "auto_mode": "yes", "dc_brightness_cap": 5000}')
    old = C.CONFIG_PATH
    try:
        C.CONFIG_PATH = p
        c2 = C.load()
    finally:
        C.CONFIG_PATH = old
    ok(c2["poll_seconds"] == C.DEFAULT_CONFIG["poll_seconds"],
       "load() 把坏值纠回默认，不抛异常", c2["poll_seconds"])
    ok(c2["dc_brightness_cap"] == 100, "load() 也夹越界值", c2["dc_brightness_cap"])


# ------------------------------------------- 5. 两个写入方共用一个旋钮
def test_alloc_cur_sync():
    print("== 6. PD 改写 CPU 上限后，分配器必须同步记账 ==")
    from lp.alloc import PowerAllocator

    a = PowerAllocator({"alloc_enabled": True, "alloc_cooldown_seconds": 0})
    a._base = 100
    a._cur = 60
    a._probe = {"done": False, "at": 0, "gpu_w": 50.0, "cpu_w": 20.0,
                "proc_max_before": 65}
    a.sync_cur(100)
    ok(a._cur == 100, "外部把上限放开回 100% 后 _cur 同步", a._cur)
    ok(a._probe is None, "让渡验证的对照组作废（否则误判「让渡无效」并回滚）")
    a._cur = 55
    a._cap_reached = True
    a.write_failed()
    ok(a._cur is None and a._cap_reached is False,
       "写失败撤销记账，下一轮从计划真值重新起步")

    b = PowerAllocator({"alloc_enabled": True})
    b.sync_cur(70)
    ok(b._cur is None, "还没进入分配状态时不凭空造基准")


# ------------------------------------------- 6. 日志裁切要受字节预算约束
def test_logtrim_byte_bound():
    print("== 7. 日志裁切受字节预算约束（否则每次写都全量重写） ==")
    from lp import logtrim

    p = os.path.join(tempfile.mkdtemp(), "big.log")
    line = "x" * 4000
    for i in range(200):                 # 200 行 × 4KB ≈ 800KB
        logtrim.log_line(p, "%d %s" % (i, line), keep_lines=2000,
                         prob=0.0, max_bytes=200 * 1024)
    size = os.path.getsize(p)
    ok(size < 200 * 1024, "超体积必裁，且裁到字节预算内（只按行数裁会一直超）",
       "%d B" % size)
    with open(p, encoding="utf-8") as f:
        head = f.readline()
    ok("略去较早内容" in head, "留一行裁切标记，读者知道前面被截过")


# ------------------------------------------- 7. 合盖模式的原值守卫
def test_clamshell_origin_guard():
    print("== 8. 读不到合盖原值：不许「假装已保存」后写坏 LIDACTION ==")
    from lp import clamshell as CL

    class FakePP:
        LID_LABEL = {}

        def __init__(self, vals):
            self.vals = vals
            self.writes = []

        def available(self):
            return True

        def lid_action(self, scheme, on_bat):
            return self.vals.get("dc" if on_bat else "ac")

        def set_lid_action(self, scheme, val, on_bat):
            self.writes.append((val, on_bat))
            return True

        def set_active(self, scheme):
            return True

    old = CL.pp
    try:
        fake = FakePP({"ac": None, "dc": None})
        CL.pp = fake
        c = CL.ClamshellCtl({"clamshell_enabled": True, "custom_scheme": "G"}, "G")
        c.monitor_count = lambda: 2          # 外接显示器
        c.tick(True, True)
        ok(fake.writes == [], "读不到原值就不写（无原值不许接管）", fake.writes)
        ok(c._saved is None, "_saved 保持 None，而不是空 dict", c._saved)
        c.tick(True, True)
        ok(fake.writes == [],
           "第二轮也不许写（旧实现此处会写 0，且 reset 再也还原不了）", fake.writes)
        ok(c.engaged is False, "未进入蛤壳模式")

        # 正常路径：读到原值 -> 接管 -> 还原
        fake2 = FakePP({"ac": 1, "dc": 1})
        CL.pp = fake2
        c2 = CL.ClamshellCtl({"clamshell_enabled": True, "custom_scheme": "G"}, "G")
        c2.monitor_count = lambda: 2
        c2.tick(True, True)
        ok(c2.engaged is True and len(fake2.writes) == 2, "有原值才接管", fake2.writes)
        c2.reset(reason="test")
        ok(fake2.writes[-2:] == [(1, False), (1, True)], "还原回原值", fake2.writes[-2:])
    finally:
        CL.pp = old


def main():
    test_errlog()
    test_gpueco_ttl()
    test_learner_disable_ttl()
    test_restore_keeps_original()
    test_config_sanitize()
    test_alloc_cur_sync()
    test_logtrim_byte_bound()
    test_clamshell_origin_guard()


if __name__ == "__main__":
    main()
    print()
    print("%d 项断言，失败 %d" % (N[0], len(FAILS)))
    if FAILS:
        print("失败项：%s" % FAILS)
        sys.exit(1)
    print("全部通过")
