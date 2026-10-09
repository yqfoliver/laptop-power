# -*- coding: utf-8 -*-
"""把 AI 生成的浣熊 PNG 转成多尺寸 raccoon.ico（纯标准库）

流程：解码 PNG（RGB 8bit 不隔行）→ 按背景色差找圆形徽章边界（顺带裁掉水印）
→ 圆形 alpha 蒙版 → 双线性重采样到各尺寸 → ICO（小尺寸裸 DIB + 256 用 PNG 压缩）
"""
import struct
import sys
import zlib

SRC = "Flat_vector_app_icon__cute_car_2026-10-04T11-33-51.png"
DST = "lp/raccoon.ico"
SIZES = [16, 20, 24, 32, 48, 64, 256]


# ------------------------------------------------------------- PNG 解码
def decode_png(path):
    d = open(path, "rb").read()
    assert d[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, meta = 8, b"", None
    while pos < len(d):
        ln, typ = struct.unpack(">I4s", d[pos:pos + 8])
        body = d[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, bd, ct, cm, fm, il = struct.unpack(">IIBBBBB", body)
            meta = (w, h, bd, ct, il)
        elif typ == b"IDAT":
            idat += body
        pos += 12 + ln
    w, h, bd, ct, il = meta
    assert bd == 8 and il == 0 and ct in (2, 6), "unsupported %s" % (meta,)
    nch = 3 if ct == 2 else 4
    raw = zlib.decompress(idat)
    stride = w * nch
    out = bytearray(w * h * nch)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:      # Sub
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 0xFF
        elif f == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:    # Average
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:    # Paeth
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    # 统一成 RGBA
    if nch == 3:
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            rgba[i * 4:i * 4 + 3] = out[i * 3:i * 3 + 3]
            rgba[i * 4 + 3] = 255
        out = rgba
    return w, h, out


# ------------------------------------------------------------- 几何处理
def bbox_by_bg(w, h, px, tol=30):
    """从中心向外扫描找圆形徽章的精确边界（背景有渐变，用强阈值）"""
    def get(x, y):
        i = (y * w + x) * 4
        return px[i], px[i + 1], px[i + 2], px[i + 3]

    cx, cy = w // 2, h // 2
    corners = [get(3, 3), get(w - 4, 3), get(3, h - 4), get(w - 4, h - 4)]
    bg = tuple(sum(c[i] for c in corners) / 4.0 for i in range(3))

    def is_fg(x, y):
        r, g, b, a = get(x, y)
        return a > 10 and abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > tol * 3

    # 水平/垂直各扫一行列
    x0 = next((x for x in range(cx) if is_fg(x, cy)), 0)
    x1 = next((x for x in range(w - 1, cx, -1) if is_fg(x, cy)), w - 1)
    y0 = next((y for y in range(cy) if is_fg(cx, y)), 0)
    y1 = next((y for y in range(h - 1, cy, -1) if is_fg(cx, y)), h - 1)
    return x0, y0, x1, y1


def circle_mask(x0, y0, x1, y1):
    """返回裁剪后的方形 RGBA，徽章圆外 alpha=0（边缘 1px 抗锯齿）"""
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    side = max(cw, ch)
    # 居中放进 side x side
    ox, oy = (side - cw) // 2, (side - ch) // 2
    out = bytearray(side * side * 4)
    cx = cy = (side - 1) / 2.0
    R = side * 0.47          # 保守半径：确保水印（在 r≈0.6R 外）与边缘伪影全部裁掉
    for y in range(side):
        for x in range(side):
            sx, sy = x - ox, y - oy
            if 0 <= sx < cw and 0 <= sy < ch:
                i = ((y0 + sy) * SRCW + (x0 + sx)) * 4
                j = (y * side + x) * 4
                out[j:j + 4] = SRCPIX[i:i + 4]
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d > R + 1:
                out[(y * side + x) * 4 + 3] = 0
            elif d > R - 1:                      # 抗锯齿边缘
                j = (y * side + x) * 4
                a = out[j + 3]
                if a:
                    t = max(0.0, min(1.0, (R + 1 - d) / 2.0))
                    out[j + 3] = int(a * t)
    return side, out


def resize(side, px, n):
    """区域平均缩放（比双线性更抗锯齿，适合小图标）"""
    scale = side / n
    out = bytearray(n * n * 4)
    for y in range(n):
        fy0, fy1 = y * scale, (y + 1) * scale
        for x in range(n):
            fx0, fx1 = x * scale, (x + 1) * scale
            r = g = b = a = 0.0
            cnt = 0.0
            yy = int(fy0)
            while yy < fy1:
                wgt_y = min(fy1, yy + 1) - max(fy0, yy)
                xx = int(fx0)
                while xx < fx1:
                    wgt = wgt_y * (min(fx1, xx + 1) - max(fx0, xx))
                    i = (yy * side + xx) * 4
                    r += px[i] * wgt
                    g += px[i + 1] * wgt
                    b += px[i + 2] * wgt
                    a += px[i + 3] * wgt
                    cnt += wgt
                    xx += 1
                yy += 1
            j = (y * n + x) * 4
            if cnt:
                # 预乘 alpha 风格收缩，避免边缘发暗
                out[j] = int(b / cnt)
                out[j + 1] = int(g / cnt)
                out[j + 2] = int(r / cnt)
                out[j + 3] = int(a / cnt)
    return out


# ------------------------------------------------------------- ICO 编码
def dib_entry(n, rgba):
    """BITMAPINFOHEADER + 自下而上 BGRA + AND 掩码（全 0）"""
    hdr = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, n * n * 4, 0, 0, 0, 0)
    body = bytearray()
    for y in range(n - 1, -1, -1):
        for x in range(n):
            i = (y * n + x) * 4
            body += bytes((rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]))
    row = ((n + 31) // 32) * 4
    body += bytes(row * n)
    return hdr + bytes(body)


def png_entry(n, rgba):
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    raw = bytearray()
    for y in range(n):
        raw.append(0)
        raw += rgba[y * n * 4:(y + 1) * n * 4]
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    return png


def build_ico(images):
    """images: [(size, bytes, is_png)]"""
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    offset = 6 + 16 * len(images)
    blobs = b""
    for n, data, is_png in images:
        if is_png:
            entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32,
                                   len(data), offset)
        else:
            entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32,
                                   len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


