# -*- coding: utf-8 -*-
"""
离电续航 A/B 对照测试（2026-10-06）

目的：回答两个问题
    1) 本程序在离电续航状态下**有没有 bug**（档位抖动 / 刷新率反复改 /
       亮度反复改 / 独显 Eco 反复翻转 / 限频看门狗误触发）
    2) 本程序到底**省电还是费电**（同机同负载下与系统原生「平衡」对照）

做法：
    A 段（基线）：强制切到系统「平衡」计划，本程序不干预
    B 段（本程序）：恢复自动判定（离电 -> 续航档）
    两段各跑 N 分钟，采样电池放电功率、整机功耗、CPU 上限、刷新率、
    亮度、独显 Eco、档位；统计各指标的**变化次数**来暴露抖动类 bug。

主指标为什么用 system_w（整机功耗）而不是电池放电率：
    本机电池 rate_w 读数不可靠 —— 实测出现过「放电 6.25W < 整机 8.03W」
    这种物理上不成立的情况（ACPI BATTERY_STATUS 的 Rate 字段有噪声、
    且 rate_avg 是 EWMA，短窗口内漂得厉害）。system_w 来自 PDH Energy
    Meter，是整机实际能耗，短时对比更可信；电池电量% 的变化则作为
    长时段的辅助佐证。

用法：
    python tools/battery_soak.py --minutes 5                 # 只测本程序（零干扰）
    python tools/battery_soak.py --minutes 5 --ab            # A/B 对照（会短暂切档）
    python tools/battery_soak.py --minutes 5 --ab --settle 20

安全：结束时一定还原（清 manual_override + auto_mode=True），
      无论中途是否异常。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from lp import battery, config, display  # noqa: E402

BASE = "http://127.0.0.1:8753"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api_get(path, timeout=5):
    try:
        return json.loads(_opener.open(BASE + path, timeout=timeout).read())
    except Exception:
        return None


def api_post(path, payload, timeout=8):
    try:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        return _opener.open(req, timeout=timeout).read().decode()[:200]
    except Exception as e:
        return "ERR:%r" % e


EXE = os.path.join(HERE, "笔记本电源自适应.exe")


def pids_of_exe():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq 笔记本电源自适应.exe", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=15).stdout.decode("mbcs", "replace")
        res = []
        for line in out.splitlines():
            p = [x.strip('"') for x in line.split('","')]
            if len(p) >= 2 and p[0].startswith("笔记本电源自适应"):
                try:
                    res.append(int(p[1]))
                except Exception:
                    pass
        return res
    except Exception:
        return []


def quit_app(timeout=30, retries=3):
    """干净退出本程序（发菜单退出命令，不留僵尸托盘图标）。
    PyInstaller onefile 是父子双进程，必须按 exe 名把所有 pid 一起发。

    坑（2026-10-06 踩到）：程序刚启动的头几秒托盘窗口还没建好，
    EnumWindows 找不到窗口 -> 退出命令根本没发出去 -> 进程一直活着，
    于是「裸机基线」段实际仍在跑本程序，整轮对照悄悄失效。
    所以要重试：等窗口出现再发。"""
    import ctypes
    u = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    dword = ctypes.c_ulong()

    for attempt in range(retries):
        pids = set(pids_of_exe())
        if not pids:
            return True
        found = []

        def cb(hwnd, lp):
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(dword))
            if dword.value in pids:
                found.append(hwnd)
            return True

        u.EnumWindows(WNDENUMPROC(cb), 0)
        for h in found:
            u.PostMessageW(h, 0x0111, 1006, 0)     # WM_COMMAND / MENU_QUIT
        end = time.time() + timeout / retries
        while time.time() < end:
            if not pids_of_exe():
                return True
            time.sleep(1)
        print("   第 %d 次退出未成功，重试…" % (attempt + 1))
    return not pids_of_exe()


def start_app(timeout=40):
    DETACHED, NEWGROUP, NOWIN = 0x00000008, 0x00000200, 0x08000000
    if not os.path.isfile(EXE):
        return False
    subprocess.Popen([EXE], creationflags=DETACHED | NEWGROUP | NOWIN,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(1)
        if api_get("/api/status"):
            return True
    return False


def restore_auto():
    """无论发生什么，最后都把程序恢复成自动判定"""
    api_post("/api/auto", {"on": True})
    try:
        c = config.load()
        c["manual_override"] = None
        c["auto_mode"] = True
        config.save(c)
    except Exception:
        pass
    print("   已还原：自动判定开、manual_override 清空")


def _keep_awake():
    """测试期间阻止系统待机（但**不阻止熄屏**）。

    2026-10-07 踩到：续航档把「待机超时」设成 240 秒，第 4 段跑到 4 分 21 秒
    时机器直接睡了，采样戛然而止，最后一段只剩 27/60 个采样，数据全废。

    只阻止**系统**待机、不禁熄屏，是有意的：熄屏省电本来就是被测程序的一项
    收益，把它也禁掉会让对照偏向"程序没用"。
    """
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return True
    except Exception:
        return False


def _wait_dc(timeout=900, interval=3):
    """阻塞等待「拔电」。插电时放电率为空，跑满 40 分钟也拿不到一个数字。"""
    bm = battery.BatteryMonitor()
    t0 = time.time()
    last_msg = -99
    while time.time() - t0 < timeout:
        try:
            b = bm.sample()
        except Exception:
            b = {}
        if not b.get("ac"):
            return True
        el = int(time.time() - t0)
        if el - last_msg >= 30:
            print("   仍插电（%s%%）… 已等 %ds，请拔掉电源适配器" % (b.get("percent"), el))
            last_msg = el
        time.sleep(interval)
    return False


class Sampler:
    def __init__(self):
        self.bat = battery.BatteryMonitor()
        try:
            self.disp = display.DisplayCtl()
        except Exception:
            self.disp = None
        # 程序退出时（A 段基线）没有面板可用，本地也要能采到整机功耗 /
        # 独显 Eco / CPU 上限，否则两段数据不可比
        try:
            from lp.power import PowerMonitor
            self.pmon = PowerMonitor()
            for _ in range(3):          # PDH 速率计数器前两次无效，先预热
                self.pmon.sample(want_cores=False, want_gpu=False)
        except Exception:
            self.pmon = None
        try:
            from lp.gpueco import GpuEco
            self.eco = GpuEco()
        except Exception:
            self.eco = None

    def once(self):
        row = {"ts": time.strftime("%H:%M:%S")}
        try:
            b = self.bat.sample()
            row.update({
                "percent": b.get("percent"),
                "rate_w": b.get("rate_w"),
                "rate_avg_w": b.get("rate_avg_w"),
                "eta_min": b.get("eta_minutes"),
                "ac": b.get("ac"),
            })
        except Exception:
            pass
        st = api_get("/api/status")
        if st is None and self.pmon:
            # 程序没在跑：本地采样补齐主指标
            try:
                p = self.pmon.sample(want_cores=False, want_gpu=False) or {}
                row.update({
                    "system_w": round(p["system_w"], 2) if p.get("system_w") else None,
                    "soc_w": round(p["soc_w"], 2) if p.get("soc_w") else None,
                    "cpu_util": round(p.get("cpu_util") or 0, 1),
                    "mode": "OFF",
                })
            except Exception:
                pass
            if self.eco:
                try:
                    row["eco"] = self.eco.read()
                except Exception:
                    pass
        if st:
            p = st.get("power") or {}
            k = st.get("knobs") or {}
            row.update({
                "mode": st.get("mode"),
                "system_w": round(p["system_w"], 2) if p.get("system_w") else None,
                "soc_w": round(p["soc_w"], 2) if p.get("soc_w") else None,
                "cpu_util": round(p.get("cpu_util") or 0, 1),
                "proc_max": (k.get("proc_max") or {}).get("value"),
                "bright": (k.get("bright") or {}).get("value"),
                "eco": (st.get("gpu_eco") or {}).get("eco"),
                "ppm_fixes": st.get("ppm_fixes"),
            })
        if self.disp:
            try:
                row["hz"] = self.disp.current_hz()
            except Exception:
                row["hz"] = None
        return row


def run_phase(name, label, minutes, interval, rows, seg=None):
    print()
    print("─" * 68)
    if seg:
        print("第 %d/4 段 · %s：%s（%d 分钟，每 %ds 采样）" % (seg, name, label, minutes, interval))
    else:
        print("段 %s：%s（%d 分钟，每 %ds 采样）" % (name, label, minutes, interval))
    print("─" * 68)
    s = Sampler()
    end = time.time() + minutes * 60
    n = 0
    while time.time() < end:
        r = s.once()
        r["phase"] = name
        r["seg"] = seg or 1
        rows.append(r)
        n += 1
        print("   %s %s%% 放电 %sW 整机 %sW | 档=%s CPU上限=%s%% 亮=%s %sHz eco=%s"
              % (r["ts"], r.get("percent"), r.get("rate_avg_w"), r.get("system_w"),
                 r.get("mode"), r.get("proc_max"), r.get("bright"),
                 r.get("hz"), r.get("eco")))
        time.sleep(interval)
    return n


def summarize(rows, phase):
    sub = [r for r in rows if r.get("phase") == phase]
    if not sub:
        return None
    rates = [r["rate_avg_w"] for r in sub if r.get("rate_avg_w")]
    sysw = [r["system_w"] for r in sub if r.get("system_w")]
    pcts = [r["percent"] for r in sub if r.get("percent") is not None]

    # A-B-A-B 时同一字母有两段。抖动次数必须**分段内**统计再相加 ——
    # 直接在合并后的行上数，会把「A1 末尾 → A2 开头」的跳变误记成抖动。
    segs = sorted({int(r.get("seg") or 1) for r in sub})

    def _in_seg(s):
        return [r for r in sub if int(r.get("seg") or 1) == s]

    def changes(key):
        tot = 0
        for s in segs:
            vals = [r.get(key) for r in _in_seg(s) if r.get(key) is not None]
            tot += sum(1 for i in range(1, len(vals)) if vals[i] != vals[i - 1])
        return tot

    dur_h = len(sub) * (sub[-1]["ts"] != sub[0]["ts"]) or 0
    out = {
        "phase": phase,
        "samples": len(sub),
        "avg_discharge_w": round(sum(rates) / len(rates), 2) if rates else None,
        "min_w": round(min(rates), 2) if rates else None,
        "max_w": round(max(rates), 2) if rates else None,
        "avg_system_w": round(sum(sysw) / len(sysw), 2) if sysw else None,
        "pct_start": pcts[0] if pcts else None,
        "pct_end": pcts[-1] if pcts else None,
        "mode_changes": changes("mode"),
        "hz_changes": changes("hz"),
        "bright_changes": changes("bright"),
        "eco_changes": changes("eco"),
        "proc_max_changes": changes("proc_max"),
        "ppm_fixes_end": sub[-1].get("ppm_fixes"),
        # 这一段实际跑在哪些档位/刷新率下 —— 用于校验 A/B 是否真的切成两种状态
        "modes": sorted({str(r.get("mode")) for r in sub}),
        "hzs": sorted({str(r.get("hz")) for r in sub}),
        "ecos": sorted({str(r.get("eco")) for r in sub}),
    }
    # 每小时掉电百分比：分段算再平均（合并算会跨过中间的重启空档）
    pph = []
    seg_w = []
    for s in segs:
        rs = _in_seg(s)
        ps = [r["percent"] for r in rs if r.get("percent") is not None]
        ws = [r["system_w"] for r in rs if r.get("system_w")]
        if ws:
            seg_w.append(round(sum(ws) / len(ws), 2))
        if len(rs) >= 2 and ps:
            secs = (len(rs) - 1) * ARGS.interval
            if secs > 0:
                pph.append((ps[0] - ps[-1]) * 3600.0 / secs)
    out["pct_per_hour"] = round(sum(pph) / len(pph), 2) if pph else None
    out["seg_w"] = seg_w          # 各段整机功耗，用来看有没有单调漂移
    out["segments"] = len(segs)
    return out


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--ab", action="store_true", help="A/B 对照（切到系统平衡档）")
    ap.add_argument("--ab-quit", action="store_true",
                    help="A/B 对照（真基线：本程序退出 vs 运行）。"
                         "刷新率与独显 Eco 是离电常驻策略，切档关不掉，"
                         "只有退出程序才能构造出真正的裸机基线")
    ap.add_argument("--abab", action="store_true",
                    help="A-B-A-B 四段交替（各 --minutes 分钟）。"
                         "两段交替能抵消电池电量下降带来的单调漂移，"
                         "是短窗口里唯一能拿到可信差值的做法")
    ap.add_argument("--wait-dc", action="store_true",
                    help="等到真正离电再开始（插电时放电率恒空，测了也白测）")
    ap.add_argument("--wait-dc-timeout", type=int, default=900,
                    help="等拔电的最长秒数，超时直接放弃（默认 900）")
    ap.add_argument("--settle", type=int, default=25, help="切档后稳定等待秒数")
    args = ap.parse_args()
    ARGS = args

    b0 = battery.BatteryMonitor().sample()
    if b0.get("ac") and not args.wait_dc:
        # 插着电跑 = 放电率全是空值，结论不可用。与其跑完才发现，不如自动等。
        print("!! 检测到插电：自动开启 --wait-dc（拔电后立即开跑）")
        args.wait_dc = True
    print("=" * 68)
    print("离电续航测试")
    print("=" * 68)
    print("当前：%s  电量 %s%%  放电 %sW  预计剩余 %s 分钟"
          % ("插电" if b0.get("ac") else "离电", b0.get("percent"),
             b0.get("rate_avg_w"), b0.get("eta_minutes")))
    if b0.get("ac"):
        print("!! 插电状态：放电率无意义，测试仍会继续但结论不可用")

    st = api_get("/api/status")
    if not st:
        print("!! 本程序未运行（面板不可达）：只能测基线，无法评估本程序行为")
    else:
        print("本程序状态：模式=%s 自动=%s" % (st.get("mode_label"), st.get("auto")))

    if args.wait_dc:
        print("\n⏳ 等待拔电（最多 %d 秒）… 拔掉电源适配器后会立即开始，无需任何操作"
              % args.wait_dc_timeout)
        if not _wait_dc(args.wait_dc_timeout):
            print("\n✘ 超时仍未拔电，测试放弃（插电状态测不出放电率）")
            return
        b = battery.BatteryMonitor().sample()
        print("✔ 已离电：%s%%  放电 %sW  —— 开始计时" % (b.get("percent"), b.get("rate_avg_w")))

    if args.abab or args.ab_quit or args.ab:
        print("防待机：%s" % ("已开启" if _keep_awake() else "开启失败（机器可能在 4 分钟后睡过去）"))

    rows = []
    try:
        if args.abab:
            seq = [("A", "裸机（本程序已退出）"), ("B", "本程序（离电续航档）"),
                   ("A", "裸机（本程序已退出）"), ("B", "本程序（离电续航档）")]
            for i, (ph, label) in enumerate(seq, 1):
                if ph == "A":
                    print("\n→ 退出本程序，构造裸机基线（刷新率/独显 Eco 会还原）…")
                    ok = quit_app()
                    print("   退出 %s（残留进程 %s）" % ("成功" if ok else "超时", pids_of_exe()))
                    time.sleep(args.settle)
                else:
                    print("\n→ 启动本程序…")
                    print("   启动 %s" % ("成功" if start_app() else "失败"))
                    time.sleep(args.settle)
                    stx = api_get("/api/status") or {}
                    print("   实际档位 = %s（离电期望 battery）" % stx.get("mode"))
                run_phase(ph, label, args.minutes, args.interval, rows, seg=i)
        elif args.ab_quit:
            print("\n→ 退出本程序，构造裸机基线（刷新率/独显 Eco 会还原）…")
            ok = quit_app()
            print("   退出 %s（残留进程 %s）" % ("成功" if ok else "超时", pids_of_exe()))
            time.sleep(args.settle)
            run_phase("A", "裸机（本程序已退出）", args.minutes, args.interval, rows)

            print("\n→ 重新启动本程序…")
            print("   启动 %s" % ("成功" if start_app() else "失败"))
            time.sleep(args.settle)
            run_phase("B", "本程序运行（离电续航档）", args.minutes, args.interval, rows)
        elif args.ab:
            if st:
                # 注意：接口参数名是 mode（不是 name），写错会静默返回 400，
                # 整段基线就变成了"没切换"，测试结论会完全失真（2026-10-06 踩过）
                print("\n→ 切到系统「平衡」作为基线…")
                print("   ", api_post("/api/mode", {"mode": "balanced"}))
                time.sleep(args.settle)
                now = api_get("/api/status") or {}
                got = now.get("mode")
                print("   切换后实际档位 = %s（期望 balanced）" % got)
                if got != "balanced":
                    print("   !! 切档未生效，A 段将不是真正的基线，结论不可用")
            run_phase("A", "系统平衡（本程序不干预）", args.minutes, args.interval, rows)

            print("\n→ 恢复本程序自动判定（离电应走续航档）…")
            print("   ", api_post("/api/auto", {"on": True}))
            time.sleep(args.settle)
            now = api_get("/api/status") or {}
            print("   恢复后实际档位 = %s（期望 battery）" % now.get("mode"))
            run_phase("B", "本程序自动（续航档）", args.minutes, args.interval, rows)
        else:
            run_phase("B", "本程序自动（续航档）", args.minutes, args.interval, rows)
    finally:
        print()
        restore_auto()

    # ---------- 汇总 ----------
    print()
    print("=" * 68)
    print("汇总")
    print("=" * 68)
    summaries = []
    for ph in ("A", "B"):
        sm = summarize(rows, ph)
        if sm:
            summaries.append(sm)
            print("\n【段 %s】采样 %d 次" % (ph, sm["samples"]))
            print("  档位/刷新率/Eco : %s / %sHz / eco=%s"
                  % (sm.get("modes"), sm.get("hzs"), sm.get("ecos")))
            print("  平均整机功耗 : %s W  ← 主指标" % sm["avg_system_w"])
            print("  平均放电功率 : %s W（%s ~ %s，仅供参考）"
                  % (sm["avg_discharge_w"], sm["min_w"], sm["max_w"]))
            print("  电量         : %s%% → %s%%（%s %%/小时）"
                  % (sm["pct_start"], sm["pct_end"], sm.get("pct_per_hour")))
            if len(sm.get("seg_w") or []) > 1:
                print("  各段整机功耗 : %s W（看是否单调漂移）" % sm.get("seg_w"))
            print("  ——— 抖动统计（bug 信号）———")
            print("  档位变化 %d 次 | 刷新率变化 %d 次 | 亮度变化 %d 次"
                  % (sm["mode_changes"], sm["hz_changes"], sm["bright_changes"]))
            print("  独显 Eco 变化 %d 次 | CPU上限变化 %d 次 | 限频自愈累计 %s 次"
                  % (sm["eco_changes"], sm["proc_max_changes"], sm["ppm_fixes_end"]))

    if len(summaries) == 2 and all(s["avg_system_w"] for s in summaries):
        a, b = summaries[0], summaries[1]
        d = a["avg_system_w"] - b["avg_system_w"]
        pct = 100.0 * d / a["avg_system_w"] if a["avg_system_w"] else 0
        print("\n【结论】以整机功耗为准（电池放电率噪声大，仅作参考）")
        print("  系统平衡 %.2fW → 本程序续航档 %.2fW，差 %+.2fW（%+.1f%%）"
              % (a["avg_system_w"], b["avg_system_w"], -d, pct))
        print("  %s" % ("本程序更省电 ✔" if d > 0 else "本程序更耗电 ✘（需排查）"))
        # 一致性校验：两段档位必须真的不同，否则对照无意义
        same = True
        for key in ("mode_changes",):
            pass
        if "modes" in a and "modes" in b and a["modes"] == b["modes"]:
            print("  !! 两段档位相同（%s），本次对照无效，请检查切档是否生效"
                  % a["modes"])
            same = False
        # A-B-A-B 专属校验：两次 A 之间、两次 B 之间不应有明显漂移。
        # 若 A1/A2 差得比 A/B 之差还大，说明是环境在变，不是程序在省电。
        for tag, sm in (("A", a), ("B", b)):
            sw = sm.get("seg_w") or []
            if len(sw) >= 2:
                drift = abs(sw[0] - sw[-1])
                print("  段间漂移 %s：%.2f W（%s → %s）%s"
                      % (tag, drift, sw[0], sw[-1],
                         "✔ 稳定" if drift < abs(d) else "!! 漂移大于组间差，结论不可信"))

    # ---------- 落盘 ----------
    out_csv = os.path.join(HERE, "battery_soak.csv")
    keys = ["phase", "seg", "ts", "percent", "rate_w", "rate_avg_w", "eta_min", "ac",
            "mode", "system_w", "soc_w", "cpu_util", "proc_max", "bright",
            "hz", "eco", "ppm_fixes"]
    try:
        with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print("\n明细已写入: %s" % out_csv)
    except Exception as e:
        print("\n写 CSV 失败: %r" % e)

    # 顺带拉一份系统自带的电池报告做历史对照
    try:
        rep = os.path.join(HERE, "battery_report.xml")
        subprocess.run(["powercfg", "/batteryreport", "/output", rep, "/xml"],
                       capture_output=True, timeout=60)
        if os.path.isfile(rep):
            print("系统电池报告: %s" % rep)
    except Exception as e:
        print("电池报告生成失败: %r" % e)


if __name__ == "__main__":
    main()
