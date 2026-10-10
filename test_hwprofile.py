# -*- coding: utf-8 -*-
"""硬件自适应（hwprofile）回归测试 —— 假通道，不碰真硬件、不改注册表

核心要守的一条线：**源码里不能带某一台机器的标定值**。
所以默认值必须是通用兜底（独显上限 60W / 独显温度 87℃ / 降刷 None），
真值一律由目标机现测 + 实测峰值学习得到，且**用户手设值永远优先**。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import hwprofile as H          # noqa: E402

FAILS = []
N = [0]


def ok(cond, msg, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append("%s%s" % (msg, ("  <- %s" % extra) if extra else ""))
        print("  FAIL: %s %s" % (msg, extra))
    return bool(cond)


def tmp_json():
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(p)
    return p


# ---------------------------------------------------------------- 假通道
class FakeNvml:
    def __init__(self, ready=True, names=("NVIDIA GeForce RTX 4060 Laptop GPU",),
                 tgp_range=(None, 100000), slowdown=91):
        self.ready = ready
        self.gpus = [type("G", (), {"name": n, "index": i, "handle": None})
                     for i, n in enumerate(names)]
        self._tgp = tgp_range
        self._slow = slowdown

    def power_limit_range(self):
        return self._tgp

    def temp_threshold(self, which=1):
        return self._slow


class FakeDisplay:
    def __init__(self, hz=165, avail=(60, 165)):
        self._hz = hz
        self._avail = list(avail)

    def current_hz(self):
        return self._hz

    def available_hz_list(self, refresh=False):
        return self._avail


# ---------------------------------------------------------------- 1. 刷新率挑选
print("== 1. 降刷档位只挑真实存在的（绝不自造频率 = 绝不黑屏） ==")
ok(H.pick_lower_hz([60, 165], 165) == 60, "165Hz 屏 -> 60")
ok(H.pick_lower_hz([60, 120, 144], 144) == 120, "144Hz 屏 -> 挑低一档 120")
ok(H.pick_lower_hz([60], 60) is None, "只有 60Hz 的屏 -> 不降")
ok(H.pick_lower_hz([], 165) is None, "拿不到列表 -> 不降")
ok(H.pick_lower_hz([60, 165], None) is None, "当前刷新率未知 -> 不降")

# ---------------------------------------------------------------- 2. 独显/核显识别
print("== 2. 独显 / 核显按名称识别 ==")
ok(H._is_dgpu("NVIDIA GeForce RTX 4060 Laptop GPU"), "NVIDIA -> 独显")
ok(H._is_dgpu("AMD Radeon RX 7600S"), "Radeon RX -> 独显")
ok(H._is_dgpu("Intel(R) Arc(TM) A770 Graphics"), "Intel Arc -> 独显")
ok(not H._is_dgpu("AMD Radeon(TM) 890M Graphics"), "890M -> 不是独显")
ok(not H._is_dgpu("Intel(R) Iris(R) Xe Graphics"), "Iris Xe -> 不是独显")
ok(H._is_igpu("AMD Radeon(TM) 890M Graphics"), "890M -> 核显")
ok(H._is_igpu("Intel(R) UHD Graphics 620"), "UHD -> 核显")

# ---------------------------------------------------------------- 3. 探测
print("== 3. 探测结果（假 NVML / 假显示 / ATKACPI 注入） ==")
p = H.probe(nvml=FakeNvml(), display=FakeDisplay(), atkacpi_ok=True)
ok(p["has_dgpu"] is True, "识别出独显")
ok("4060" in p["dgpu_name"], "独显型号进档案", p["dgpu_name"])
ok(p["nvml_tgp_max_w"] == 100.0, "NVML 功耗上限约束 100W", p["nvml_tgp_max_w"])
ok(p["nvml_gpu_temp_slowdown_c"] == 91, "NVML slowdown 阈值 91℃")
ok(p["current_hz"] == 165 and p["available_hz"] == [60, 165], "刷新率进档案")
ok(p["atkacpi_ok"] is True, "厂商通道标记")
ok(any("NVML" in s or "名称" in s for s in p["sources"]), "记录了取值来源")

p2 = H.probe(nvml=FakeNvml(ready=False, names=()), display=FakeDisplay(hz=60, avail=[60]),
             atkacpi_ok=False, gpu_names=["AMD Radeon(TM) 890M Graphics"])
ok(not p2["has_dgpu"], "纯核显机：没有独显")
ok(p2["igpu_name"].startswith("AMD"), "核显名进档案", p2["igpu_name"])
ok(p2["atkacpi_ok"] is False, "非华硕机：厂商通道不可用")

# ---------------------------------------------------------------- 4. 自动位填充
print("== 4. apply 只填自动位（None），用户手设值不动 ==")
hp = H.HwProfile(path=tmp_json(), prof=p)
cfg = {"gpu_tgp_max_watts": None, "gpu_temp_limit_c": None,
       "power_envelope_watts": None, "dc_refresh_hz": None,
       "pd_low_hz_value": None, "alloc_enabled": True,
       "gpu_eco_auto": True, "gpupick_enabled": True}
filled = hp.apply(cfg)
ok(cfg["gpu_tgp_max_watts"] == 60.0, "没学到峰值 -> 通用兜底 60W", cfg["gpu_tgp_max_watts"])
ok(cfg["gpu_temp_limit_c"] == 87.0, "独显温度 = slowdown 91 - 4 = 87", cfg["gpu_temp_limit_c"])
ok(cfg["dc_refresh_hz"] == 60 and cfg["pd_low_hz_value"] == 60, "降刷自动挑 60")
ok("gpu_tgp_max_watts" in filled and "dc_refresh_hz" in filled, "返回被填的键名")

cfg2 = {"gpu_tgp_max_watts": 45.0, "dc_refresh_hz": 0, "gpu_temp_limit_c": 80.0,
        "power_envelope_watts": None, "pd_low_hz_value": None}
hp.apply(cfg2)
ok(cfg2["gpu_tgp_max_watts"] == 45.0, "用户手设的独显上限不被覆盖")
ok(cfg2["dc_refresh_hz"] == 0, "用户手设 0（不改）不被覆盖")
ok(cfg2["gpu_temp_limit_c"] == 80.0, "用户手设温度线不被覆盖")
ok(cfg2["power_envelope_watts"] == 100.0, "没手设的包络仍自动填")

print("== 4b. 自动值能随学习更新（不会被 config 冻结） ==")
hp_b = H.HwProfile(path=tmp_json(), prof=p)
cfg_b = {"gpu_tgp_max_watts": None}
hp_b.apply(cfg_b)
ok(cfg_b["gpu_tgp_max_watts"] == 60.0, "第一轮：填兜底 60", cfg_b["gpu_tgp_max_watts"])
hp_b.learn_gpu_peak(75.0)            # 后来在游戏里学到了更高的峰值
hp_b.apply(cfg_b)
ok(abs(cfg_b["gpu_tgp_max_watts"] - 77.25) < 0.06,
   "第二轮：自动值跟着抬到 ~77.3，而不是被 60 冻住", cfg_b["gpu_tgp_max_watts"])
cfg_b["gpu_tgp_max_watts"] = 50.0    # 用户手设
hp_b.apply(cfg_b)
ok(cfg_b["gpu_tgp_max_watts"] == 50.0, "用户手设后不再被自动值覆盖")

# ---------------------------------------------------------------- 5. 降级
print("== 5. 没有独显 / 没有厂商通道时自动关掉对应功能 ==")
cfg3 = {"gpu_tgp_max_watts": None, "gpu_temp_limit_c": None,
        "power_envelope_watts": None, "dc_refresh_hz": None,
        "pd_low_hz_value": None, "alloc_enabled": True,
        "gpu_eco_auto": True, "gpupick_enabled": True}
H.HwProfile(path=tmp_json(), prof=p2).apply(cfg3)
ok(cfg3["alloc_enabled"] is False, "纯核显机：功耗分配器无从谈起")
ok(cfg3["gpu_eco_auto"] is False, "纯核显机：独显断电无从谈起")
ok(cfg3["gpupick_enabled"] is False, "纯核显机：核显优先无从谈起")

hp4 = H.HwProfile(path=tmp_json(), prof=dict(p, atkacpi_ok=False))
cfg4 = {"gpu_eco_auto": None}
hp4.apply(cfg4)
ok(cfg4["gpu_eco_auto"] is False, "厂商通道不可用 -> 不每轮白试 GPU Eco")

# ---------------------------------------------------------------- 6. 峰值学习
print("== 6. 独显峰值学习（只升不降 + 坏值过滤） ==")
hp5 = H.HwProfile(path=tmp_json(), prof=p)
ok(hp5.gpu_peak_w is None, "初始没有峰值")
hp5.learn_gpu_peak(68.8)
ok(abs(hp5.gpu_peak_w - 68.8) < 0.05, "学到 68.8W", hp5.gpu_peak_w)
hp5.learn_gpu_peak(30.0)
ok(abs(hp5.gpu_peak_w - 68.8) < 0.05, "更低的采样不会把峰值拉下来")
hp5.learn_gpu_peak(588.0)
ok(abs(hp5.gpu_peak_w - 68.8) < 0.05, "传感器坏值 588W 被拒")
hp5.learn_gpu_peak(7.0)
ok(abs(hp5.gpu_peak_w - 68.8) < 0.05, "空载 7W 不算峰值（否则到顶判据会低到离谱）")
hp5.learn_gpu_peak(None)
ok(abs(hp5.gpu_peak_w - 68.8) < 0.05, "None 不参与学习")

print("== 7. 让渡停止点：兜底 60W -> 学到峰值后抬到真值 ==")
ok(H.HwProfile(path=tmp_json(), prof=p).gpu_tgp_max() == 60.0, "没学过 -> 兜底 60W")
ok(abs(hp5.gpu_tgp_max() - 70.9) < 0.05, "峰值 68.8 -> 停止点 70.9", hp5.gpu_tgp_max())
ok(hp5.gpu_tgp_max() < 100.0,
   "绝不用 NVML 的 100W 可设上限当判据（那会让到顶判据永远达不到）")

# ---------------------------------------------------------------- 8. 持久化
print("== 8. 档案与学习值落盘 ==")
path = tmp_json()
hp6 = H.HwProfile(path=path, prof=p)
hp6.learn_gpu_peak(72.4)
hp6.save(force=True)
hp7 = H.HwProfile(path=path)
ok(abs((hp7.gpu_peak_w or 0) - 72.4) < 0.05, "峰值跨实例保留", hp7.gpu_peak_w)
ok(not hp7.stale(), "刚写过的档案不算过期")
os.remove(path)
ok(H.HwProfile(path=tmp_json()).stale(), "没有档案 -> 需要现测")

# ---------------------------------------------------------------- 9. 版本/过期
print("== 9. 版本变了要重测 ==")
hp8 = H.HwProfile(path=tmp_json())
hp8.data["profile"] = dict(p, version=-1)
ok(hp8.stale(), "旧版本档案 -> 重测")
hp9 = H.HwProfile(path=tmp_json())
hp9.data["profile"] = dict(p, ts=time.time() - 86400 * 30)
ok(hp9.stale(), "30 天前的档案 -> 重测")

# ---------------------------------------------------------------- 10. 钳制放宽
print("== 10. 功耗读数钳制按本机硬件放宽（别把真值当坏值） ==")
wl = H.HwProfile(path=tmp_json(), prof=p).watt_limits()
ok(wl["gpu_w"] == 140.0, "100W 档独显 -> 钳制 140W", wl["gpu_w"])
wl2 = H.HwProfile(path=tmp_json(),
                  prof=dict(p, nvml_tgp_max_w=175.0)).watt_limits()
ok(wl2["gpu_w"] > 140.0, "175W 档独显 -> 钳制放宽，真值不会被丢", wl2["gpu_w"])
wl3 = H.HwProfile(path=tmp_json(), prof=dict(p, cpu_cores=24)).watt_limits()
ok(wl3["cpu_w"] >= 200.0, "24 核 -> CPU 钳制放宽", wl3["cpu_w"])

# ---------------------------------------------------------------- 11. ensure 端到端
print("== 11. ensure 端到端（假通道） ==")
cfg5 = {"gpu_tgp_max_watts": None, "gpu_temp_limit_c": None,
        "power_envelope_watts": None, "dc_refresh_hz": None,
        "pd_low_hz_value": None, "alloc_enabled": True,
        "gpu_eco_auto": True, "gpupick_enabled": True}
path2 = tmp_json()
hp10 = H.ensure(cfg5, nvml=FakeNvml(), display=FakeDisplay(hz=240, avail=[60, 120, 240]),
                atkacpi_ok=True, path=path2)
ok(cfg5["dc_refresh_hz"] == 120, "240Hz 屏 -> 挑低一档 120", cfg5["dc_refresh_hz"])
ok(os.path.isfile(path2), "档案已落盘")
ok(hp10.summary()["has_dgpu"] is True, "summary 里有独显标记")
os.remove(path2)

# ---------------------------------------------------------------- 12. 源码不带本机标定
print("== 12. 仓库默认值必须是通用兜底，不能是某一台机器的实测值 ==")
from lp import config          # noqa: E402

D = config.DEFAULT_CONFIG
ok(D["gpu_tgp_max_watts"] is None, "默认不写死独显上限（交给现测）", D["gpu_tgp_max_watts"])
ok(D["gpu_temp_limit_c"] is None, "默认不写死独显温度线")
ok(D["power_envelope_watts"] is None, "默认不写死整机包络")
ok(D["dc_refresh_hz"] is None, "默认不写死降刷目标")
ok(D["pd_low_hz_value"] is None, "默认不写死 PD 降刷目标")
ok(D["pd_supply_w"] is None, "默认不写死电源上限（学习得到）")

# ---------------------------------------------------------------- 13. 静默底线
print("== 13. 常驻路径上的子进程必须不弹黑窗（CREATE_NO_WINDOW） ==")
import glob      # noqa: E402
import io        # noqa: E402
import re        # noqa: E402

bad = []
for f in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lp", "*.py")):
    src = io.open(f, encoding="utf-8").read()
    for m in re.finditer(r"subprocess\.(?:run|Popen|check_output|call)\(", src):
        seg = src[m.start():m.start() + 600]
        if "creationflags" not in seg:
            bad.append(os.path.basename(f))
            break
ok(not bad, "lp/ 下所有子进程调用都带 creationflags", ",".join(bad))

print("\n%d 项断言，失败 %d" % (N[0], len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
