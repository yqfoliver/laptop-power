# -*- coding: utf-8 -*-
"""探测两件事：
1) PDH 的 GPU Engine 计数器能否按进程读到核显 / 独显的 3D 利用率
2) HKCU\\...\\UserGpuPreferences 的现状（Windows 图形偏好）
"""
import winreg
import subprocess


def gpu_engine_probe():
    print("=" * 60)
    print("1) PDH GPU Engine 计数器探测")
    print("=" * 60)
    try:
        import win32pdh  # type: ignore
    except Exception as e:
        print("  win32pdh 不可用:", e)
        return
    try:
        # 枚举 GPU Engine 下的所有实例
        items = win32pdh.EnumObjectItems(None, None, "GPU Engine", -1)
        insts = items[-1] if items else []
        print("  实例数:", len(insts))
        for s in insts[:14]:
            print("   ", s)
        if not insts:
            print("  (无实例：本机 PDH 不暴露 GPU Engine)")
            return
        # 取第一个实例的利用率
        q = win32pdh.OpenQuery()
        try:
            path = win32pdh.MakeCounterPath((None, "GPU Engine", insts[0], None, -1,
                                             "Utilization Percentage"))
            h = win32pdh.AddCounter(q, path)
            win32pdh.CollectQueryData(q)
            import time
            time.sleep(0.6)
            win32pdh.CollectQueryData(q)
            v = win32pdh.GetFormattedCounterValue(h, win32pdh.PDH_FMT_DOUBLE)[1]
            print("  样例读数:", insts[0], "=", round(v, 2))
        except Exception as e:
            print("  读数失败:", type(e).__name__, e)
        finally:
            win32pdh.CloseQuery(q)
    except Exception as e:
        print("  枚举失败:", type(e).__name__, e)


def reg_probe():
    print()
    print("=" * 60)
    print("2) UserGpuPreferences 现状")
    print("=" * 60)
    KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY)
        n = winreg.QueryInfoKey(k)[1]
        print("  已有条目:", n)
        for i in range(n):
            name, val, _ = winreg.EnumValue(k, i)
            print("   ", name, "=", val)
        winreg.CloseKey(k)
    except FileNotFoundError:
        print("  键不存在（尚未设置过图形偏好）→ 可自行创建")
    except Exception as e:
        print("  读取失败:", type(e).__name__, e)

    # 显卡清单
    print()
    print("--- 显示适配器 ---")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | "
             "Select-Object Name,AdapterCompatibility | Format-Table -Auto"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=30)
        print(out.stdout.strip()[:900])
    except Exception as e:
        print("  查询失败:", type(e).__name__, e)


if __name__ == "__main__":
    gpu_engine_probe()
    reg_probe()
