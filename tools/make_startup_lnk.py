# -*- coding: utf-8 -*-
"""
手写构造 .lnk 快捷方式 —— 沙箱禁用了 COM，不能用 WScript.Shell.CreateShortcut。

最小可用结构：
  ShellLinkHeader(0x4C)
  + LinkInfo(VolumeIDAndLocalBasePath)
  + StringData(Unicode: WorkingDir, Arguments)
  + TerminalBlock
不构造 IDList：Windows 仍能靠 LinkInfo 里的本地绝对路径解析目标。
"""
import os
import struct

CLSID = bytes([0x01, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
               0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46])

HAS_LINK_INFO = 0x00000002
HAS_WORKING_DIR = 0x00000010
HAS_ARGUMENTS = 0x00000020
IS_UNICODE = 0x00000080


def _u(s):
    """StringData 里的 Unicode 字符串：2 字节字符数 + UTF-16LE（无终止符）"""
    return struct.pack("<H", len(s)) + s.encode("utf-16-le")


def build(target, args="", workdir="", show_cmd=7):
    flags = HAS_LINK_INFO | IS_UNICODE
    if workdir:
        flags |= HAS_WORKING_DIR
    if args:
        flags |= HAS_ARGUMENTS

    header = (struct.pack("<I", 0x4C) + CLSID
              + struct.pack("<I", flags)
              + struct.pack("<I", 0x20)              # FILE_ATTRIBUTE_ARCHIVE
              + b"\x00" * 24                         # 三个 FILETIME 全 0
              + struct.pack("<I", os.path.getsize(target))
              + struct.pack("<i", 0)                 # icon index
              + struct.pack("<I", show_cmd)          # SW_SHOWMINNOACTIVE
              + struct.pack("<H", 0)                 # hotkey
              + struct.pack("<HII", 0, 0, 0))        # reserved1(H)/2(I)/3(I)
    assert len(header) == 0x4C, len(header)

    # VolumeID：size / DRIVE_FIXED / serial / labelOffset(=末尾，表示无卷标)
    vol_id = struct.pack("<IIII", 0x10, 3, 0, 0x10)
    local_base = target.encode("mbcs") + b"\x00"
    hdr_size = 0x1C
    vol_off = hdr_size
    local_off = vol_off + len(vol_id)
    suffix_off = local_off + len(local_base)
    suffix = b"\x00"                                  # CommonPathSuffix（空串）
    link_info = (struct.pack("<IIIIIII", suffix_off + len(suffix), hdr_size, 1,
                             vol_off, local_off, 0, suffix_off)
                 + vol_id + local_base + suffix)

    strings = b""
    if workdir:
        strings += _u(workdir)
    if args:
        strings += _u(args)

    return header + link_info + strings + struct.pack("<I", 0)


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = os.path.join(here, "\u7b14\u8bb0\u672c\u7535\u6e90\u81ea\u9002\u5e94.exe")
    if not os.path.isfile(app):
        print("\u627e\u4e0d\u5230 exe:", app)
        return 1
    startup = os.path.join(os.environ.get("APPDATA", "."),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
    if not os.path.isdir(startup):
        print("\u542f\u52a8\u6587\u4ef6\u5939\u4e0d\u5b58\u5728:", startup)
        return 1
    lnk = os.path.join(startup, "\u7b14\u8bb0\u672c\u7535\u6e90\u81ea\u9002\u5e94.lnk")
    data = build(app, args="--autostart", workdir=os.path.dirname(app))
    with open(lnk, "wb") as f:
        f.write(data)

    raw = open(lnk, "rb").read()
    checks = {
        "header": raw[:4] == struct.pack("<I", 0x4C),
        "clsid": raw[4:20] == CLSID,
        "target": app.encode("mbcs") in raw,
        "args": "--autostart".encode("utf-16-le") in raw,
        "workdir": os.path.dirname(app).encode("utf-16-le") in raw,
    }
    print("\u5df2\u5199\u5165:", lnk, len(data), "bytes")
    for k, v in checks.items():
        print("   %-8s %s" % (k, "OK" if v else "FAIL"))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
