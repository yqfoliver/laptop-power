"""更新 Release v5.0 的说明正文（去掉开发机实测口径，补上硬件自适应）。"""
import json
import subprocess

TOK = ""
out = subprocess.run(["git", "credential", "fill"],
                     input="protocol=https\nhost=github.com\n\n",
                     capture_output=True, text=True, timeout=60).stdout or ""
for l in out.splitlines():
    if l.startswith("password="):
        TOK = l[9:].strip()

BODY = """笔记本电源自适应 v5.0 —— **零依赖、完全静默**的 Windows 笔记本电源管理常驻程序。

下载下方 exe，双击即可运行（**不需要管理员、不需要装驱动**）。

## 主要功能

- **五档策略**（游戏 / 办公 / 续航 / 极限省电 / 平衡）+ 按前台程序自动判定
- **离电续航**：独显 ACPI 级断电、降刷新率、限 CPU 上限、亮度分级 —— 层层往下压整机的实际功耗
- **Type-C 弱电源保护**：实时学习这根线的可持续供电上限，逼近上限时逐级压制（限 CPU → 降亮度 → 降刷新率），**绝不让电池倒贴**
- **插电游戏功耗分配**：按实测功耗把 CPU 的瓦数让给独显，并知道什么时候该停
- **核显优先调度**：核显跑得动的程序不唤醒独显（内置分级名单 + 手动试探实测 3D 占用后再固化）
- **面板**：WebView2 网页形态（默认）/ 原生 GDI 窗口；托盘右键切档、开关面板、退出

## 硬件自适应（v5.0 新增）

源码里**不带任何一台机器的标定值**。首次启动在本机现测一遍（独显有无与型号、温度线、屏幕真实存在的刷新率档位、核显档位、厂商通道可用性），
结果存 `hwprofile.json`；游戏档还会按实测独显功耗持续修正上限。没有独显的机器会自动关掉相关功能，厂商接口不可用就不再白试。

想看自己的机器探测到了什么：在程序目录执行 `笔记本电源自适应.exe --hw`。

## 四条硬性底线

1. **完全静默** —— 不弹窗、不黑窗、不气泡，失败只写日志
2. **零依赖、普通权限** —— 不需要管理员，不装驱动
3. **不改系统自带计划** —— 系统「平衡」计划一个字节都不动，只用自建计划
4. **实证优先** —— 一切参数以本机实测为准，不迷信文档与他人数据

## 注意事项

- 未签名 exe 可能被 **Microsoft Defender 误删**（症状：开机不自启、exe 消失、日志为空）。请把程序所在目录加入 Defender 排除项。
- 本程序在华硕（ATKACPI）机型上开发。其它品牌的厂商接口大概率不可用，但**自建计划 / 处理器上限 / 刷新率 / 亮度 / PD 预算**部分仍然有效。复刻指南见仓库内 `用WorkBuddy复刻到其它品牌.md`。
- 附件 `LaptopPowerAuto.exe` 就是主程序（与中文名 `笔记本电源自适应.exe` 内容完全一致；GitHub 会把中文文件名重命名为 `default.exe`，所以统一用英文名发布）。
- **下载直链**：https://github.com/yqfoliver/laptop-power/releases/latest/download/LaptopPowerAuto.exe

## 源码

101 个文件，含 18 套回归测试（约 500 项断言，全部离线运行、不碰真硬件）。构建方式见 README。
"""

r = subprocess.run(
    ["curl", "-sS", "-X", "PATCH",
     "https://api.github.com/repos/yqfoliver/laptop-power/releases/407989979",
     "-H", "Authorization: token " + TOK,
     "-H", "Accept: application/vnd.github+json",
     "-H", "User-Agent: x",
     "-H", "Content-Type: application/json",
     "--data-binary", "@-",
     "-w", "\n%{http_code}"],
    input=json.dumps({"body": BODY}), capture_output=True, text=True, timeout=180)
body, _, code = r.stdout.rpartition("\n")
print("HTTP", code.strip())
