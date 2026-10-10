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


def main():
    test_errlog()
    test_gpueco_ttl()
    test_learner_disable_ttl()
    test_restore_keeps_original()


if __name__ == "__main__":
    main()
    print()
    print("%d 项断言，失败 %d" % (N[0], len(FAILS)))
    if FAILS:
        print("失败项：%s" % FAILS)
        sys.exit(1)
    print("全部通过")
