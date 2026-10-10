# -*- coding: utf-8 -*-
"""核显优先调度（gpupick）回归测试。

全部走假注册表（dict）与假利用率探针，不碰真实注册表、不启动 PDH。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lp.gpupick import GpuPick, PROBE_MIN_SAMPLES, PROBE_OK_P90, PROBE_HARD_P90  # noqa: E402

OK = 0
FAIL = []


def ck(name, cond, extra=""):
    global OK
    if cond:
        OK += 1
    else:
        FAIL.append(name + ((" | " + str(extra)) if extra else ""))


def mk(tier=2, reg=None):
    tmp = tempfile.mkdtemp(prefix="gpupick_")
    r = {} if reg is None else reg
    gp = GpuPick(reg=r, path=os.path.join(tmp, "gpupick.json"))
    gp.igpu_tier = tier
    return gp, r


PATH_LOL = "C:\\Riot Games\\League of Legends\\League of Legends.exe"
PATH_CP = "C:\\Steam\\Cyberpunk 2077\\bin\\x64\\Cyberpunk2077.exe"
PATH_UN = "C:\\Some\\UnknownGame.exe"


def test_classify():
    gp, _ = mk(tier=2)
    ck("LOL(tier2) 在 tier2 机器走核显", gp.classify("League of Legends.exe")[0] == "igpu")
    ck("LOL 判定来源是内置名单", gp.classify("League of Legends.exe")[1] == "builtin")
    ck("赛博朋克走独显", gp.classify("Cyberpunk2077.exe")[0] == "dgpu")
    ck("名单外不干预", gp.classify("unknowngame.exe")[0] is None)
    ck("空进程名不干预", gp.classify("")[0] is None)

    gp1, _ = mk(tier=1)
    ck("tier1 机器不敢跑 tier2 游戏", gp1.classify("League of Legends.exe")[0] is None)
    ck("tier1 机器仍跑 tier1 游戏",
       gp1.classify("Stardew Valley.exe")[0] == "igpu")
    ck("大小写不敏感", gp1.classify("STARDEW VALLEY.EXE")[0] == "igpu")

    gp3, _ = mk(tier=3)
    ck("tier3 机器可跑 tier3 游戏", gp3.classify("GTA5.exe")[0] == "igpu")


def test_apply_and_orig():
    gp, reg = mk(tier=2)
    r = gp.apply_for(PATH_LOL, "League of Legends.exe")
    ck("应用核显返回 igpu", r == "igpu")
    ck("注册表写入 GpuPreference=1", reg.get(PATH_LOL) == "GpuPreference=1;")
    ck("原值被记录（原本不存在→None）", gp.data["orig"].get(PATH_LOL, "MISSING") is None)

    # 名单外：不写任何东西
    reg2 = {}
    gp2, reg2 = mk(tier=2)
    gp2.apply_for(PATH_UN, "unknowngame.exe")
    ck("名单外不写注册表", PATH_UN not in reg2)

    # 重复应用：幂等，不重复记 orig
    gp.apply_for(PATH_LOL, "League of Legends.exe")
    ck("重复应用幂等", reg.get(PATH_LOL) == "GpuPreference=1;")


def test_restore_keeps_original():
    """厂商/用户已有的设置必须被还原回去，而不是删掉。"""
    reg = {PATH_CP: "GpuPreference=2;"}
    gp, reg = mk(tier=2, reg=reg)
    gp.reg_set(PATH_CP, 1)
    ck("改写后为核显", reg[PATH_CP] == "GpuPreference=1;")
    gp.reg_restore(PATH_CP)
    ck("还原回厂商原值（不是删除）", reg.get(PATH_CP) == "GpuPreference=2;")

    # 原本没有的：还原 = 删除
    gp.reg_set(PATH_LOL, 1)
    gp.reg_restore(PATH_LOL)
    ck("原本没有的还原后删除", PATH_LOL not in reg)


def test_user_override():
    gp, reg = mk(tier=2)
    gp.set_user("Cyberpunk2077.exe", "igpu")
    ck("用户钉住优先于内置『必须独显』",
       gp.classify("Cyberpunk2077.exe")[0] == "igpu")
    ck("来源标记为 user", gp.classify("Cyberpunk2077.exe")[1] == "user")
    gp.set_user("Cyberpunk2077.exe", None)
    ck("取消钉住后回到内置判定", gp.classify("Cyberpunk2077.exe")[0] == "dgpu")

    # 用户钉 dgpu 时，即使没动过也允许写入（用户明确要求）
    gp.set_user("unknowngame.exe", "dgpu")
    gp.apply_for(PATH_UN, "unknowngame.exe")
    ck("用户钉独显会写入", reg.get(PATH_UN) == "GpuPreference=2;")


def test_probe_ok():
    """核显游刃有余 → 固化 igpu。"""
    gp, reg = mk(tier=2)
    gp.probe_start(PATH_UN, "unknowngame.exe", 999, seconds=0)
    for _ in range(PROBE_MIN_SAMPLES + 2):
        gp._probe["samples"].append(30.0)
    gp.probe_tick()
    ck("试探完成", gp._probe["done"] is True)
    ck("判定为 ok", gp._probe["verdict"] == "ok")
    ck("固化为核显", gp.data["apps"]["unknowngame.exe"]["mode"] == "igpu")
    ck("固化来源是 probe", gp.data["apps"]["unknowngame.exe"]["src"] == "probe")
    ck("保留核显设置", reg.get(PATH_UN) == "GpuPreference=1;")


def test_probe_hard():
    """核显吃满 → 退回独显，并把注册表还原成原值。"""
    reg = {PATH_UN: "GpuPreference=2;"}
    gp, reg = mk(tier=2, reg=reg)
    gp.probe_start(PATH_UN, "unknowngame.exe", 999, seconds=0)
    for _ in range(PROBE_MIN_SAMPLES + 2):
        gp._probe["samples"].append(99.0)
    gp.probe_tick()
    ck("判定为 hard", gp._probe["verdict"] == "hard")
    ck("退回独显", gp.data["apps"]["unknowngame.exe"]["mode"] == "dgpu")
    ck("吃力时还原原值", reg.get(PATH_UN) == "GpuPreference=2;")


def test_probe_marginal_and_nodata():
    gp, _ = mk(tier=2)
    gp.probe_start(PATH_UN, "u1.exe", 999, seconds=0)
    for _ in range(PROBE_MIN_SAMPLES + 2):
        gp._probe["samples"].append((PROBE_OK_P90 + PROBE_HARD_P90) / 2)
    gp.probe_tick()
    ck("中间地带判 marginal", gp._probe["verdict"] == "marginal")

    gp2, _ = mk(tier=2)
    gp2.probe_start(PATH_UN, "u2.exe", 999, seconds=0)
    gp2._probe["samples"].append(50.0)      # 样本不足
    gp2.probe_tick()
    ck("样本不足判 no_data", gp2._probe["verdict"] == "no_data")
    ck("样本不足不下结论", "u2.exe" not in gp2.data["apps"])

    # 没在用 GPU（利用率 0）不算有效样本
    gp3, _ = mk(tier=2)
    gp3.probe_start(PATH_UN, "u3.exe", 999, seconds=0)
    for _ in range(20):
        gp3._probe["samples"].append(0.0)
    gp3.probe_tick()
    ck("全零样本判 no_data", gp3._probe["verdict"] == "no_data")


def test_restore_all():
    gp, reg = mk(tier=2)
    gp.apply_for(PATH_LOL, "League of Legends.exe")
    gp.apply_for(PATH_CP, "Cyberpunk2077.exe")     # dgpu：本例未介入过→不写
    gp.reg_set(PATH_UN, 1)
    n = gp.restore_all()
    ck("还原条目数 >= 1", n >= 1, n)
    ck("还原后不留我们的痕迹", PATH_UN not in reg and PATH_LOL not in reg)
    ck("orig 清空", gp.data["orig"] == {})


def test_disabled():
    gp, reg = mk(tier=2)
    gp.enabled = False
    ck("关闭后不应用", gp.apply_for(PATH_LOL, "League of Legends.exe") is None)
    ck("关闭后不写注册表", PATH_LOL not in reg)


def test_report_shape():
    gp, _ = mk(tier=2)
    r = gp.report()
    for k in ("enabled", "igpu_tier", "managed", "n_igpu", "note", "probe"):
        ck("report 含字段 %s" % k, k in r)



def test_pick_target_panel_focus():
    """面板当前台时（foreground 返回空），按钮要作用在「上一个真实前台程序」上。

    事故（2026-10-10）：四个核显按钮点了没反应 —— 因为用户点按钮时前台就是
    本面板，hw.foreground() 把自家人排除返回空名字，gpupick_set 直接 return。
    """
    from lp.gpupick import pick_target
    # 前台是别的程序：直接用它
    t = pick_target({"process": "game.exe", "path": "C:////g////game.exe", "pid": 42},
                    {"process": "old.exe", "path": "C:////o.exe", "pid": 7})
    ck("前台有程序就用前台", t["process"] == "game.exe" and t["stale"] is False, t)
    ck("路径与 pid 一起带出", t["path"].endswith("game.exe") and t["pid"] == 42, t)
    # 前台是面板自己（self / 空）→ 退到上一个真实前台程序
    t2 = pick_target({"process": "", "path": "", "pid": 0, "self": True},
                     {"process": "old.exe", "path": "C:////o.exe", "pid": 7})
    ck("面板当前台时退到上一个程序", t2["process"] == "old.exe", t2)
    ck("并标记为 stale（UI 要如实说明）", t2["stale"] is True, t2)
    ck("stale 也要带出路径，注册表才写得进去", t2["path"] == "C:////o.exe", t2)
    # 从来没有过真实前台程序 → 全空，不能瞎猜
    t3 = pick_target({"process": "", "pid": 0}, None)
    ck("没有可操作对象时返回空", t3["process"] == "" and t3["stale"] is False, t3)
    t4 = pick_target(None, {"process": "", "pid": 0})
    ck("last_fg 也是空则返回空", t4["process"] == "", t4)


def main():
    test_classify()
    test_apply_and_orig()
    test_restore_keeps_original()
    test_user_override()
    test_probe_ok()
    test_probe_hard()
    test_probe_marginal_and_nodata()
    test_restore_all()
    test_disabled()
    test_pick_target_panel_focus()
    test_report_shape()
    print("结果: %d 项通过, %d 项失败" % (OK, len(FAIL)))
    for f in FAIL:
        print("  ✘", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
