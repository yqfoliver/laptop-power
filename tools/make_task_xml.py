# -*- coding: utf-8 -*-
"""
生成「计划任务」自启用的 XML（UTF-16 编码，schtasks /create /xml 要求）。

为什么需要它：Run 键在本机开机瞬间没能创建进程（boot.log 一行都没有）。
计划任务由 Task Scheduler 服务拉起，可设登录延迟，避开开机高峰与
未就绪环境，是比 Run 键更稳的通道。
"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(HERE, "\u7b14\u8bb0\u672c\u7535\u6e90\u81ea\u9002\u5e94.exe")
OUT = os.path.join(HERE, "LaptopPowerAuto-task.xml")

TPL = """<?xml version="1.0" encoding="UTF-16"?>
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


def main():
    if not os.path.isfile(APP):
        print("找不到 exe:", APP)
        return 1
    xml = TPL.format(cmd=APP, cwd=os.path.dirname(APP))
    # schtasks /create /xml 要求 UTF-16 带 BOM
    with open(OUT, "wb") as f:
        f.write(b"\xff\xfe")
        f.write(xml.encode("utf-16-le"))
    print("已生成:", OUT, os.path.getsize(OUT), "bytes")
    print("导入命令（无需管理员，复制整行到 PowerShell 执行）：")
    print('  schtasks /create /tn "LaptopPowerAuto" /xml "%s" /f' % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
