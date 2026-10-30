/* 面板前端行为测试：用最小 DOM 垫片在 node 里真跑 web/index.html 的那段 JS。
 *
 * 为什么不用浏览器：本机容器里 playwright 装不上、Edge 起进程又被沙箱回收，
 * 而这里要验的全是「函数被调用之后 DOM 变成什么样」，不需要真实排版。
 * 垫片只实现页面真正用到的那一小撮 API（getElementById / classList /
 * children / innerHTML / textContent / style / dataset），谁新增了 API 而垫片
 * 没有，测试会当场抛错 —— 不会静默漏测。
 *
 * 用法：node tools/_web_sim.js [web/index.html]
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const HTML = process.argv[2] || path.join(ROOT, "web", "index.html");

let PASS = 0;
const FAILS = [];
function ok(cond, msg, extra) {
  if (cond) { PASS++; return; }
  FAILS.push(msg + (extra === undefined ? "" : "  << " + JSON.stringify(extra)));
  console.log("FAIL: " + msg + (extra === undefined ? "" : "  << " + JSON.stringify(extra)));
}

/* ------------------------------------------------------------ DOM 垫片 */
function mkEl(id) {
  const el = {
    id: id || "",
    tagName: "DIV",
    _text: "",
    _html: "",
    style: {},
    dataset: {},
    children: [],
    hidden: false,
    onclick: null,
    _cls: new Set(),
  };
  Object.defineProperty(el, "textContent", {
    get() { return el._text; },
    set(v) { el._text = v == null ? "" : String(v); },
  });
  Object.defineProperty(el, "className", {
    get() { return Array.from(el._cls).join(" "); },
    set(v) {
      el._cls = new Set(String(v || "").split(/\s+/).filter(Boolean));
    },
  });
  el.classList = {
    add(c) { el._cls.add(c); },
    remove(c) { el._cls.delete(c); },
    contains(c) { return el._cls.has(c); },
    toggle(c, force) {
      const want = force === undefined ? !el._cls.has(c) : !!force;
      if (want) el._cls.add(c); else el._cls.delete(c);
      return want;
    },
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html; },
    set(v) {
      el._html = String(v || "");
      // 只把 <button> 解析成元素（页面靠它们绑事件），div/span 只当文本存着。
      // 属性解析是通用的：class / id / 任意 data-*（转成 dataset 驼峰键）。
      el.children = [];
      const re = /<button([^>]*)>([\s\S]*?)<\/button>/g;
      let m;
      while ((m = re.exec(el._html))) {
        const at = m[1];
        const b = mkEl("");
        b.tagName = "BUTTON";
        const cm = /class="([^"]*)"/.exec(at);
        if (cm) b.className = cm[1];
        const im = /id="([^"]*)"/.exec(at);
        if (im) b.id = im[1];
        const dre = /data-([a-z-]+)="([^"]*)"/g;
        let dm;
        while ((dm = dre.exec(at))) {
          const key = dm[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
          b.dataset[key] = dm[2];
        }
        // 按钮文字（剥掉内层 span 之类）
        b.textContent = m[2].replace(/<[^>]*>/g, "").trim();
        el.children.push(b);
      }
    },
  });
  Object.defineProperty(el, "firstChild", {
    get() { return el.children.length ? el.children[0] : null; },
  });
  el.querySelectorAll = function (sel) {
    if (sel.charAt(0) === ".") {
      const c = sel.slice(1);
      return el.children.filter((x) => x._cls.has(c));
    }
    const want = sel.toUpperCase();
    return el.children.filter((x) => x.tagName === want);
  };
  el.querySelector = function () { return null; };
  el.getBoundingClientRect = function () {
    return { bottom: 400, height: 400, top: 0, left: 0, right: 600, width: 600 };
  };
  el.appendChild = function (c) { el.children.push(c); return c; };
  return el;
}

