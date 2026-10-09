# -*- coding: utf-8 -*-
"""
自启加固：补一条「计划任务」通道。

背景（2026-10-08 实测）：Run 键在本机开机瞬间**没能创建进程** ——
boot.log 连一行"被调用"都没有，说明不是程序自己崩了，而是外部压根没把它
拉起来（manifest 是 asInvoker 排除 UAC、cwd 换成 System32 也照样起得来，
Run 键值本身也没问题）。

计划任务由 Task Scheduler 服务拉起，可设登录延迟（避开开机高峰与安全软件
高密度扫描），是比 Run 键更稳的通道。让**程序自己**用 schtasks 建 ——
它运行时不受沙箱限制，用户不用手动敲命令。

全程静默：无窗口、不弹窗、失败只写日志。
"""
import os
import subprocess
import threading
import time

from .config import APP_ROOT

TASK_NAME = "LaptopPowerAuto"
XML_NAME = "LaptopPowerAuto-task.xml"
_CREATE_NO_WINDOW = 0x08000000

_TPL = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>LaptopPowerAuto</Author>
    <Description>\u7b14\u8bb0\u672c\u7535\u6e90\u81ea\u9002\u5e94\uff08\u767b\u5f55\u540e\u5ef6\u8fdf\u542f\u52a8\uff09</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT15S</Delay>
    </LogonTrigger>
  </Triggers>
  <Settings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>false</Hidden>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <Priority>7</Priority>
    <StartWhenAvailable>true</StartWhenAvailable>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <WakeToRun>false</WakeToRun>
  </Settings>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Actions Context="Author">
    <Exec>
      <Command>{cmd}</Command>
      <Arguments>--autostart</Arguments>
      <WorkingDirectory>{cwd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _log(msg):
    try:
        with open(os.path.join(APP_ROOT, "boot.log"), "a", encoding="utf-8") as f:
            f.write("%s  [task] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _run(args, timeout=10):
    try:
        p = subprocess.run(args, creationflags=_CREATE_NO_WINDOW,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        out = (p.stdout or b"").decode("mbcs", "ignore")
        return p.returncode, out.strip()
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e)


def task_exists():
    rc, out = _run(["schtasks", "/query", "/tn", TASK_NAME])
    return rc == 0


def xml_path():
    return os.path.join(APP_ROOT, XML_NAME)


def write_xml():
    from .config import exe_path
    exe = exe_path()
    if not exe:
        return None
    p = xml_path()
    try:
        xml = _TPL.format(cmd=exe, cwd=os.path.dirname(exe))
        with open(p, "wb") as f:
            f.write(b"\xff\xfe")
            f.write(xml.encode("utf-16-le"))
        return p
    except Exception:
        return None


def ensure():
    """任务不存在就建一个。返回 True 表示「已存在或已建好」"""
    try:
        if task_exists():
            return True
        p = write_xml()
        if not p:
            _log("跳过：找不到 exe，无法生成任务 XML")
            return False
        rc, out = _run(["schtasks", "/create", "/tn", TASK_NAME, "/xml", p, "/f"])
        if rc == 0:
            _log("已创建计划任务 %s（登录延迟 15s 启动）" % TASK_NAME)
            return True
        _log("创建计划任务失败 rc=%d out=%s" % (rc, out[:160]))
        return False
    except Exception as e:
        _log("ensure 异常 %s: %s" % (type(e).__name__, e))
        return False


def ensure_async():
    """后台线程里跑，不拖慢启动（schtasks 每次约 200ms）"""
    try:
        t = threading.Thread(target=ensure, name="autostart-task", daemon=True)
        t.start()
        return True
    except Exception:
        return False
