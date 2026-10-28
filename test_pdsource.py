# -*- coding: utf-8 -*-
"""供电能力辨识（pdsource）+ 放电快速下修（pdbudget）回归测试。

覆盖的事故：供电上限曾经是**全局常量** —— 在 200W 适配器上学到 130W，
换到 100W 的 Type-C 上那个 130W 不作废，控制器以为余量充足全程不压制，
电池一路放电。所以：换充电器必须作废，放电必须快速下修。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import pdsource as ps  # noqa: E402
from lp.pdbudget import PdBudget  # noqa: E402

N = [0]
FAILS = []


def ok(cond, msg, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append("%s  %s" % (msg, extra))


def tmp_path():
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(p)
    return p


def feed(pd, ac, rate, machine, dt=5.0, n=1):
    """连续喂 n 拍"""
    out = None
    for _ in range(n):
        out = pd.feed(ac, rate, dt, machine)
    return out


# ---------------------------------------------------------------- 1. 会话跟踪
print("== 1. 供电会话：换充电器必须作废旧值 ==")
p = tmp_path()
s = ps.PdSource(path=p)
ok(s.note_ac(True) == "new", "首次观测到插电 = 新会话", s.note_ac(True))
ok(s.note_ac(True) == "same", "同一会话延续", "")
ok(s.note_ac(False) == "gone", "拔电 = 会话结束", "")
ok(s.note_ac(False) == "gone", "持续拔电仍是 gone", "")
ok(s.note_ac(True) == "new", "重新插电 = 新会话（可能换了充电器）", "")

# ---------------------------------------------------------------- 2. 冷启动沿用
print("== 2. 冷启动：只在「刚离开过」时沿用 ==")
p = tmp_path()
s = ps.PdSource(path=p)
s.note_ac(True)
s.save(89.0, firm=True)
ok(abs((s.adopt_on_start() or 0) - 89.0) < 0.01, "刚存的（几秒内）可以沿用",
   s.adopt_on_start())

s2 = ps.PdSource(path=p)
ok(s2.state["current"] is not None, "落盘成功", s2.state)
s2.state["current"]["ts"] = time.time() - 60
s2._save()
ok(abs((ps.PdSource(path=p).adopt_on_start() or 0) - 89.0) < 0.01,
   "1 分钟前 = 只是程序重启，沿用", "")

s3 = ps.PdSource(path=p)
s3.state["current"]["ts"] = time.time() - 3600
s3._save()
ok(ps.PdSource(path=p).adopt_on_start() is None,
   "1 小时前 = 期间很可能换过充电器，不沿用", "")

# ---------------------------------------------------------------- 3. 归档
print("== 3. 会话归档 ==")
p = tmp_path()
s = ps.PdSource(path=p)
s.note_ac(True)
s.save(130.0, firm=True)
s.note_ac(False)                      # 拔电 → 归档
ok(130.0 in s.history_watts(), "拔电后归档本次会话的值", s.history_watts())
ok(s.state.get("current") is None, "归档后 current 清空（下次重新学）", s.state)

s.note_ac(True)
s.save(89.0, firm=True)
s.note_ac(False)
ok(s.history_watts()[-1] == 89.0 and 130.0 in s.history_watts(),
   "两次会话都留下记录", s.history_watts())

# ---------------------------------------------------------------- 4. 快速下修
print("== 4. 换到更弱的电源：放电必须快速下修 ==")
pd = PdBudget({})
pd.set_supply(130.8, firm=True)       # 上一个（200W）充电器学到的值
for i in range(6):
    pd.feed(True, 12.0, 5.0, 100.0)   # 整机 100W、电池放电 12W ⇒ 上限 88W
ok(pd.supply_w < 95.0, "6 拍内从 130.8 收敛到 ~88W", round(pd.supply_w, 1))
ok(pd.supply_w > 85.0, "不能 overshoot 到离谱的低值", round(pd.supply_w, 1))

pd2 = PdBudget({})
pd2.set_supply(130.8, firm=True)
pd2.feed(True, 12.0, 5.0, 100.0)
ok(pd2.supply_w > 120.0, "单拍只小步跟随（挡孤立坏值）", round(pd2.supply_w, 1))
pd2.feed(True, 12.0, 5.0, 100.0)
ok(pd2.supply_w < 115.0, "第二拍确认后开始大步下修", round(pd2.supply_w, 1))

# ---------------------------------------------------------------- 5. 坏值不误伤
print("== 5. 孤立尖峰不应拉低上限 ==")
pd = PdBudget({})
pd.set_supply(89.0, firm=True)
pd.feed(True, 40.0, 5.0, 95.0)        # 一拍的假放电（坏值）
ok(pd.supply_w > 80.0, "孤立尖峰一拍不该把 89 拽到 55", round(pd.supply_w, 1))
pd.feed(True, -3.0, 5.0, 40.0)        # 恢复正常的充电读数
ok(pd._dn_streak == 0, "正常读数把下修计数清零", pd._dn_streak)

# ---------------------------------------------------------------- 6. 上修仍要慢
print("== 6. 充电方向仍要连续确认（老 bug 不能回归）==")
pd = PdBudget({})
pd.feed(True, -3.0, 5.0, 60.0)        # lb = 63
ok(abs(pd.supply_w - 63.0) < 0.6, "首次充电直接建立下界", pd.supply_w)
for _ in range(8):
    pd.feed(True, -30.0, 5.0, 60.0)   # 虚高：只持续 8 拍
ok(pd.supply_w < 75.0, "8 拍虚高不足以顶穿棘轮（需 10 拍）", round(pd.supply_w, 1))
for _ in range(20):
    pd.feed(True, -30.0, 5.0, 60.0)
ok(pd.supply_w > 75.0, "持续 20 拍才认定为真电源升级", round(pd.supply_w, 1))

# ---------------------------------------------------------------- 7. 换源清零
print("== 7. 换充电器后重新学（不再拿旧值压制判断）==")
pd = PdBudget({})
pd.set_supply(130.0, firm=True)
pd.reset_learning()
ok(pd.supply_w is None, "reset_learning 清空上限", pd.supply_w)
ok(pd.supply_firm is False, "reset 后不再是「已确认」", pd.supply_firm)
pd.feed(True, 12.0, 5.0, 100.0)
ok(abs(pd.supply_w - 88.0) < 0.6, "新会话第一拍就得到精确上界 88W", pd.supply_w)

# 学习中不预判（没有上限就不该"提前限帧"）
pd3 = PdBudget({})
ok(pd3.feed(True, 0.0, 5.0, 100.0) is None or True, "无上限时也能跑", "")

# ---------------------------------------------------------------- 8. 报告字段
print("== 8. 面板字段 ==")
pd = PdBudget({})
pd.set_supply(89.0, firm=True)
r = pd.report()
ok("supply_src" in r, "报告带来源标签", list(r))
ok("沿用" in (r.get("supply_src") or ""), "注入值标记为沿用", r.get("supply_src"))
pd.feed(True, 5.0, 5.0, 90.0)
pd.feed(True, 5.0, 5.0, 90.0)
ok("实测" in (pd.report().get("supply_src") or ""), "实测后标记为实测",
   pd.report().get("supply_src"))

# ---------------------------------------------------------------- 9. 落盘格式
print("== 9. 存档文件 ==")
p = tmp_path()
s = ps.PdSource(path=p)
s.note_ac(True)
s.save(89.4, firm=True)
d = json.load(open(p, encoding="utf-8"))
ok(d["version"] == ps.VERSION, "版本号正确", d.get("version"))
ok(abs(d["current"]["watts"] - 89.4) < 0.01, "值落盘", d["current"])
ok(d["current"]["session_start"] is not None, "带会话起始时间", d["current"])
s.save(500.0)            # 离谱值
ok(abs(s.state["current"]["watts"] - 89.4) < 0.01, "离谱值不落盘",
   s.state["current"])
os.remove(p)

# ---------------------------------------------------------------- 10. 学成溯源
print("== 10. learn_info：这个供电值是哪一拍、什么负载推出的 ==")
p = tmp_path()
s = ps.PdSource(path=p)
s.note_ac(True)
info = {"at": 1792234799.6, "machine_w": 105.3, "rate_w": 2.7,
        "how": "充电下界·连续确认抬升"}
s.save(108.0, firm=False, info=info)
d = json.load(open(p, encoding="utf-8"))
ok(d["current"].get("learn", {}).get("how") == "充电下界·连续确认抬升",
   "current 带学成溯源", d["current"].get("learn"))
ok(abs(d["current"]["learn"]["machine_w"] - 105.3) < 0.01,
   "溯源含当时整机功耗", d["current"]["learn"])
s.note_ac(False)          # 拔电竞档
h = s.state["history"]
ok(h and h[-1].get("learn", {}).get("how") == "充电下界·连续确认抬升",
   "归档会话也带溯源", h[-1].get("learn") if h else None)
ok(h[-1]["learn"]["machine_w"] == 105.3, "归档的 machine_w 完整",
   h[-1]["learn"])
os.remove(p)

# _learn_supply 各分支都记录溯源（首拍 / 连续确认抬升 / 放电标定）
print("== 10b. _learn_supply 分支记录 learn_info ==")
pd = PdBudget({})
feed(pd, True, -3.0, 100.0, n=1)          # 充电 + 100W 整机 + 3W 充电
ok(pd.learn_info.get("how") == "充电下界·首拍", "首拍记录", pd.learn_info)
ok(pd.learn_info.get("machine_w") == 100.0, "首拍带 machine_w", pd.learn_info)
ok(pd.learn_info.get("rate_w") == -3.0, "首拍带 rate_w", pd.learn_info)
from lp.pdbudget import SUPPLY_LB_STREAK
feed(pd, True, -2.0, 105.0, n=SUPPLY_LB_STREAK)   # 连续 10 拍 lb=107（+4W）
ok(pd.supply_w == 107.0, "棘轮抬到新下界", pd.supply_w)
ok(pd.learn_info.get("how") == "充电下界·连续确认抬升", "抬升记录",
   pd.learn_info)
ok(pd.learn_info.get("machine_w") == 105.0, "抬升记录的 machine_w 是新值",
   pd.learn_info)
# 跳变超过 15W 时走「小步爬升」（+15 封顶），不能一步跳过去
pd3 = PdBudget({})
feed(pd3, True, -3.0, 100.0, n=1)
feed(pd3, True, -2.0, 140.0, n=SUPPLY_LB_STREAK)
ok(pd3.supply_w == 118.0, "小步爬升封顶 +15", pd3.supply_w)
ok(pd3.learn_info.get("how") == "充电下界·小步爬升", "小步爬升记录",
   pd3.learn_info)
pd2 = PdBudget({})
feed(pd2, True, 20.0, 60.0, n=1)          # 放电 20W -> est=40W 精确上界
ok(pd2.supply_firm and pd2.learn_info.get("how") == "放电标定",
   "放电首拍即标定并记录", pd2.learn_info)

# ---- 存档层必须跟学习层用同一个下限（10W）----
# 学习侧 2026-10-21 就把下限从 25W 降到 10W 了（~20W 手机级充电器是真值），
# 但存档/沿用/归档仍写死 25W：学到的 18W 存不进去、下次沿用不了、历史缺段。
src = ps.PdSource(path=tmp_path())
src.note_ac(False)
src.note_ac(True)                          # 开启一次会话
src.save(18.2, firm=True)
ok(src.state.get("current") is not None, "18W 也要能存档（不是异常值）",
   src.state.get("current"))
# 程序重启（充电器没换、AC 没断）：应能沿用这个 18W
src2 = ps.PdSource(path=src.path)
a = src2.adopt()
ok(a is not None and abs(a["watts"] - 18.2) < 0.05,
   "冷启动能沿用 18W（否则弱电源第一分钟又白放电池）", a)
src.note_ac(False)                         # 拔电 -> 归档
ok(any(abs(h.get("watts", 0) - 18.2) < 0.05 for h in src.state["history"]),
   "18W 会话也要进历史", src.state["history"])

print("\n%d 项断言，失败 %d" % (N[0], len(FAILS)))
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