function buildDocument() {
  const html = fs.readFileSync(HTML, "utf8");
  const els = new Map();
  const idRe = /id="([A-Za-z0-9_:-]+)"/g;
  let m;
  while ((m = idRe.exec(html))) {
    if (!els.has(m[1])) els.set(m[1], mkEl(m[1]));
  }
  const doc = {
    title: "笔记本电源自适应",
    documentElement: mkEl("html"),
    body: mkEl("body"),
    getElementById(id) {
      if (!els.has(id)) { idsMissing.add(id); return null; }
      return els.get(id);
    },
    querySelector(sel) { return sel === ".wrap" ? mkEl("wrap") : null; },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
  const idsMissing = new Set();
  doc.__els = els;
  doc.__idsMissing = idsMissing;
  return doc;
}

const doc = buildDocument();

/* ------------------------------------------------------------ 运行页面脚本 */
const html = fs.readFileSync(HTML, "utf8");
const scripts = [];
const sre = /<script>([\s\S]*?)<\/script>/g;
let sm;
while ((sm = sre.exec(html))) scripts.push(sm[1]);
const main = scripts.find((s) => s.indexOf("function renderBatt") >= 0);
if (!main) { console.log("FAIL: 找不到主脚本（function renderBatt）"); process.exit(1); }

// fetch 桩：payload / 失败模式 / 调用记录都由测试改
const net = {
  // 初值必须是**完整**的一份 payload：页面加载时就会自己 tick() 一次，这次
  // 请求的续体可能在测试中途才落地，把 state 覆盖成这里的内容。给个残缺对象
  // 会让后续断言拿到空 care，误报成「回执丢了」。
  payload: {
    mode: "balanced", mode_label: "平衡", auto: true, foreground: "",
    ac: true, battery_info: {
      percent: 76, charging: true, care: {
        enabled: true, score: 88, target_limit: 80, intent_active: false,
        calib_due: false, charge_limit: { supported: true, value: 80 },
        charge_line: "充电上限 80%（养护）",
      },
    },
    alloc: {}, thermal: {}, fan: {}, gpu_eco: {}, pd: {}, load: {},
    gpupick: { enabled: true, current: {} },
  },
  fail: false, calls: [],
};
function fetchStub(url, opt) {
  net.calls.push({ url: String(url), opt: opt || {} });
  if (net.fail) return Promise.reject(new Error("net down"));
  const p = net.payload;
  const body = (p && p.__routes && p.__routes[String(url)]) || p;
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

const timers = [];
const win = {
  chrome: undefined,
  innerHeight: 900,
  addEventListener() {},
  close() {},
};
const loc = { search: "" };

// 可推进的时钟：chgMsg 的 7 秒有效期靠它测
const clock = { off: 0 };
const realNow = Date.now;
Date.now = () => realNow() + clock.off;

const epilogue = `
;globalThis.__T = {
  setState: function(s){ state = s; },
  getState: function(){ return state; },
  render: render,
  renderBatt: renderBatt,
  renderAlloc: renderAlloc,
  renderCharge: renderCharge,
  renderThermal: renderThermal,
  renderModes: renderModes,
  renderLoad: renderLoad,
  tick: tick,
  chgSet: chgSet,
  fmtDur: fmtDur,
};`;
const fn = new Function(
  "window", "document", "fetch", "setTimeout", "setInterval", "location", "console",
  main + epilogue
);
fn(win, doc, fetchStub, (f, ms) => { timers.push({ f, ms, once: true }); return 0; },
   (f, ms) => { timers.push({ f, ms, once: false }); return 0; }, loc, console);

const T = globalThis.__T;
if (!T) { console.log("FAIL: 页面脚本未暴露测试钩子"); process.exit(1); }

/* ------------------------------------------------------------ 夹具 */
const el = (id) => doc.getElementById(id);

function basePayload(over) {
  const p = {
    mode: "office", mode_label: "办公·跟手", reason: "前台 code.exe",
    ac: true, ac_unstable: false, auto: true, foreground: "Code.exe",
    battery: 76, charging: true,
    panel_font_scale: 1.0,
    load: { state: "轻负载", heavy: false, cpu: 12, disk: 3, net: 1, gpu: 0 },
    busy: 12, cooldown_left: 0,
    alloc: { ok: true, active: false, verdict_cn: "判定中", cpu_w: 12.0, gpu_w: 0.0,
             system_w: 24.0, envelope_w: 110, gpu_tgp_max: 100,
             cpu_temp: 55, gpu_temp: null, gpu_util: null, reason: "", events: [] },
    thermal: { mode: "auto" },
    fan: { fan_cpu: 30, fan_gpu: 30, temp_cpu: 55, perf_label: "平衡" },
    gpu_eco: { eco: 0, mode: "standard", has_dgpu: true },
    gpupick: { enabled: true, probe: null,
               current: { process: "code.exe", path: "C:\\x\\code.exe", mode: null,
                          src: "builtin", note: "", managed: false, stale: false } },
    pd: {},
    battery_info: {
      percent: 76, charging: true, discharging: false, rate_avg_w: 12.0,
      health_pct: 75.7, capacity_mwh: 52000, full_mwh: 69000,
      display_hz: 60, display_changed: false, eta_minutes: 90,
      care: { enabled: true, score: 88, target_limit: 80, intent_active: false,
              calib_due: false, advice_short: "",
              charge_limit: { supported: true, value: 80, mode_cn: "保养" },
              charge_line: "充电上限 80%（养护）", limit_state: { state: "synced" } },
    },
  };
  return Object.assign(p, over || {});
}

function load(p) {
  net.payload = p;
  T.setState(p);
}

/* ------------------------------------------------------------ 1. 五档按钮不被重建 */
console.log("\n== 1. 模式按钮只建一次（每次轮询重建会吃掉点击）==");
load(basePayload());
T.renderModes();
const box = el("modes");
ok(box.children.length === 3, "三档按钮已生成（五并三）", box.children.length);
const firstBtn = box.children[0];
firstBtn.__mark = "keep-me";
T.renderModes();
T.renderModes();
ok(box.children[0] === firstBtn, "再次渲染不重建 DOM（元素身份不变）");
ok(box.children[0].__mark === "keep-me", "旧元素上的标记还在（说明没被替换）");
// 切档后 class 要跟着变，而不是重建
load(basePayload({ mode: "gaming" }));
T.renderModes();
ok(box.children[0]._cls.has("on"), "游戏档按钮点亮");
ok(!box.children[1]._cls.has("on"), "办公档不再点亮");
ok(box.children[0] === firstBtn, "切档也不重建 DOM");

/* ------------------------------------------------------------ 1b. 负载并入头部卡 */
console.log("\n== 1b. 负载状态并入头部（原独立卡已删）==");
load(basePayload({ load: { state: "轻负载", heavy: false, cpu: 12, disk: 3, net: 1, gpu: 5 },
                   busy: 12, cooldown_left: 0 }));
T.renderLoad();
ok(/轻负载/.test(el("loadDetail").textContent), "档位状态名并入明细行开头",
   el("loadDetail").textContent);
ok(el("loadPct").textContent === "12%", "负载百分比照常更新", el("loadPct").textContent);
ok(el("loadBar").style.width === "12%", "负载条宽度照常更新", el("loadBar").style.width);
ok(el("loadDetail").style.color === "", "非重负载不高亮", el("loadDetail").style.color);
load(basePayload({ load: { state: "重负载", heavy: true, cpu: 90, disk: 5, net: 0, gpu: 60 },
                   busy: 90 }));
T.renderLoad();
ok(/已保护/.test(el("loadDetail").textContent), "重负载保护标记保留",
   el("loadDetail").textContent);
ok(el("loadDetail").style.color !== "", "重负载明细行变色提示", el("loadDetail").style.color);

/* ------------------------------------------------------------ 2. 按钮真的会发请求 */
console.log("\n== 2. 按钮点击真的发出 POST ==");
net.calls.length = 0;
box.children[2].onclick();
setTimeoutReal(0, () => {});
ok(net.calls.some((c) => c.url === "/api/mode" && c.opt.method === "POST"),
   "点档位按钮 → POST /api/mode", net.calls.map((c) => c.url));
ok(net.calls.some((c) => c.url === "/api/mode" &&
   JSON.parse(c.opt.body).mode === "battery"), "带上了正确的档位");

/* ------------------------------------------------------------ 3. 充电按钮有绑定 */
console.log("\n== 3. 「本次充满 / 回养护」绑定了 chgSet ==");
ok(typeof el("chgFull").onclick === "function", "chgFull 有 onclick");
ok(typeof el("chgRestore").onclick === "function", "chgRestore 有 onclick");

/* ------------------------------------------------------------ 4. 充电反馈不被轮询擦掉 */
console.log("\n== 4. 充电操作的回执要停住（旧代码下一拍就被覆盖）==");
net.calls.length = 0;
net.payload = basePayload({
  __routes: { "/api/charge_limit": { ok: true, pending: true,
    msg: "已下发 100%，本次允许充满（EC 约 15 分钟内生效）" } },
});
let pending = null;
try { pending = el("chgFull").onclick(); } catch (e) {
  ok(false, "点击 chgFull 抛异常（没绑定时就是这样）：" + e.message);
}
if (!pending) { next2(); } else Promise.resolve(pending).then(() => {
  return new Promise((r) => setImmediate(r));
}).then(() => {
  ok(net.calls.some((c) => c.url === "/api/charge_limit"), "点击发出了 charge_limit 请求");
  const after = el("chgLine").textContent;
  ok(/已下发/.test(after), "回执写进了 chgLine", after);
  // 模拟下一次轮询重画同一张卡
  T.renderBatt();
  ok(/已下发/.test(el("chgLine").textContent), "轮询重画后回执仍在", el("chgLine").textContent);
  // 超过 7 秒后自动让位给常规文案
  clock.off = 8000;
  T.renderBatt();
  ok(!/已下发/.test(el("chgLine").textContent), "7 秒后回执自动消失",
     el("chgLine").textContent);
  clock.off = 0;
  next2();
});

/* ------------------------------------------------------------ 5. 进度条不能留残影 */
function next2() {
  console.log("\n== 5. 读数缺失时进度条要清零（否则数字「—」配满格条）==");
  load(basePayload({ ac: true, alloc: Object.assign(basePayload().alloc,
       { active: true, gpu_w: 58.7, gpu_util: 91, gpu_temp: 71, verdict_cn: "正常分配" }) }));
  T.renderAlloc();
  ok(el("pwGpuBar").style.width !== "0%", "先画出独显功耗条", el("pwGpuBar").style.width);
  load(basePayload({ ac: false, gpu_eco: { eco: 1, mode: "eco", has_dgpu: true },
       alloc: Object.assign(basePayload().alloc, { active: false, gpu_w: null,
       gpu_util: null, gpu_temp: null }) }));
  T.renderAlloc();
  ok(el("pwGpuBar").style.width === "0%", "独显断电后条被清空", el("pwGpuBar").style.width);
  ok(el("pwGpu").textContent === "已断电", "独显一栏写「已断电」", el("pwGpu").textContent);

  /* -------------------------------------------------------- 6. 三态不再一律「核显模式」 */
  console.log("\n== 6. 电池卡说清独显到底在哪一态 ==");
  load(basePayload({ ac: false, gpu_eco: { eco: 1, mode: "eco", has_dgpu: true } }));
  T.renderBatt();
  ok(/核显模式/.test(el("battDetail").textContent), "eco 态 → 核显模式",
     el("battDetail").textContent);
  load(basePayload({ ac: false, gpu_eco: { eco: 0, mode: "standard", has_dgpu: true } }));
  T.renderBatt();
  ok(!/核显模式/.test(el("battDetail").textContent), "standard 态（独显在跑）不许说核显模式",
     el("battDetail").textContent);
  load(basePayload({ ac: false, gpu_eco: { eco: 0, mode: "forced_standard", has_dgpu: true } }));
  T.renderBatt();
  ok(/外接屏/.test(el("battDetail").textContent), "外接屏强制点亮要标出来",
     el("battDetail").textContent);

  /* -------------------------------------------------------- 7. 离电不等于非游戏档 */
  console.log("\n== 7. 离电但分配器在工作时，不许写成「非游戏档」==");
  load(basePayload({ ac: false, alloc: Object.assign(basePayload().alloc,
       { active: true, verdict_cn: "正常分配", cpu_w: 20, gpu_w: 40 }) }));
  T.renderAlloc();
  ok(el("allocVerdict").textContent === "正常分配", "离电显示真实判定",
     el("allocVerdict").textContent);
  load(basePayload({ ac: true, alloc: Object.assign(basePayload().alloc,
       { active: false, verdict_cn: "判定中" }) }));
  T.renderAlloc();
  ok(el("allocVerdict").textContent === "非游戏档", "未分配时才写「非游戏档」",
     el("allocVerdict").textContent);

  /* -------------------------------------------------------- 8. AC 确认中 */
  console.log("\n== 8. AC 防抖窗口内在界面上有交代 ==");
  load(basePayload({ ac: true, ac_unstable: true, charging: false }));
  T.renderBatt();
  ok(/确认中/.test(el("battState").textContent), "插拔确认中要标出来",
     el("battState").textContent);
  load(basePayload({ ac: true, ac_unstable: false, charging: true }));
  T.renderBatt();
  ok(!/确认中/.test(el("battState").textContent), "确认完成后不再显示",
     el("battState").textContent);

  /* -------------------------------------------------------- 9. 秒数取整 */
  console.log("\n== 9. 秒数不许出现 3.7999999 秒 ==");
  ok(T.fmtDur(45.6) === "46 秒", "45.6 秒 → 46 秒", T.fmtDur(45.6));
  ok(T.fmtDur(undefined) === "—", "缺值给 —", T.fmtDur(undefined));

  next3();
}

/* ------------------------------------------------------------ 10. 断连要有提示 */
function next3() {
  console.log("\n== 10. 后端挂了面板不能假装没事 ==");
  const good = basePayload();
  net.payload = good;
  return T.tick().then(() => {
    ok(!/未连接/.test(el("fg").textContent), "正常时不显示未连接", el("fg").textContent);
    net.fail = true;
    return T.tick();
  }).then(() => {
    ok(/未连接/.test(el("fg").textContent), "请求失败 → 明确提示未连接",
       el("fg").textContent);
    ok(el("fg").style.color === "var(--amber)", "提示是告警色", el("fg").style.color);
    net.fail = false;
    return T.tick();
  }).then(() => {
    ok(!/未连接/.test(el("fg").textContent), "恢复后提示消失（自愈）", el("fg").textContent);
    ok(el("fg").style.color === "", "颜色复位", el("fg").style.color);
    next4();
  }).catch((e) => { ok(false, "断连测试抛异常：" + e); next4(); });
}

/* ------------------------------------------------------------ 11. 静态一致性 */
function next4() {
  console.log("\n== 11. 静态一致性（未定义变量 / 死函数 / 路由）==");
  ok(doc.__idsMissing.size === 0, "JS 引用的 id 全都在 HTML 里存在",
     Array.from(doc.__idsMissing));

  const cssVars = new Set();
  const vre = /--([a-z0-9-]+)\s*:/g;
  let vm;
  while ((vm = vre.exec(html))) cssVars.add(vm[1]);
  const used = new Set();
  const ure = /var\(--([a-z0-9-]+)/g;
  while ((vm = ure.exec(html))) used.add(vm[1]);
  const undef = Array.from(used).filter((v) => !cssVars.has(v));
  ok(undef.length === 0, "var(--x) 都已定义", undef);

  // 定义了却从未被引用的顶层函数 = 死代码（chgSet 就是这么漏了绑定）
  const defs = [];
  const dre = /^function\s+([A-Za-z_$][\w$]*)\s*\(/gm;
  let dm;
  while ((dm = dre.exec(main))) defs.push(dm[1]);
  const dead = defs.filter((n) => {
    const re = new RegExp("[^\\w$.]" + n + "\\s*\\(", "g");
    let cnt = 0;
    let mm;
    while ((mm = re.exec(main))) cnt++;
    return cnt <= 1;   // 只有定义处那一次
  });
  ok(dead.length === 0, "没有从未被调用的函数", dead);

  // 前端调的后端路由必须真的存在
  const py = fs.readFileSync(path.join(ROOT, "lp", "webui.py"), "utf8");
  const routes = new Set();
  const rre = /"\/api\/([a-z_]+)"/g;
  let rm;
  while ((rm = rre.exec(py))) routes.add(rm[1]);
  const called = new Set();
  const cre = /(?:\/api\/([a-z_]+))/g;
  let cm;
  while ((cm = cre.exec(main))) called.add(cm[1]);
  const ghost = Array.from(called).filter((r) => !routes.has(r));
  ok(ghost.length === 0, "前端调用的 /api/* 后端都有", ghost);

  // 必须成对出现的防护
  ok(/AbortController/.test(main), "请求带超时（AbortController）");
  ok(/__panel_clamped\s*\?\s*"auto"\s*:\s*"hidden"/.test(main),
     "被钳住/解除钳住都要处理 overflowY");
  ok(/box\.children\.length\s*!==\s*MODES\.length/.test(main),
     "模式按钮按长度判断后只建一次");
  ok(/chgMsgUntil/.test(main), "充电回执有有效期机制");

  finish();
}

function finish() {
  console.log("\n" + "=".repeat(52));
  if (FAILS.length) {
    console.log("失败 " + FAILS.length + " 项 / 通过 " + PASS + " 项");
    process.exit(1);
  }
  console.log("全部通过：" + PASS + " 项");
}

// 让 promise 链有机会跑完的小工具（node 里有 setImmediate）
function setTimeoutReal(_ms, f) { setImmediate(f); }
