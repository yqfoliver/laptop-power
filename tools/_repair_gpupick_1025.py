# -*- coding: utf-8 -*-
"""2026-10-25 gpupick 误禁用事故修复（一次性）。

事故链：上轮实例被强杀 → GPU Eco 停在 1（独显断电）→ 重启探测只见核显
→ has_dgpu=false 触发「纯核显机」分支单向关闭 alloc/eco/gpupick → 面板
「用独显」点击后端生效、界面停在「未启用」→ 用户以为按钮坏了，且
workbuddy.exe / explorer.exe 被误钉进注册表 GpuPreference=2。

本脚本（按序）：
1. 优雅退出运行中实例（WM_COMMAND 1006）
2. GPU Eco 唤醒独显（set(0)，回读校验）并等 WMI 重新枚举出 4060
3. gpupick.json：摘除 workbuddy/explorer 误钉 + 注册表还原（orig=null=删除）
4. config.json：alloc_enabled / gpu_eco_auto / gpupick_enabled 恢复 True
5. 删 hwprofile.json（下一轮启动全量重探，PROFILE_VERSION 已升 2）
6. 覆盖新 exe（WinError 32 重试）→ 拉起 → 验证 → taskkill 收尾
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time

u = ctypes.windll.user32
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

EXE = os.path.join(ROOT, "笔记本电源自适应.exe")
SRC = os.path.join(ROOT, "dist", "笔记本电源自适应.exe")


def stop_instance():
    h = u.FindWindowW("LaptopPowerTrayWin", None)
    print("旧实例托盘 hwnd:", h)
    if h:
        u.PostMessageW(h, 0x0111, 1006, 0)          # WM_COMMAND / MENU_QUIT
        for _ in range(15):
            time.sleep(1)
            if not u.FindWindowW("LaptopPowerTrayWin", None):
                break
        print("退出后 hwnd:", u.FindWindowW("LaptopPowerTrayWin", None))


def wake_dgpu():
    from lp.gpueco import GpuEco
    ge = GpuEco()
    print("eco 现状:", ge.read())
    if ge.read() == 1:
        ok = ge.set(0)
        print("eco 唤醒写入:", ok, "回读:", ge.read())
    # 等 WMI 重新枚举出独显（最长 12s）
    from lp.hwprofile import video_controllers, _is_dgpu
    for i in range(12):
        names = video_controllers()
        dg = [n for n in names if _is_dgpu(n)]
        if dg:
            print("第 %d 秒独显重新可见: %s" % (i + 1, dg))
            return True
        time.sleep(1)
    print("!! 独显 12s 内未重新枚举:", video_controllers())
    return False


def repair_gpupick():
    from lp.gpupick import GpuPick, _data_path
    gp = GpuPick()
    victims = ["workbuddy.exe", "explorer.exe"]
    orig = gp.data.get("orig") or {}
    for exe, mode in list((gp.data.get("apps") or {}).items()):
        if exe.lower() in victims:
            path = None
            for op in orig:
                if os.path.basename(op or "").lower() == exe.lower():
                    path = op
                    break
            print("摘除误钉:", exe, "注册表路径:", path)
            gp.data["apps"].pop(exe, None)
            if path:
                gp.reg_restore(path)
                gp.data.get("orig", {}).pop(path, None)
    gp._save()
    print("gpupick.json 现存 apps:", sorted(gp.data.get("apps") or {}))


def repair_config():
    from lp import config
    cfg = config.load()
    for k in ("alloc_enabled", "gpu_eco_auto", "gpupick_enabled"):
        print("config %s: %r -> True" % (k, cfg.get(k)))
        cfg[k] = True
    config.save(cfg)


def copy_exe():
    for i in range(5):
        try:
            shutil.copy2(SRC, EXE)
            print("已覆盖 exe: %.2f MB" % (os.path.getsize(EXE) / 1e6))
            return True
        except PermissionError as e:
            print("覆盖失败(第%d次)：%s，3s 重试" % (i + 1, e))
            time.sleep(3)
    return False


def verify_and_kill():
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    st = None
    for i in range(30):
        time.sleep(1)
        try:
            st = json.load(op.open("http://127.0.0.1:8753/api/status", timeout=3))
            if "battery_info" in st:
                break
        except Exception:
            pass
    if not st:
        print("!! 面板未就绪")
        return
    gp = json.load(op.open("http://127.0.0.1:8753/api/gpupick", timeout=3))
    hw = json.load(open(os.path.join(ROOT, "hwprofile.json"), encoding="utf-8"))
    p = hw.get("profile") or {}
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    checks = [
        ("gpupick enabled", gp.get("enabled") is True),
        ("hwprofile has_dgpu", p.get("has_dgpu") is True),
        ("hwprofile dgpu_name 含 4060", "4060" in (p.get("dgpu_name") or "")),
        ("config 三键 True",
         all(cfg.get(k) is True for k in
             ("alloc_enabled", "gpu_eco_auto", "gpupick_enabled"))),
        ("误钉已清", "workbuddy.exe" not in (gp.get("apps") or {})
         and "explorer.exe" not in (gp.get("apps") or {})),
    ]
    for name, okk in checks:
        print(("PASS " if okk else "FAIL ") + name)
    subprocess.run(["taskkill", "/F", "/IM", "笔记本电源自适应.exe", "/T"],
                   capture_output=True)
    print("验证实例已收尾（常驻请双击 exe）")


def main():
    stop_instance()
    if not wake_dgpu():
        print("!! 独显唤不醒，中止（避免带着错误档案继续）")
        return
    repair_gpupick()
    repair_config()
    hp = os.path.join(ROOT, "hwprofile.json")
    if os.path.isfile(hp):
        os.remove(hp)
        print("已删除 hwprofile.json（下轮全量重探）")
    if not copy_exe():
        return
    DETACHED, NEW_GROUP = 0x00000008, 0x00000200
    subprocess.Popen([EXE], cwd=ROOT,
                     creationflags=DETACHED | NEW_GROUP, close_fds=True)
    print("已拉起新实例")
    verify_and_kill()


if __name__ == "__main__":
    main()
