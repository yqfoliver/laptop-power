"""离电时刻学习：从历史「插电 → 离电」的时刻里，找出你平时几点出门。

为什么单独一层：华硕 EC 的充电上限只有 60/80/100 三档，下发后约 13 分钟才真的
改变充电行为，还可能被厂商服务回滚（见 hw.set_asus_charge_limit）。也就是说
「出门前充满」必须**提前足够久**，而且误判的代价很实在（整天顶在 100% 正是养护
要避免的事）。所以判定要便宜、可测、不碰硬件 —— 本模块只做一件事：
读一串时间戳，回答「你现在是不是快出门了」。

标定（实测，别凭感觉改）：EC 延迟 ~13 分钟；80%→100% 再充约 18 分钟
（60Wh 级电池、~45W 充电）。合计 ~31 分钟 ⇒ 提前量取 40 分钟留余量。
提前量给 2 小时反而更糟：充完就顶在 100% 干等，等于白养护。
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

MIN_SAMPLES = 3          # 同一时间窗至少要见过 3 次才敢预测（一次巧合不算规律）
WINDOW_MIN = 45          # 时间窗半宽：±45 分钟内的历史离电都算「同一趟」
LOOKBACK_D = 21          # 只看最近三周：作息变了要能跟上，不能被旧习惯绑架
DEDUPE_S = 30 * 60       # 同一次出门只记一条（合盖/抖动/重插不算新的一趟）
MAX_KEEP = 240           # 最多留 240 条（约小半年），文件不膨胀
LEAD_MIN = 40            # 提前量（见上面的标定）
DAY_MIN = 24 * 60


def _clean(seq) -> List[float]:
    """把持久化回来的序列洗成合法时间戳（JSON 里可能是字符串/None/坏值）。"""
    out: List[float] = []
    if not isinstance(seq, (list, tuple)):
        return out
    for v in seq:
        try:
            t = float(v)
        except (TypeError, ValueError):
            continue
        if 0 < t < 4102444800.0:          # 2100 年之前都算合理（挡住 0 / 负 / 毫秒）
            out.append(t)
    out.sort()
    return out


def record(seq, ts: Optional[float] = None) -> bool:
    """记一次「出门」。返回是否真的新增（同一趟重复调用返回 False）。

    seq 由调用方持有并负责持久化（本模块不读文件、不写文件）。
    """
    try:
        t = float(time.time() if ts is None else ts)
    except (TypeError, ValueError):
        return False
    # 与 _clean 同一口径：0 / 负数 / 毫秒时间戳 / 2100 年后一律不记。
    # 记进去也能被 _clean 滤掉，但那就成了"文件里有脏值"，排查时更费劲。
    if not (0 < t < 4102444800.0):
        return False
    clean = _clean(seq)
    if clean and (t - clean[-1]) < DEDUPE_S:
        return False
    clean.append(t)
    if len(clean) > MAX_KEEP:
        clean = clean[-MAX_KEEP:]
    if isinstance(seq, list):
        seq[:] = clean
    return True


def _minute_of_day(ts: float) -> int:
    lt = time.localtime(ts)
    return lt.tm_hour * 60 + lt.tm_min


def _dist(a: int, b: int) -> int:
    """一天之内的环形距离（23:50 和 00:10 只差 20 分钟）。"""
    d = abs(a - b) % DAY_MIN
    return min(d, DAY_MIN - d)


def _signed(a: int, b: int) -> int:
    """a 相对 b 的有符号分钟差，跨零点取向最近的一侧。"""
    d = (a - b) % DAY_MIN
    return d - DAY_MIN if d > DAY_MIN // 2 else d


def predict(seq, now: Optional[float] = None,
            lead_min: int = LEAD_MIN) -> Optional[Dict]:
    """此刻是否处在「快出门」的窗口里。

    返回 None（样本不足 / 现在不像要出门）或：
      {hhmm: "08:10", n: 5, in_min: 35, due: True, lead_min: 40}
    in_min 是最常见的出门时刻距现在多少分钟（正=还没到）。
    """
    now = float(time.time() if now is None else now)
    cut = now - LOOKBACK_D * 86400
    now_m = _minute_of_day(now)
    pts = []
    for ts in _clean(seq):
        if ts < cut or ts > now + 86400:      # 太旧 / 未来（时钟错乱）一律不用
            continue
        m = _minute_of_day(ts)
        if _dist(m, now_m) <= WINDOW_MIN:
            pts.append(m)
    if len(pts) < MIN_SAMPLES:
        return None
    pts.sort()
    med = pts[len(pts) // 2]                  # 中位数：比均值抗「偶尔一次早走」
    in_min = _signed(med, now_m)
    return {
        "hhmm": "%02d:%02d" % (med // 60, med % 60),
        "n": len(pts),
        "in_min": int(round(in_min)),
        "due": 0 <= in_min <= int(lead_min),
        "lead_min": int(lead_min),
    }