# ------------------------------------------------------------- 主流程
w, h, pix = decode_png(SRC)
SRCW, SRCH, SRCPIX = w, h, pix
x0, y0, x1, y1 = bbox_by_bg(w, h, pix)
print("徽章边界: (%d,%d)-(%d,%d) 尺寸 %dx%d" % (x0, y0, x1, y1, x1 - x0 + 1, y1 - y0 + 1))
side, square = circle_mask(x0, y0, x1, y1)
print("方形化: %dx%d" % (side, side))

images = []
for n in SIZES:
    px = resize(side, square, n)
    if n <= 64:
        images.append((n, dib_entry(n, px), False))
    else:
        images.append((n, png_entry(n, px), True))
    print("  已生成 %dx%d" % (n, n))

ico = build_ico(images)
open(DST, "wb").write(ico)
print("已写出 %s（%d 字节，%d 个尺寸）" % (DST, len(ico), len(images)))

# 预览：导出 32x32 的 PNG 供人工检查
px32 = resize(side, square, 32)


def chunk(t, d):
    c = t + d
    return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
raw = bytearray()
for y in range(32):
    raw.append(0)
    raw += px32[y * 32 * 4:(y + 1) * 32 * 4]
png = chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 6, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
open("_icon_preview.png", "wb").write(b"\x89PNG\r\n\x1a\n" + png)
print("预览: _icon_preview.png")
