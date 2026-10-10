# -*- coding: utf-8 -*-
"""按 luid 分组看 GPU Engine 的 3D 实例，判断能否区分核显 / 独显。"""
import time
import win32pdh  # type: ignore
from collections import defaultdict

items = win32pdh.EnumObjectItems(None, None, "GPU Engine", -1)
insts = [s for s in (items[-1] if items else []) if "engtype_3D" in s]

by_luid = defaultdict(list)
for s in insts:
    luid = s.split("_luid_")[1].split("_")[0] + "_" + s.split("_luid_")[1].split("_")[1]
    by_luid[luid].append(s)

print("engtype_3D 实例数:", len(insts))
print("luid 分组:", len(by_luid))
for l, v in by_luid.items():
    print("  ", l, "→", len(v), "个进程实例")

q = win32pdh.OpenQuery()
handles = []
for l, v in by_luid.items():
    # 每个 luid 只取前 6 个实例，避免打开过多计数器
    for s in v[:6]:
        try:
            p = win32pdh.MakeCounterPath((None, "GPU Engine", s, None, -1,
                                          "Utilization Percentage"))
            handles.append((l, s, win32pdh.AddCounter(q, p)))
        except Exception:
            pass

print("\n采样 3 次（间隔 1s），按 luid 汇总 3D 利用率：")
for i in range(3):
    win32pdh.CollectQueryData(q)
    time.sleep(1.0)
    win32pdh.CollectQueryData(q)
    agg = defaultdict(float)
    for l, s, h in handles:
        try:
            v = win32pdh.GetFormattedCounterValue(h, win32pdh.PDH_FMT_DOUBLE)[1]
            agg[l] += v
        except Exception:
            pass
    print("  第 %d 次:" % (i + 1),
          {l: round(v, 2) for l, v in agg.items()})
win32pdh.CloseQuery(q)

print("\n--- 对照：NVML 独显利用率 ---")
try:
    from lp import nvmlctl
    print("  ", nvmlctl.snapshot() if hasattr(nvmlctl, "snapshot") else "需查接口")
except Exception as e:
    print("  NVML 读取:", type(e).__name__, e)
