# -*- coding: utf-8 -*-
"""阶梯加压，测出「当前电源到底能长期供多少瓦」（只读采样 + 可控合成负载）。

原理：插电时 适配器输出 ≈ 整机功耗 + 电池充电功率（-rate）。
      负载逐步加大，充电功率会被挤到 0；再往上电池就开始放电（补电）。
      ⇒ 充电刚好为 0 时的整机功耗，就是这块电源的可持续上限。

负载：
  CPU 段：多进程浮点烧核（把 SoC 拉到接近 PPT）
  GPU 段：Edge 全屏跑 WebGL 片元着色器（不锁帧），把独显拉起来

用法：
    python tools/pd_ramp.py            # 全程约 3 分钟
    python tools/pd_ramp.py cpu        # 只跑 CPU 段
"""
import multiprocessing as mp
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from lp import battery, power, nvmlctl  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
WEBGL = os.path.join(HERE, "tools", "_gpu_stress.html")
OVERHEAD = 12.0      # 屏幕/风扇/SSD/VRM 损耗等固定开销（与 power.total_w 同口径）
STEP = 1.5


# ------------------------------------------------------------------ 负载
def _burn(_):
    end = time.time() + 600
    x = 1.0000001
    while time.time() < end:
        for _ in range(200000):
            x = x * 1.0000001 + 0.0000001
    return x


def start_cpu(n=16):
    ps = []
    for _ in range(n):
        p = mp.Process(target=_burn, args=(0,))
        p.daemon = True
        p.start()
        ps.append(p)
    return ps


def stop_cpu(ps):
    for p in ps:
        try:
            p.terminate()
        except Exception:
            pass
    for p in ps:
        try:
            p.join(2)
        except Exception:
            pass


_WEBGL_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}
canvas{display:block;width:100vw;height:100vh}</style></head><body>
<canvas id="c"></canvas><script>
const c=document.getElementById('c');const gl=c.getContext('webgl');
c.width=2560;c.height=1440;
const vs=gl.createShader(gl.VERTEX_SHADER);
gl.shaderSource(vs,'attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}');gl.compileShader(vs);
const fs=gl.createShader(gl.FRAGMENT_SHADER);
gl.shaderSource(fs,`precision highp float;uniform float t;uniform vec2 r;
void main(){vec2 u=gl_FragCoord.xy/r;float s=0.0;vec2 z=u*3.0-1.5;
for(int i=0;i<48;i++){z=vec2(z.x*z.x-z.y*z.y,2.0*z.x*z.y)+vec2(sin(t)*0.4,cos(t)*0.4);
s+=exp(-length(z)*2.0);}gl_FragColor=vec4(s*0.4,fract(s*0.7),fract(s*0.13+t),1.0);}`);
gl.compileShader(fs);const p=gl.createProgram();gl.attachShader(p,vs);gl.attachShader(p,fs);gl.linkProgram(p);gl.useProgram(p);
const b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);
gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
const l=gl.getAttribLocation(p,'p');gl.enableVertexAttribArray(l);gl.vertexAttribPointer(l,2,gl.FLOAT,false,0,0);
const tu=gl.getUniformLocation(p,'t'),ru=gl.getUniformLocation(p,'r');
gl.uniform2f(ru,c.width,c.height);
function f(ms){gl.uniform1f(tu,ms*0.001);gl.drawArrays(gl.TRIANGLES,0,3);requestAnimationFrame(f);}
requestAnimationFrame(f);
</script></body></html>"""


def start_gpu():
    try:
        with open(WEBGL, "w", encoding="utf-8") as f:
            f.write(_WEBGL_HTML)
    except Exception as e:
        print("  写 WebGL 页面失败: %r" % e)
        return None
    try:
        return subprocess.Popen(
            [EDGE, "--disable-frame-rate-limit", "--disable-gpu-vsync",
             "--disable-features=CalculateNativeWinOcclusion",
             "--start-fullscreen", "--app=file:///" + WEBGL.replace("\\", "/")],
            creationflags=0x00000008 | 0x00000200)
    except Exception as e:
        print("  启动 Edge 失败: %r" % e)
        return None


def stop_gpu(p):
    if not p:
        return
    try:
        p.terminate()
    except Exception:
        pass
    subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"], capture_output=True)


# ------------------------------------------------------------------ 采样
def sample(bat, pw, secs, label, rows):
    t0 = time.time()
    while time.time() - t0 < secs:
        b = bat.sample(force=True)
        s = pw.sample(want_gpu=True)
        rate = b.get("rate_w")
        soc = s.get("soc_w") or 0.0
        gpu = s.get("gpu_w") or 0.0
        tot = soc + gpu + OVERHEAD
        rows.append((label, tot, rate, soc, gpu,
                     s.get("cpu_temp"), s.get("gpu_temp"), s.get("gpu_util")))
        print("  %-6s 整机%6.1fW (soc %5.1f + gpu %5.1f)  电池%7.2fW  CPU %2.0f℃ GPU %2.0f℃"
              % (label, tot, soc, gpu, rate if rate is not None else 0.0,
                 s.get("cpu_temp") or 0, s.get("gpu_temp") or 0))
        time.sleep(STEP)


def summary(rows):
    print()
    print("=" * 72)
    print("分阶段汇总（整机 = SoC + 独显 + %.0fW 平台开销）" % OVERHEAD)
    print("=" * 72)
    print("%-8s %10s %10s %10s %10s" % ("阶段", "整机均值", "电池均值", "适配器≈", "峰值整机"))
    labels = []
    for r in rows:
        if r[0] not in labels:
            labels.append(r[0])
    for lb in labels:
        sub = [r for r in rows if r[0] == lb]
        tot = sum(r[1] for r in sub) / len(sub)
        rate = sum((r[2] or 0) for r in sub) / len(sub)
        mx = max(r[1] for r in sub)
        supply = tot - rate            # rate 负=充电：适配器 = 整机 + 充电
        print("%-8s %9.1fW %9.2fW %9.1fW %9.1fW" % (lb, tot, rate, supply, mx))
    print()
    print("说明：适配器≈ = 整机 - 电池rate（rate 为负=在充电）。")
    print("      取负载最高、且电池仍未放电那一档的「整机」即为电源可持续上限。")


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    bat = battery.BatteryMonitor()
    pw = power.PowerMonitor(nvml=nvmlctl.Nvml())
    print("阶梯加压测试：先空载 → CPU 满载 → +GPU 满载 → 卸载")
    rows = []
    sample(bat, pw, 15, "空载", rows)
    if only != "gpu":
        print("[CPU 满载]")
        ps = start_cpu(16)
        sample(bat, pw, 45, "CPU", rows)
        if only != "cpu":
            print("[CPU + GPU 满载]")
            g = start_gpu()
            sample(bat, pw, 60, "CPU+GPU", rows)
            stop_gpu(g)
        stop_cpu(ps)
    elif only == "gpu":
        print("[GPU 满载]")
        g = start_gpu()
        sample(bat, pw, 60, "GPU", rows)
        stop_gpu(g)
    print("[卸载]")
    sample(bat, pw, 15, "卸载", rows)
    summary(rows)


if __name__ == "__main__":
    mp.freeze_support()
    main()
