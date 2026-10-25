# -*- coding: utf-8 -*-
"""一次性探测：本机 ATKACPI 的 PPT 功耗端点是否可写（对标 G-Helper AsusACPI.cs）。

做法（全部可还原，不残留任何系统状态）：
  1. 对每个目标端点先读原值
  2. 写一个「安全测试值」（落入常规区间，绝非 0 / 极小值）
  3. 立刻读回校验
  4. 无论结果如何，finally 把原值写回去
  5. 打印每个端点的可写性

只验证、不启用。跑完即退出，不留句柄。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from lp.atkacpi import AtkAcpi, DEV

# (端点名, 安全测试值) —— 测试值必须在常规区间内，且与本机原值不同，
# 否则「写不写读回都一样」无法判定可写性。即便还原因异常失败，也只是短暂
# 限速/限功，重启即恢复，不会变砖。
TARGETS = [
    ("ppt_sustained", 40),   # A3 = SPL / PL1（PD S3 最想直写的）
    ("ppt_slow",      45),   # A0 = sPPT / PL2
    ("ppt_fast",      50),   # C1 = fPPT
    ("ppt_gpu_boost", 15),   # C0 = Dynamic Boost（5W 步进）
    ("gpu_power_base",45),   # 99 = 独显 TGP 基准（本机原读 55，测 45 区分）
    ("gpu_power_var", 10),   # 98 = 独显 TGP 附加瓦数（本机原读 0，测 10 区分）
]


def safe_read(a, name):
    """既试标量读，也回退到原始缓冲读，尽量拿到当前值。"""
    v = a.read_val(name)
    if v is not None:
        return v
    raw = a.read_buf(name, 0, 16)
    if raw and len(raw) >= 2:
        return int.from_bytes(raw[:2], "little")
    return None


def main():
    a = AtkAcpi()
    if not a.ok:
        print("ATKACPI 不可用（句柄打不开），无法探测。退出。")
        return
    print("ATKACPI 句柄已打开，开始探测（每个端点 写→读→还原）\n")

    results = []
    for name, test_v in TARGETS:
        dev_id = DEV.get(name)
        if not dev_id:
            print("  [跳过] %s：不在 DEV 表" % name)
            continue
        orig = None
        writable = False
        readback = None
        err = 0
        try:
            orig = safe_read(a, name)
            # 写测试值
            ok_w = a.write_int(name, test_v)
            time.sleep(0.05)  # 给固件一点落盘时间
            readback = safe_read(a, name)
            # 判定可写：写成功了且读回≈测试值（允许 ±2 量化）
            writable = bool(ok_w) and readback is not None and abs(readback - test_v) <= 2
            err = a.last_error
        except Exception as e:
            print("  [异常] %s: %r" % (name, e))
        finally:
            # 无论如何还原原值
            try:
                if orig is not None:
                    a.write_int(name, orig)
            except Exception:
                pass
        status = "可写 ✅" if writable else "不可写 ❌"
        print("  %-16s dev=0x%08X  原值=%s  测试写=%d  读回=%s  %s (last_err=%d)"
              % (name, dev_id, orig, test_v, readback, status, err))
        results.append((name, writable, orig, test_v, readback))

    print("\n=== 汇总 ===")
    for name, writable, orig, test_v, readback in results:
        print("  %-16s %s" % (name, "WRITABLE" if writable else "read-only/denied"))
    nw = sum(1 for _, w, *_ in results if w)
    print("\n可写端点 %d / %d" % (nw, len(results)))
    print("探测结束，所有原值已还原。")


if __name__ == "__main__":
    main()
