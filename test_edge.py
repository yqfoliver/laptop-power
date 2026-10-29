# -*- coding: utf-8 -*-
"""
边缘模块回归测试（2026-10-29 第五轮审查）
=========================================
覆盖这一轮审查出并修掉的每一条缺陷 —— 每条断言都对应一个真实的坑：

  1. config.sanitize 漏掉「默认值为 None」的数值键（面板 POST 可绕过护栏）
  2. config.autostart_enabled 用 "power" in 文件名 判自启（PowerToys 也算）
  3. powercfgctl.parse_query 的档位名挂错设置（重复行 index() 总取第一处）
  4. webui 没有 Host/Origin 校验（任意网页可跨站 POST 到 127.0.0.1）
  5. checkup.run_checks 单点异常连坐（一项算不出来，后面全部消失）
  6. checkup.set_startup 接受任意绝对路径（可给任意文件改名）
  7. learner.report 把 disabled 字典 list() 成时间戳
  8. autostart_task 的 XML 不转义 & < >（路径带 & 时任务建不起来）
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import winreg
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lp import autostart_task, checkup, config, learner, powercfgctl, webui

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


# ---------------------------------------------------------------- 1. 配置护栏
print("== 1. sanitize：默认值为 None 的数值键也要过护栏 ==")
cfg = {"gpu_tgp_max_watts": "abc", "dc_refresh_hz": "not-a-number",
       "pd_low_hz_value": "60", "power_envelope_watts": 9999,
       "gpupick_igpu_tier": "2", "poll_seconds": "xyz", "notify": "yes"}
fixed = config.sanitize(cfg)
ok(cfg["gpu_tgp_max_watts"] is None, "坏字符串 -> 回到「未测量」(None)",
   cfg["gpu_tgp_max_watts"])
ok(cfg["dc_refresh_hz"] is None, "dc_refresh_hz 坏值 -> None", cfg["dc_refresh_hz"])
ok(cfg["pd_low_hz_value"] == 60.0, "数字字符串 -> float", cfg["pd_low_hz_value"])
ok(cfg["power_envelope_watts"] == 300.0, "越界 -> 夹到上限", cfg["power_envelope_watts"])
ok(cfg["gpupick_igpu_tier"] == 2.0, "igpu_tier 数字字符串 -> 数值",
   cfg["gpupick_igpu_tier"])
ok(cfg["poll_seconds"] == 5, "普通数值键照旧纠正", cfg["poll_seconds"])
ok(cfg["notify"] is False, "布尔键非 bool -> 回默认", cfg["notify"])
ok("gpu_tgp_max_watts" in fixed, "被修正的键要报出来")

# 合法的 None 不能被改成默认值（None = 交给自动探测）
cfg2 = {"gpu_tgp_max_watts": None, "gpu_temp_limit_c": None}
config.sanitize(cfg2)
ok(cfg2["gpu_tgp_max_watts"] is None and cfg2["gpu_temp_limit_c"] is None,
   "None 保持 None（自动探测位不被冻结）")

# 数值键仍必须能通过 clamp 的正常路径
cfg3 = {"pd_restore_ratio": 3.0, "idle_dim_level": -5}
config.sanitize(cfg3)
ok(cfg3["pd_restore_ratio"] == 1.0 and cfg3["idle_dim_level"] == 0,
   "范围夹紧仍然有效", cfg3)

# ---------------------------------------------------------------- 2. 自启判定
print("== 2. autostart_enabled：只认自己的快捷方式 ==")
tmp_startup = tempfile.mkdtemp(prefix="lppa_startup_")
_old_env = os.environ.get("APPDATA")
_old_runkey = config.RUN_KEY
_old_exe = config.exe_path
_old_root = config.APP_ROOT
TEST_RUN = r"Software\LPPA_Test_Autostart"
try:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_RUN)
    except Exception:
        pass
    winreg.CloseKey(winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_RUN))
    config.RUN_KEY = TEST_RUN                 # 别读用户真实的 Run 键
    config.exe_path = lambda: None            # 关掉 exe 那条路
    config.APP_ROOT = tmp_startup             # 也别让「静默启动.vbs」兜底命中
    os.environ["APPDATA"] = os.path.join(tmp_startup, "appdata")
    start_dir = os.path.join(os.environ["APPDATA"],
                             r"Microsoft\Windows\Start Menu\Programs\Startup")
    os.makedirs(start_dir, exist_ok=True)

    with open(os.path.join(start_dir, "PowerToys.lnk"), "wb") as f:
        f.write(b"x")
    with open(os.path.join(start_dir, "powershell 定时.ps1"), "wb") as f:
        f.write(b"x")
    ok(config.autostart_enabled() is False,
       "别家带 power 的启动项不算本程序自启（旧实现会误报 True）")

    with open(os.path.join(start_dir, "笔记本电源自适应.lnk"), "wb") as f:
        f.write(b"x")
    ok(config.autostart_enabled() is True, "本程序自己的快捷方式才算")
finally:
    config.RUN_KEY = _old_runkey
    config.exe_path = _old_exe
    config.APP_ROOT = _old_root
    if _old_env is None:
        os.environ.pop("APPDATA", None)
    else:
        os.environ["APPDATA"] = _old_env
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_RUN)
    except Exception:
        pass
    shutil.rmtree(tmp_startup, ignore_errors=True)

# ---------------------------------------------------------------- 3. 计划解析
print("== 3. parse_query：档位名必须挂在自己的设置上 ==")
TXT = """电源方案 GUID: 11111111-1111-1111-1111-111111111111  (测试)

  子组 GUID: 54533251-82be-4824-96c1-47b60b740d00  (处理器电源管理)
    GUID 别名: SUB_PROCESSOR
    电源设置 GUID: 893dee8e-2bef-41e0-89c6-b55d0929964c  (最小处理器状态)
      GUID 别名: PROCTHROTTLEMIN
      当前交流电源设置索引: 0x00000005
      当前直流电源设置索引: 0x00000005
      可能的设置索引: 0
        可能的设置友好名称: 最小值
      可能的设置索引: 1
        可能的设置友好名称: 低
    电源设置 GUID: 94d3a615-a899-4ac5-ae2b-e4d8f634367f  (系统散热方式)
      GUID 别名: SYSCOOLPOL
      当前交流电源设置索引: 0x00000001
      当前直流电源设置索引: 0x00000001
      可能的设置索引: 0
        可能的设置友好名称: 被动
      可能的设置索引: 1
        可能的设置友好名称: 主动
