# -*- coding: utf-8 -*-
"""功耗分配集成测试：传感器链路 + 控制律 + 真实写入 powercfg 并还原"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lp import config, powercfgctl as pc
from lp.manager import Manager
from lp.alloc import PowerAllocator, _mk

OK = []
BAD = []
SKIP = []

def check(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print("  [%s] %s %s" % ("OK " if cond else "FAIL", name, extra))

def skip(name, why):
    """环境不支持时跳过（不是失败）。

    典型场景：离电时 GPU Eco 已把独显断电，NVML 自然什么都读不到 ——
    这是**程序正确工作的证据**，不该判失败。早年就是在这里误报过。
    """
    SKIP.append(name)
    print("  [SKIP] %s  —— %s" % (name, why))

print("=" * 72)
print("1) 传感器链路")
print("=" * 72)
cfg = config.load()
m = Manager(cfg)
s = m.pmon.sample()
# % Processor Time 等速率计数器前两次采样必为无效值（PDH warmup），预热到位再断言
for _ in range(3):
    s = m.pmon.sample()
    if s["core_max"] is not None:
        break
print("   PDH available:", m.pmon.available, "| NVML ready:", m.nvml.ready)
check("PDH 可用", m.pmon.available)
check("读得到 SoC 功耗", s["soc_w"] is not None, "= %.1f W" % (s["soc_w"] or -1))
check("读得到 CPU 功耗", s["cpu_w"] is not None, "= %.1f W" % (s["cpu_w"] or -1))
check("读得到 CPU 温度", s["cpu_temp"] is not None, "= %.1f ℃" % (s["cpu_temp"] or -1))
check("温度量纲合理(30~105℃)", 30 <= (s["cpu_temp"] or 0) <= 105)
# 独显可能处于「物理离线」状态：离电时 gpu_eco 会 ACPI 级断电（eco=1），
# 此时 NVML 全部读数返回 None 是**预期**，必须跳过而不是判失败。
_gpu_offline = not getattr(m.nvml, "ready", False)
_gpu_offline_why = "NVML 未就绪"
if not _gpu_offline:
    try:
        from lp import gpueco
        if gpueco.GpuEco().read() == 1:
            _gpu_offline, _gpu_offline_why = True, "独显已断电（GPU Eco=1，离电常驻策略）"
    except Exception:
        pass

if _gpu_offline:
    skip("NVML 可读独显温度", _gpu_offline_why)
    skip("NVML 可读独显功耗", _gpu_offline_why)
    skip("NVML 可读独显利用率", _gpu_offline_why)
    skip("NVML 可读降频原因", _gpu_offline_why)
else:
    check("NVML 可读独显温度", s["gpu_temp"] is not None, "= %s ℃" % s["gpu_temp"])
    # 功耗传感器会间歇吐坏值（实测见过 588/590W，TGP 上限才 140W），
    # _sane 会把它钳成 None（宁缺毋滥）。所以断言是「读数可用或被正确钳掉」，
    # 只有"传感器链路完全没读到"才算失败。
    _gpu_w_raw = m.nvml.power_usage() if m.nvml.ready else None
    check("NVML 可读独显功耗", s["gpu_w"] is not None or (_gpu_w_raw or 0) > 140,
          "= %.2f W（原始 %s，坏值被钳制）" % (s["gpu_w"] or -1, _gpu_w_raw))
    check("NVML 可读独显利用率", s["gpu_util"] is not None, "= %s%%" % s["gpu_util"])
    check("NVML 可读降频原因", isinstance(s["gpu_throttle"], list), str(s["gpu_throttle"]))
check("读得到每核峰值", s["core_max"] is not None, "= %.0f%%" % (s["core_max"] or -1))
check("读得到实际频率", s["cpu_mhz"] is not None, "= %.0f MHz" % (s["cpu_mhz"] or -1))
t0 = time.time()
for _ in range(20):
    m.pmon.sample()
print("   20 次采样耗时 %.0f ms（每次 %.1f ms）" % ((time.time() - t0) * 1000, (time.time() - t0) * 50))
check("单次采样 < 20ms", (time.time() - t0) / 20 < 0.02)

print()
print("=" * 72)
print("2) 控制律（离线仿真，不碰电源计划）")
print("=" * 72)
a = PowerAllocator({"alloc_enabled": True, "alloc_cooldown_seconds": 0,
                    "alloc_min_cpu_pct": 45, "alloc_step_pct": 5})
base = 100
# 模拟 GPU 瓶颈 + 让渡真的生效（每次降档 GPU 功耗 +2W）
# 功耗区间按本机实测标定（power_alloc.csv：峰值 68.8W / 均值 59.8W），
# 55W 起步逐步涨到 69W —— 越过 0.97*70 的到顶线后应停止让渡
a._cur = base; a._base = base
steps = []
for i in range(8):
    gpu_w = 55 + 2.0 * i
    snap = _mk(gpu_util=99, core_max=60, cpu_util=45, gpu_w=gpu_w,
               soc_w=25 - i, cpu_w=22 - 3 * i, cpu_temp=85, gpu_temp=80)
    a._hist = [snap] * a.HIST_N
    a._probe = None                       # 绕过 8 秒有效性等待（仿真时间压缩）
    r = a.feed(snap, base, active=True, on_ac=True)
    steps.append(a._cur if r is None else r["proc_max"])
print("   连续 GPU 瓶颈下的 CPU 上限轨迹:", steps)
check("GPU 瓶颈会逐步让渡 CPU", steps[0] < base)
check("让渡有下限保护(>=45)", min(steps) >= 45, "min=%d" % min(steps))
check("GPU 到达 100W 后停止让渡", a._cap_reached or steps[-1] == 45)

# CPU 瓶颈：应把上限加回去
a2 = PowerAllocator({"alloc_enabled": True, "alloc_cooldown_seconds": 0,
                     "alloc_min_cpu_pct": 45, "alloc_step_pct": 5})
a2._cur = 60; a2._base = 100; a2._cap_reached = True
snap = _mk(gpu_util=65, core_max=97, cpu_util=60, gpu_w=55, cpu_temp=80, gpu_temp=70)
a2._hist = [snap] * a2.HIST_N
r2 = a2.feed(snap, 100, active=True, on_ac=True)
print("   CPU 瓶颈 -> 建议 proc_max =", r2["proc_max"] if r2 else None, r2["reason"] if r2 else "")
check("CPU 瓶颈会归还 CPU", bool(r2 and r2["proc_max"] > 60))
# 归还到基准档才允许重新让渡（中间档位不解除，否则会在
# 「让渡→判 CPU 瓶颈→归还半档→再让渡」之间每轮振荡）
check("未回到基准档时不解除 cap_reached", a2._cap_reached is True)
a2b = PowerAllocator({"alloc_enabled": True, "alloc_cooldown_seconds": 0,
                      "alloc_min_cpu_pct": 45, "alloc_step_pct": 5})
a2b._cur = 95; a2b._base = 100; a2b._cap_reached = True
a2b._hist = [snap] * a2b.HIST_N
a2b.feed(snap, 100, active=True, on_ac=True)
check("回到基准档后解除 cap_reached", a2b._cap_reached is False)

# 温度越界：无条件降档
a3 = PowerAllocator({"alloc_enabled": True, "alloc_cooldown_seconds": 0,
                     "alloc_step_pct": 5, "cpu_temp_limit_c": 90})
a3._cur = 90; a3._base = 100
snap = _mk(gpu_util=70, core_max=60, cpu_util=50, gpu_w=60, cpu_temp=95, gpu_temp=70)
a3._hist = [snap] * a3.HIST_N
r3 = a3.feed(snap, 100, active=True, on_ac=True)
check("CPU 超温触发保护降档", bool(r3 and r3["action"] == "thermal"), str(r3 and r3["reason"]))

# 未插电 / 非游戏档：不动
a4 = PowerAllocator({"alloc_enabled": True})
snap = _mk(gpu_util=99, core_max=60, gpu_w=80)
a4._hist = [snap] * a4.HIST_N
check("未激活时返回 None", a4.feed(snap, 100, active=False, on_ac=True) is None)
check("未插电时返回 None", a4.feed(snap, 100, active=True, on_ac=False) is None)

print()
print("=" * 72)
print("3) 真实写入 powercfg（改完立刻还原）")
print("=" * 72)
scheme = cfg.get("custom_scheme")
plan = pc.load_plan(scheme)
ps = plan.get("PROCTHROTTLEMAX") or plan.get("bc5038f7-23e0-4960-96da-33abaf5935ec")
orig_ac, orig_dc = ps.current(False), ps.current(True)
print("   原值: AC=%s%%  DC=%s%%" % (orig_ac, orig_dc))
m.current = "gaming"
try:
    okw = m.set_proc_max(87)
    plan2 = pc.load_plan(scheme)
    ps2 = plan2.get("PROCTHROTTLEMAX") or plan2.get("bc5038f7-23e0-4960-96da-33abaf5935ec")
    print("   写入 87%% -> 回读 AC=%s%%  DC=%s%%" % (ps2.current(False), ps2.current(True)))
    check("能写入 PROCTHROTTLEMAX", bool(okw))
    check("回读值正确", ps2.current(False) == 87 and ps2.current(True) == 87)
finally:
    for on_bat, val in ((False, orig_ac), (True, orig_dc)):
        if val is not None:
            pc.set_value(scheme, ps, val, on_bat)
    plan3 = pc.load_plan(scheme)
    ps3 = plan3.get("PROCTHROTTLEMAX") or plan3.get("bc5038f7-23e0-4960-96da-33abaf5935ec")
    print("   已还原: AC=%s%%  DC=%s%%" % (ps3.current(False), ps3.current(True)))
    check("成功还原原值", ps3.current(False) == orig_ac and ps3.current(True) == orig_dc)

print()
print("=" * 72)
print("3.5) 温度墙兜底：节流原因位不可用时不误判")
print("=" * 72)
# 真实事故风险（2026-10-11）：nvmlDeviceGetCurrentClocksThrottleReasons 在
# 部分驱动/显卡上不存在（返回空）。那种机器上「温度墙」永远检测不到，
# 分配器会误判成 gpu_bound，继续把 CPU 的瓦让给已经过热的独显。
_a = PowerAllocator({"gpu_temp_limit_c": 87, "gpu_tgp_max_watts": 70})
def _feed(a, **kw):
    a._hist.clear()
    for _ in range(3):
        a._hist.append(dict(kw))
    return a.classify()
_v = _feed(_a, gpu_util=92.0, gpu_w=60.0, gpu_temp=91.0, gpu_throttle=[],
           core_max=70.0, cpu_util=70.0)
check("温度超上限 + 无节流原因 → gpu_thermal", _v == "gpu_thermal", _v)
_v = _feed(_a, gpu_util=92.0, gpu_w=60.0, gpu_temp=80.0, gpu_throttle=[],
           core_max=70.0, cpu_util=70.0)
check("温度正常 + 无节流原因 → 不误报温度墙", _v == "gpu_bound", _v)
_v = _feed(_a, gpu_util=92.0, gpu_w=60.0, gpu_temp=None, gpu_throttle=[],
           core_max=70.0, cpu_util=70.0)
check("读不到温度时跳过兜底（不硬猜）", _v == "gpu_bound", _v)
_v = _feed(_a, gpu_util=92.0, gpu_w=60.0, gpu_temp=80.0,
           gpu_throttle=["\u6e29\u5ea6\u5899 SW_THERMAL"], core_max=70.0, cpu_util=70.0)
check("节流原因位可用时照旧走原因判据", _v == "gpu_thermal", _v)

print()
print("=" * 72)
print("4) 报告字段")
print("=" * 72)
m.current = None
rep = m.alloc_report()
need = ["verdict", "verdict_cn", "action", "reason", "cpu_w", "gpu_w", "cpu_temp",
        "gpu_temp", "gpu_util", "envelope_w", "gpu_tgp_max", "cpu_cap_pct", "est_gain_pct"]
missing = [k for k in need if k not in rep]
check("报告字段齐全", not missing, "缺: %s" % missing)
st = m.status()
check("status 带 power 字段", "power" in st and "alloc" in st)

print()
print("=" * 72)
print("结果: %d 项通过, %d 项失败%s" % (
    len(OK), len(BAD), ("，%d 项跳过（环境不支持）" % len(SKIP)) if SKIP else ""))
if BAD:
    print("失败项: %s" % BAD)
if SKIP:
    print("跳过项: %s" % SKIP)
print("=" * 72)
