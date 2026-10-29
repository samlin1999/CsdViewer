// 自動更新（watch）的回歸測試：
//
//   node tests/test_watch.js [要測的 index.html]
//
// 直接把 index.html 裡的 <script> 抽出來，在 vm 沙箱裡用假的 DOM、假的 fetch
// 與可控的時鐘跑一次，所以測到的是產品裡真正那份 JS，不是另外重寫的版本。
//
// 最重要的一條是「寫檔期間不重載」：agent 改一個版面通常是連續好幾次寫檔，
// 中途重載會畫出看不到的中間狀態，在 Windows 上還會開檔擋住對方的原子替換。
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const target = process.argv[2] ||
  path.join(__dirname, "..", "csdview", "web", "index.html");
const html = fs.readFileSync(target, "utf8");
const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");

// ---- 最小 DOM stub ----------------------------------------------------
const values = { autoReload: true, anim: "", timeline: 0, frameNum: 0,
                 zoom: "1", bg: "checker", annotate: false, particles: true,
                 fullbounds: true, search: "", filter: "" };
function makeEl(id) {
  return new Proxy({
    id, style: {}, classList: { add(){}, remove(){}, toggle(){} },
    children: [], appendChild(){}, addEventListener(){}, removeAttribute(){},
    setAttribute(){}, focus(){}, scrollIntoView(){},
    get value() { return String(values[id] ?? ""); },
    set value(v) { values[id] = v; },
    get checked() { return !!values[id]; },
    set checked(v) { values[id] = v; },
    innerHTML: "", textContent: "", hidden: true, disabled: false,
    min: 0, max: 0,
  }, { get: (t, k) => (k in t ? t[k] : undefined), set: (t, k, v) => (t[k] = v, true) });
}
const els = new Map();
const document = {
  getElementById: (id) => { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  createElement: () => makeEl("tmp"),
  addEventListener(){}, hidden: false, onkeydown: null,
  querySelectorAll: () => [],
};

// ---- 可控的計時器 ------------------------------------------------------
let now = 0;
const timers = new Map();
let nextTimer = 1;
function setInterval_(fn, ms) { const id = nextTimer++; timers.set(id, { fn, ms, next: now + ms }); return id; }
function clearInterval_(id) { timers.delete(id); }
function setTimeout_(fn, ms) { const id = nextTimer++; timers.set(id, { fn, ms, next: now + ms, once: true }); return id; }
function clearTimeout_(id) { timers.delete(id); }

// ---- 假的檔案 + 伺服器 -------------------------------------------------
let file = { mtime: 1000, size: 500, boot: 7 };
const log = [];
async function fetchStub(url) {
  log.push({ t: now, url });
  if (url.startsWith("/api/stat")) {
    return { ok: true, json: async () => ({ mtime: file.mtime, size: file.size, boot: file.boot }) };
  }
  if (url.startsWith("/api/info")) {
    return { ok: true, json: async () => ({
      name: "T.csd", size: [100, 100], duration: 10, fps: 60, defaultAnimation: "",
      animations: [], nodeCount: 3, mtime: file.mtime, size2: 0, boot: file.boot,
      ...( { size: [100, 100] } ), ...( {} ),
      // info 也帶回指紋（伺服器實際行為）
    }) };
  }
  if (url.startsWith("/api/files")) return { ok: true, json: async () => ({ files: [] }) };
  if (url.startsWith("/api/tree")) return { ok: true, json: async () => ({ nodes: [] }) };
  return { ok: true, json: async () => ({}) };
}

const sandbox = {
  document, fetch: fetchStub, console,
  setInterval: setInterval_, clearInterval: clearInterval_,
  setTimeout: setTimeout_, clearTimeout: clearTimeout_,
  Image: class { set src(v) { this.onload && this.onload(); } },
  encodeURIComponent, decodeURIComponent, URLSearchParams,
  Date: { now: () => now },
  requestAnimationFrame: (fn) => setTimeout_(fn, 16),
  performance: { now: () => now },
  location: { href: "http://x/" },
  window: {},
};
sandbox.window = sandbox;
vm.createContext(sandbox);
// const/let 在 vm 裡是語彙繫結，不會掛到 global，補一行把要用的東西導出來
vm.runInContext(js + String.fromCharCode(10) +
  ";globalThis.__x = { state, startWatching, checkForChanges, reloadCurrent };", sandbox);
const X = sandbox.__x;

// info 的 mtime/size/boot 必須跟 stat 一致，否則重載後會被判定又變了
const origFetch = sandbox.fetch;
sandbox.fetch = async (url) => {
  const r = await origFetch(url);
  if (url.startsWith("/api/info")) {
    const j = await r.json();
    return { ok: true, json: async () => ({ ...j, mtime: file.mtime, size: file.size, boot: file.boot }) };
  }
  return r;
};

// ---- 讓時間往前走 ------------------------------------------------------
async function advance(ms) {
  const target = now + ms;
  while (true) {
    let due = null, dueId = null;
    for (const [id, t] of timers) if (t.next <= target && (due === null || t.next < due.next)) { due = t; dueId = id; }
    if (!due) break;
    now = due.next;
    if (due.once) timers.delete(dueId); else due.next = now + due.ms;
    try { await due.fn(); } catch (e) { console.log("timer error:", e.message); }
    await new Promise(r => setImmediate(r));
  }
  now = target;
}

// ---- 情境 --------------------------------------------------------------
(async () => {
  const S = X;
  S.state.file = "T.csd";
  S.state.info = { name: "T.csd", size: [100, 100], duration: 10, animations: [],
                   nodeCount: 3, mtime: file.mtime, size: file.size, boot: file.boot };
  S.state.start = 0; S.state.end = 10; S.state.frame = 0;
  S.startWatching();

  const count = (pat, from = 0) => log.filter(e => e.t >= from && e.url.startsWith(pat)).length;

  // 情境一：agent 連續寫 8 次，每 150ms 一次（總共 1.2 秒）
  const t0 = now;
  await advance(2000);                       // 先讓它進入穩定輪詢
  const burstStart = now;
  for (let i = 0; i < 8; i++) {
    file = { mtime: file.mtime + 1, size: file.size + 3, boot: file.boot };
    await advance(150);
  }
  const burstEnd = now;
  const infoDuringBurst = count("/api/info", burstStart);
  await advance(3000);                       // 寫完之後
  const infoTotal = count("/api/info", burstStart);

  const pass = [];
  const check = (name, cond, extra = "") => {
    pass.push(cond); console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}${extra ? "   " + extra : ""}`);
  };
  console.log("情境一：連續 8 次寫檔（每 150ms），寫完後靜置");
  check("寫檔期間不重載", infoDuringBurst === 0, `期間 /api/info = ${infoDuringBurst}`);
  check("寫完後只重載一次", infoTotal === 1, `總共 /api/info = ${infoTotal}`);
  check("重載後看到的是最後的版本",
        S.state.info.mtime === file.mtime && S.state.info.size === file.size,
        `state=${S.state.info.mtime}/${S.state.info.size} file=${file.mtime}/${file.size}`);

  // 情境二：單次修改，多久會看到
  const before = now, infoBefore = count("/api/info");
  file = { mtime: file.mtime + 1, size: file.size + 3, boot: file.boot };
  let seenAt = null;
  for (let i = 0; i < 60 && seenAt === null; i++) {
    await advance(100);
    if (count("/api/info") > infoBefore) seenAt = now - before;
  }
  console.log("情境二：單次修改");
  check("單次修改會被抓到", seenAt !== null, seenAt !== null ? `延遲 ${seenAt}ms` : "");
  check("延遲在 2.5 秒內", seenAt !== null && seenAt <= 2500, `${seenAt}ms`);

  // 情境三：一直寫個不停，不能無限等
  const t3 = now, info3 = count("/api/info");
  for (let i = 0; i < 40; i++) {             // 持續 12 秒不停寫
    file = { mtime: file.mtime + 1, size: file.size + 3, boot: file.boot };
    await advance(300);
  }
  console.log("情境三：持續 12 秒不停寫檔");
  check("超過上限會先畫一次，不會永遠等下去", count("/api/info") > info3,
        `期間重載 ${count("/api/info") - info3} 次`);

  // 情境四：關掉自動更新就不該有任何請求
  values.autoReload = false;
  const t4 = now;
  file = { mtime: file.mtime + 1, size: file.size + 3, boot: file.boot };
  await advance(6000);
  console.log("情境四：關掉自動更新");
  check("關掉後不再重載", count("/api/info", t4) === 0);
  values.autoReload = true;

  // 情境五：分頁在背景不輪詢
  await advance(4000);
  document.hidden = true;
  const t5 = now;
  await advance(6000);
  console.log("情境五：分頁切到背景");
  check("背景時不打 /api/stat", count("/api/stat", t5) === 0, `期間 ${count("/api/stat", t5)} 次`);
  document.hidden = false;

  const ok = pass.filter(Boolean).length;
  console.log(`\n${ok}/${pass.length} 通過`);
  process.exit(ok === pass.length ? 0 : 1);
})();
