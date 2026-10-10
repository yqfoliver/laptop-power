"""实机探测：这台机器到底能「读」到哪些充电器能力信息？

只做只读探测，不改任何东西。子进程一律 CREATE_NO_WINDOW（静默底线）。
"""
import subprocess

NO_WIN = 0x08000000


def ps(script, timeout=60):
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=timeout, creationflags=NO_WIN)
        return (p.stdout or "").strip()
    except Exception as e:
        return "ERR: %s" % e


def main():
    print("== 1. root\\wmi 里有没有 UCSI / TypeC / Charger 相关类 ==")
    print(ps(r'''
$n = Get-CimInstance -Namespace root\wmi -List -ErrorAction SilentlyContinue |
     Where-Object { $_.Name -match 'Ucsi|UsbC|TypeC|Charg|PowerDelivery|Pd' } |
     Select-Object -ExpandProperty Name
if ($n) { $n -join "`n" } else { "(无)" }
'''))

    print("\n== 2. 设备管理器里的 UCSI / USB 连接器管理器 ==")
    print(ps(r'''
Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match 'UCSI|USB Connector|Type-C|Power Delivery|PD Controller|Ucm' } |
  ForEach-Object { $_.Name + "  ||  " + $_.DeviceID }
'''))

    print("\n== 3. UCSI 设备实例在注册表里的位置（UcsiControl 需要的路径） ==")
    print(ps(r'''
$p = 'HKLM:\SYSTEM\CurrentControlSet\Enum'
$hits = @()
Get-ChildItem $p -ErrorAction SilentlyContinue | ForEach-Object {
  Get-ChildItem $_.PSPath -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.PSChildName -match 'UCSI|UCM') { $hits += $_.PSPath }
  }
}
if ($hits.Count -gt 0) { $hits -join "`n" } else { "(无 UCSI 枚举节点)" }
'''))

    print("\n== 4. 电池/ACPI 侧能拿到的充电信息 ==")
    print(ps(r'''
Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus -ErrorAction SilentlyContinue
Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue |
  ForEach-Object { "Win32_Battery: " + $_.Name + " | Status=" + $_.BatteryStatus + " | Desc=" + $_.Description }
'''))

    print("\n== 5. SYSTEM_POWER_STATUS（Win32 API 的 AC 信息） ==")
    print(ps(r'''
Add-Type @"
using System;using System.Runtime.InteropServices;
public class P{ [StructLayout(LayoutKind.Sequential)] public struct SPS{
 public byte ACLineStatus; public byte BatteryFlag; public byte BatteryLifePercent;
 public byte SystemStatusFlag; public int BatteryLifeTime; public int BatteryFullLifeTime;}
 [DllImport("kernel32.dll")] public static extern bool GetSystemPowerStatus(out SPS s);}
"@
$s = New-Object P+SPS
[void][P]::GetSystemPowerStatus([ref]$s)
"ACLineStatus=$($s.ACLineStatus) BatteryFlag=$($s.BatteryFlag) Percent=$($s.BatteryLifePercent)"
'''))

    print("\n== 6. 有没有 UcsiControl.exe（MUTT 工具） ==")
    print(ps(r'''
$f = Get-ChildItem C:\ -Filter UcsiControl.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 3 -ExpandProperty FullName
if ($f) { $f -join "`n" } else { "(未找到，UCSI 命令通道走不通)" }
''', timeout=90))

    print("\n== 7. 电源相关注册表里有没有充电器额定值 ==")
    print(ps(r'''
$keys = @(
 'HKLM:\SYSTEM\CurrentControlSet\Control\Power',
 'HKCU:\SOFTWARE\ASUS\ASUS System Control Interface',
 'HKLM:\SOFTWARE\ASUS\ASUS System Control Interface'
)
foreach ($k in $keys) {
  if (Test-Path $k) {
    Get-Item $k | Get-ChildItem -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -match 'Watt|Adapter|Charger|Rating|Supply|Pdo' } |
      ForEach-Object { $_.PSPath + " = " + $_.GetValue('') }
  }
}
"(扫描完毕)"
'''))


if __name__ == "__main__":
    main()