"""
plan = powercfgctl.parse_query(TXT)
pmin = plan.get("PROCTHROTTLEMIN")
pcool = plan.get("SYSCOOLPOL")
ok(pmin is not None and pmin.options.get(1) == "低", "第一个设置的档位名正确",
   pmin.options if pmin else None)
ok(pcool is not None and pcool.options.get(1) == "主动",
   "第二个设置拿到自己的档位名（旧实现是「低」）", pcool.options if pcool else None)
ok(pmin.ac == 5 and pcool.ac == 1, "AC 值解析不受影响")

# ---------------------------------------------------------------- 4. 来源校验
print("== 4. webui：跨站请求必须被挡 ==")
ok(webui._is_loopback("127.0.0.1:8753") and
   webui._is_loopback("http://localhost:8753/") and
   webui._is_loopback("[::1]:8753"), "回环名的各种写法都认")
ok(not webui._is_loopback("http://evil.com") and
   not webui._is_loopback("attacker.com:8753") and
   not webui._is_loopback("null") and
   not webui._is_loopback(""), "外部域名 / file:// / 空 Host 都不认")


class FakeMgr:
    cfg = {"poll_seconds": 5}
    on_notify = None

    def status(self):
        return {"mode": "balanced", "knobs": {}}

    def set_manual(self, key):
        pass


srv = webui.WebServer(FakeMgr(), port=0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def call(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


st, _ = call("GET", "/api/status")
ok(st == 200, "正常 GET 放行（%s）" % st)
st, _ = call("GET", "/api/status", headers={"Origin": "http://evil.com"})
ok(st == 403, "外部 Origin 的 GET 被拒（DNS rebinding 场景，%s）" % st)
st, _ = call("GET", "/api/status", headers={"Host": "attacker.com:%d" % port})
ok(st == 403, "伪造 Host 被拒（%s）" % st)
st, _ = call("POST", "/api/notify", body=json.dumps({"title": "t", "text": "x"}),
             headers={"Content-Type": "text/plain", "Origin": "http://evil.com"})
ok(st == 403, "跨站 POST 被拒（text/plain 简单请求，%s）" % st)
st, body = call("POST", "/api/notify", body=json.dumps({"title": "t", "text": "x"}),
                headers={"Content-Type": "application/json",
                         "Origin": "http://127.0.0.1:%d" % port})
ok(st == 200, "同源 POST 放行（%s %s）" % (st, body[:40]))
st, body = call("POST", "/api/notify", body=json.dumps({"title": "t"}),
                headers={"Content-Type": "application/json"})
ok(st == 200, "不带 Origin 的本地脚本（curl）照旧可用")
srv.shutdown()

# ---------------------------------------------------------------- 5. 体检连坐
print("== 5. run_checks：一项算不出来不连坐 ==")


class BadMgr:
    """proc_max 是坏值（旧实现会让它之后的所有项一起消失）"""

    def status(self):
        return {"scheme_name": "笔记本自适应电源",
                "knobs": {"proc_max": {"value": "abc"}},
                "gpu_eco": {"eco": 0},
                "battery_info": {"health_pct": 88.5}}


keys = {x["key"] for x in checkup.run_checks(BadMgr())["items"]}
ok("plan" in keys, "坏字段之前的项仍在")
ok("gpu_eco" in keys and "battery" in keys, "坏字段之后的项也在（%s）" % sorted(keys))
ok(all(x["ok"] is not None or x["advice"] for x in checkup.run_checks(BadMgr())["items"]),
   "中性项都必须带建议，不能既不判好坏又不给话")

res = checkup.run_checks(None)
sup = [x for x in res["items"] if x["key"] == "startup"][0]
ok(sup["ok"] in (True, False), "启动项检查给明确好坏（旧实现恒为 None）")

# ---------------------------------------------------------------- 6. 启动项白名单
print("== 6. set_startup：只允许清单里真实存在的项 ==")
victim = os.path.join(tempfile.mkdtemp(prefix="lppa_victim_"), "important.txt")
with open(victim, "w") as f:
    f.write("x")
r = checkup.set_startup("dir:" + victim, False)
ok(r["ok"] is False and os.path.exists(victim),
   "清单外的任意路径被拒绝、文件没被改名（%s）" % r.get("msg"))
shutil.rmtree(os.path.dirname(victim), ignore_errors=True)

# ---------------------------------------------------------------- 7. 学习报告
print("== 7. learner.report：disabled 要给旋钮名，不是时间戳 ==")
lr = learner.Learner(path=tempfile.mktemp(suffix=".json"))
lr.data["disabled"] = {"gaming": {"PROCTHROTTLEMAX": 1750000000.0}}
lr.data["disabled_old"] = None
rep = lr.report()
ok(rep["disabled"].get("gaming") == ["PROCTHROTTLEMAX"],
   "新格式（dict）报旋钮名", rep["disabled"])
lr.data["disabled"] = {"quiet": ["PERFEPP"]}
ok(lr.report()["disabled"].get("quiet") == ["PERFEPP"], "旧格式（list）仍兼容")

# ---------------------------------------------------------------- 8. 任务 XML
print("== 8. autostart_task：路径里的 & 必须转义 ==")
tmp_root = tempfile.mkdtemp(prefix="lppa_task_")
_old_root = autostart_task.APP_ROOT
_old_exe2 = config.exe_path
try:
    autostart_task.APP_ROOT = tmp_root
    config.exe_path = lambda: r"C:\Tools & Games\lp.exe"
    p = autostart_task.write_xml()
    ok(p is not None and os.path.isfile(p), "能生成任务 XML")
    raw = open(p, "rb").read()
    ok(raw[:2] == b"\xff\xfe", "UTF-16LE BOM（schtasks 要求）")
    text = raw[2:].decode("utf-16-le")
    ok("&amp;" in text and "Tools & Games" not in text.split("<Command>")[1][:20],
       "& 被转义成 &amp;")
    root = ET.fromstring(raw)      # 带 BOM 的 UTF-16 原件必须能直接解析
    ok(root.tag.endswith("Task"), "XML 结构可解析")
finally:
    autostart_task.APP_ROOT = _old_root
    config.exe_path = _old_exe2
    shutil.rmtree(tmp_root, ignore_errors=True)

print("\n通过 %d，失败 %d" % (PASS, FAIL))
raise SystemExit(1 if FAIL else 0)
