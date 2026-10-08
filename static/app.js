"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body && typeof opts.body !== "string" && !(opts.body instanceof Blob) ? JSON.stringify(opts.body) : opts.body });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};

const STAGE_INFO = {
  asr: ["Apple ASR", "裝置端轉錄，取得每個字的時間碼"],
  reference: ["Gemini 參考稿", "分段上傳低解析影片，獨立聽寫＋認講者／專有名詞"],
  correct: ["校正", "用參考稿修正 ASR 文字（不動時間碼）"],
  cues: ["斷句", "對齊回字級時間碼，依語意切成字幕條"],
  translate: ["翻譯", "逐條翻譯"],
  summary: ["摘要", ""], highlight: ["剪輯企劃", ""], render: ["輸出精華影片", ""],
};
const OPT_FIELDS = [
  ["locale", "ASR 語言", "locale"], ["context", "影片脈絡（選填）", "textarea"], ["glossary", "詞彙表（選填）", "textarea"],
  ["ref_model", "參考稿模型（看影片）", "text"], ["text_model", "校正／翻譯模型", "text"],
  ["chunk_minutes", "每段分鐘數", "number"], ["drop_fillers", "移除 um / uh 等語助詞", "bool"], ["semantic_breaks", "語意斷句（Gemini 判斷斷點）", "bool"],
  ["correct_with_audio", "校正時讓 Gemini 聽音訊", "bool"], ["cjk_spacing", "中文與英文／數字之間加半形空格", "bool"], ["energy_snap", "依聲音音量校正字的邊界", "bool"],
  ["backchannels", "附和（mm-hmm、yeah）", "select:omit=沒資訊的省略（一般做法）|keep=全部保留"],
  ["max_chars", "每條最多字元（0 = 自動：英 70／中 22）", "number"], ["max_duration", "每條最長秒數", "number"],
  ["gap_break", "停頓多久強制斷開（秒）", "number"], ["karaoke", "逐字變色（預覽與輸出影片相同）", "bool"], ["translate", "要翻譯", "bool"],
  ["target_lang", "目標語言", "text"], ["sub_order", "雙語字幕順序（預覽與輸出影片相同）", "select:tr-first=譯文在上、原文在下|src-first=原文在上、譯文在下"], ["translate_style", "翻譯風格", "textarea"], ["workers", "並行數", "number"],
];

const S = { defaults: null, locales: [], projects: [], cur: null, cues: [], dirty: false, active: -1, poll: null, stopAt: null };
const video = $("#video");

// ───────── time helpers
const fmt = (t) => {
  t = Math.max(0, t || 0);
  const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m).padStart(2, "0")) + ":" + s.toFixed(3).padStart(6, "0");
};
const parseT = (s) => {
  const p = String(s).trim().split(":").map(Number);
  if (p.some(isNaN)) return null;
  return p.reduce((a, x) => a * 60 + x, 0);
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const normTxt = (s) => String(s || "").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");

// ───────── boot
async function boot() {
  S.defaults = await api("/api/defaults");
  const kb = $("#keyBadge");
  kb.textContent = S.defaults.has_key ? "Gemini 已連線" : "還沒設定 GEMINI_API_KEY";
  kb.className = "badge " + (S.defaults.has_key ? "ok" : "bad");
  if (!S.defaults.has_asr) { kb.textContent = "缺少 bin/apple-asr，請先執行 ./setup.sh"; kb.className = "badge bad"; }
  $(".views").hidden = true;
  S.locales = await api("/api/locales").catch(() => ["en-US", "zh-TW"]);
  await refreshProjects();
  const want = location.hash.slice(1);
  if (want) selectProject(decodeURIComponent(want));
  setInterval(refreshProjects, 4000);
  requestAnimationFrame(tick);
}

async function refreshProjects() {
  S.projects = await api("/api/projects");
  const ul = $("#projectList");
  ul.innerHTML = S.projects.map((p) => {
    const st = p.status?.state || "idle";
    const lab = st === "running" ? `執行中：${STAGE_INFO[p.status.stage]?.[0] || ""} ${Math.round((p.status.progress || 0) * 100)}%`
      : st === "error" ? "上次執行出錯" : (p.done || []).length ? `已完成到「${STAGE_INFO[p.done.at(-1)][0]}」` : "還沒開始處理";
    return `<li data-id="${esc(p.id)}" class="${S.cur?.id === p.id ? "sel" : ""}"><div class="pn" title="${esc(p.name)}">${esc(p.name)}</div>
      <div class="ps">${p.duration ? fmt(p.duration).slice(0, -4) + " · " : ""}${esc(lab)}</div></li>`;
  }).join("") || `<li class="muted small">還沒有專案</li>`;
}
$("#projectList").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) selectProject(li.dataset.id);
});

// ───────── project
async function selectProject(id) {
  if (S.dirty && S.cur && S.cur.id !== id && !confirmDiscard()) return;
  const meta = await api(`/api/projects/${encodeURIComponent(id)}`).catch(() => null);
  if (!meta) return;
  S.cur = meta;
  location.hash = encodeURIComponent(id);
  $("#empty").hidden = true;
  $("#workspace").hidden = false;
  $("#projName").textContent = meta.name;
  $("#hdrProject").textContent = meta.name;
  $(".views").hidden = false;
  $("#projMedia").textContent = meta.media;
  const src = `/api/projects/${encodeURIComponent(id)}/media`;
  exitPreview();
  if (video.dataset.src !== src) { video.src = src; video.dataset.src = src; }
  $("#karaoke").checked = !!({ ...S.defaults.options, ...(meta.options || {}) }).karaoke;
  renderStages();
  renderOpts();
  renderStatus(meta);
  await loadCues();
  loadRef();
  S.plan = null;
  refreshExtras();
  refreshProjects();
  startPolling();
}
function confirmDiscard() { return window.confirm("有未存檔的字幕修改，確定要放棄？"); }

function renderStages() {
  const done = new Set(S.cur.done || []);
  const st = S.cur.status || {};
  $("#stageList").innerHTML = S.defaults.stages.map((s) => {
    const checked = !done.has(s) || s === "cues" ? "checked" : "";
    return `<label class="stage ${done.has(s) ? "done" : ""} ${st.state === "running" && st.stage === s ? "cur" : ""}">
      <input type="checkbox" value="${s}" ${checked}><span class="dot"></span><span><b>${STAGE_INFO[s][0]}</b> <span class="desc">${STAGE_INFO[s][1]}</span></span></label>`;
  }).join("");
}

function renderStatus(m) {
  const st = m.status || {};
  const running = m.running || st.state === "running";
  $("#runBtn").disabled = running;
  $("#trMissingBtn").disabled = running;
  $("#progBar").style.width = running ? `${Math.round((st.progress || 0) * 100)}%` : st.state === "done" ? "100%" : "0";
  let line = running ? `執行中：${STAGE_INFO[st.stage]?.[0] || "…"} ${Math.round((st.progress || 0) * 100)}%`
    : st.state === "error" ? `<span class="err">錯誤：${esc(st.error)}</span>` : st.state === "done" ? "完成" : "";
  $("#statusLine").innerHTML = line;
  const log = $("#log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  log.textContent = (m.log || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
  const u = m.usage;
  $("#costBadge").textContent = u ? `本專案 Gemini 花費約 US$${u.usd.toFixed(2)}` : "";
  $$(".stage").forEach((el) => el.classList.toggle("cur", running && el.querySelector("input").value === st.stage));
}

function startPolling() {
  clearInterval(S.poll);
  let wasRunning = S.cur.running || S.cur.status?.state === "running";
  S.poll = setInterval(async () => {
    if (!S.cur) return;
    const m = await api(`/api/projects/${encodeURIComponent(S.cur.id)}`).catch(() => null);
    if (!m || m.id !== S.cur.id) return;
    const running = m.running || m.status?.state === "running";
    const doneChanged = JSON.stringify(m.done) !== JSON.stringify(S.cur.done);
    S.cur = m;
    renderStatus(m);
    if (doneChanged) renderStages();
    if (wasRunning && !running) refreshExtras();
    if (wasRunning && (!running || doneChanged)) {
      if (!S.dirty) await loadCues();
      loadRef();
      renderStages();
    }
    wasRunning = running;
  }, 1000);
}

$("#runBtn").onclick = async () => {
  const stages = $$("#stageList input:checked").map((i) => i.value);
  if (!stages.length) return;
  if (S.dirty) {
    if (stages.some((s) => s !== "translate") && !window.confirm("重跑會覆蓋你在表格裡的未存檔修改，繼續？")) return;
    if (stages.every((s) => s === "translate")) await saveCues();
  }
  if (!stages.every((s) => s === "translate") && S.cues.length && (await isEdited()) &&
      !window.confirm("你手動編修過的字幕會被重新斷句覆蓋（舊譯文若原文一致會沿用）。繼續？")) return;
  try {
    await api(`/api/projects/${encodeURIComponent(S.cur.id)}/run`, { method: "POST", body: { stages, force: $("#force").checked } });
    S.dirty = false; $("#dirty").hidden = true;
    S.cur.running = true;
    startPolling();
  } catch (e) { alertBox(e.message); }
};
$("#trMissingBtn").onclick = async () => {
  if (S.dirty) await saveCues();
  await api(`/api/projects/${encodeURIComponent(S.cur.id)}/translate-missing`, { method: "POST", body: {} }).catch((e) => alertBox(e.message));
  S.cur.running = true;
  startPolling();
};
async function isEdited() {
  const d = await api(`/api/projects/${encodeURIComponent(S.cur.id)}/cues`);
  return !!d.edited;
}
function alertBox(msg) { $("#statusLine").innerHTML = `<span class="err">${esc(msg)}</span>`; }

// ───────── tabs / options / reference
$$(".panel-tabs button").forEach((b) => b.onclick = () => {
  $$(".panel-tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => t.hidden = t.id !== "tab-" + b.dataset.tab);
});

function renderOpts() {
  const o = { ...S.defaults.options, ...(S.cur.options || {}) };
  $("#optsForm").innerHTML = OPT_FIELDS.map(([k, label, type]) => {
    const v = o[k];
    if (type === "bool") return `<label class="row gap"><input type="checkbox" name="${k}" ${v ? "checked" : ""}> ${label}</label>`;
    if (type === "textarea") return `<label><span>${label}</span><textarea name="${k}" rows="2">${esc(v)}</textarea></label>`;
    if (type.startsWith("select:")) return `<label><span>${label}</span><select name="${k}">${type.slice(7).split("|").map((o) => { const [val, txt] = o.split("="); return `<option value="${val}" ${val === v ? "selected" : ""}>${txt}</option>`; }).join("")}</select></label>`;
    if (type === "locale") return `<label><span>${label}</span><select name="${k}">${S.locales.map((l) => `<option ${l === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>`;
    return `<label><span>${label}</span><input name="${k}" type="${type}" step="any" value="${esc(v)}" class="${type === "text" ? "mono" : ""}"></label>`;
  }).join("");
}
$("#saveOpts").onclick = async () => {
  const out = {};
  for (const [k, , type] of OPT_FIELDS) {
    const el = $(`#optsForm [name="${k}"]`);
    out[k] = type === "bool" ? el.checked : type === "number" ? Number(el.value) : el.value;
  }
  await api(`/api/projects/${encodeURIComponent(S.cur.id)}/options`, { method: "POST", body: out });
  S.cur.options = out;
  $("#saveOpts").textContent = "已儲存 ✓";
  setTimeout(() => $("#saveOpts").textContent = "儲存設定", 1200);
};

async function loadRef() {
  const r = await api(`/api/projects/${encodeURIComponent(S.cur.id)}/reference`).catch(() => ({}));
  if (!r.lines) { $("#refView").innerHTML = `<p class="muted">還沒有參考稿。</p>`; return; }
  const sp = Object.entries(r.speakers || {}).map(([k, v]) => `<b>${esc(k)}</b>：${esc(v)}`).join("<br>");
  $("#refView").innerHTML = `<p class="small">${sp}</p><p class="small muted">專有名詞：${esc((r.terms || []).join("、"))}</p><hr>` +
    r.lines.map((l) => `<p><span class="t" data-t="${l.t}">${fmt(l.t).slice(0, -4)}</span><span class="sp">${esc(l.speaker)}</span>${esc(l.text)}</p>`).join("");
}
$("#refView").addEventListener("click", (e) => { if (e.target.dataset.t) seek(+e.target.dataset.t); });

// ───────── cues table
async function loadCues() {
  const d = await api(`/api/projects/${encodeURIComponent(S.cur.id)}/cues`);
  S.cues = d.cues || [];
  S.dirty = false; $("#dirty").hidden = true;
  renderCues();
}

function rowHTML(c, i) {
  const changed = c.asr && normTxt(c.asr) !== normTxt(c.text);
  return `<tr data-i="${i}">
    <td class="n ${changed ? "chg" : ""}" title="${changed ? "ASR 原文：" + esc(c.asr) : ""}">${i + 1}${c.est ? `<span class="est" title="這句有 ASR 沒聽到、由校正補上的字，時間是估計的，建議對著影片檢查">⏱</span>` : ""}</td>
    <td class="tc"><input value="${fmt(c.start)}" data-f="start"></td>
    <td class="tc"><input value="${fmt(c.end)}" data-f="end"></td>
    <td class="txt"><div contenteditable="plaintext-only" data-f="text">${esc(c.text)}</div>${changed ? `<div class="asr">ASR：${esc(c.asr)}</div>` : ""}</td>
    <td class="tr"><div contenteditable="plaintext-only" data-f="tr">${esc(c.tr || "")}</div></td>
    <td class="act"><button data-a="play" title="播放這條">▶</button><button data-a="split" title="在播放點切開（⌥S）">✂</button><button data-a="merge" title="與下一條合併">⤓</button><button data-a="del" title="刪除">✕</button></td></tr>`;
}
function renderCues() {
  const tb = $("#cues tbody");
  tb.innerHTML = S.cues.map(rowHTML).join("");
  S.active = -1;
  applyFilter();
  markOverlaps();
}
function rerenderRow(i) {
  const tr = $(`#cues tr[data-i="${i}"]`);
  if (tr) tr.outerHTML = rowHTML(S.cues[i], i);
}
function setDirty() { S.dirty = true; $("#dirty").hidden = false; }
function markOverlaps() {
  $$("#cues tbody tr").forEach((tr) => {
    const i = +tr.dataset.i, c = S.cues[i], n = S.cues[i + 1];
    tr.classList.toggle("overlap", c.end <= c.start || (n && c.end > n.start + 0.001));
  });
}

const tbody = $("#cues tbody");
tbody.addEventListener("input", (e) => {
  const f = e.target.dataset.f;
  if (f !== "text" && f !== "tr") return;
  const i = +e.target.closest("tr").dataset.i;
  S.cues[i][f] = e.target.innerText.replace(/\n+$/, "");
  if (f === "text") S.cues[i].words = null; // edited text → karaoke timings no longer match
  setDirty();
});
tbody.addEventListener("change", (e) => {
  const f = e.target.dataset.f;
  if (f !== "start" && f !== "end") return;
  const i = +e.target.closest("tr").dataset.i;
  const v = parseT(e.target.value);
  if (v == null) { e.target.value = fmt(S.cues[i][f]); return; }
  S.cues[i][f] = v;
  e.target.value = fmt(v);
  setDirty(); markOverlaps();
});
tbody.addEventListener("focusin", (e) => {
  const tr = e.target.closest("tr");
  if (tr) S.focusRow = +tr.dataset.i;
});
tbody.addEventListener("click", (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  const i = +tr.dataset.i;
  const a = e.target.dataset.a;
  if (a === "play") { seek(S.cues[i].start); S.stopAt = S.cues[i].end; video.play(); }
  else if (a === "split") splitAt(i, video.currentTime);
  else if (a === "merge") merge(i);
  else if (a === "del") { S.cues.splice(i, 1); setDirty(); renderCues(); }
  else if (e.target.closest("td.n")) seek(S.cues[i].start);
});

function splitAt(i, t) {
  const c = S.cues[i];
  if (!(t > c.start + 0.05 && t < c.end - 0.05)) { alertBox(`播放點 ${fmt(t)} 不在第 ${i + 1} 條範圍內`); return; }
  let a, b;
  if (c.words?.length) {
    const k = c.words.findIndex((w) => (w.s + w.e) / 2 >= t);
    if (k <= 0) { alertBox("播放點太靠邊，切不開"); return; }
    const wa = c.words.slice(0, k), wb = c.words.slice(k);
    a = { ...c, end: Math.min(t, wa.at(-1).e + 0.1), text: joinWords(wa), words: wa, tr: c.tr };
    b = { ...c, start: Math.max(t, wb[0].s), text: joinWords(wb), words: wb, tr: "", asr: "" };
  } else {
    const frac = (t - c.start) / (c.end - c.start);
    let pos = Math.round(c.text.length * frac);
    const sp = c.text.lastIndexOf(" ", pos);
    if (sp > 0) pos = sp;
    a = { ...c, end: t, text: c.text.slice(0, pos).trim() };
    b = { ...c, start: t, text: c.text.slice(pos).trim(), tr: "", asr: "" };
  }
  S.cues.splice(i, 1, a, b);
  setDirty(); renderCues();
}
function joinWords(ws) {
  const cjk = /[㐀-鿿぀-ヿ가-힯]/;
  const nos = "，。、！？；：「」『』（）《》〈〉～·";
  return ws.reduce((s, w) => s && !(nos.includes(s.at(-1)) || nos.includes(w.t[0]) || (cjk.test(s.at(-1)) && cjk.test(w.t[0]))) ? s + " " + w.t : s + w.t, "");
}
function merge(i) {
  const a = S.cues[i], b = S.cues[i + 1];
  if (!b) return;
  const cjk = /[㐀-鿿]/.test(a.text);
  S.cues.splice(i, 2, { ...a, end: b.end, text: a.text + (cjk ? "" : " ") + b.text,
    tr: [a.tr, b.tr].filter(Boolean).join(/[㐀-鿿]/.test(a.tr || "") ? "" : " "),
    words: a.words && b.words ? [...a.words, ...b.words] : null, asr: [a.asr, b.asr].filter(Boolean).join(" ") });
  setDirty(); renderCues();
}

async function saveCues() {
  await api(`/api/projects/${encodeURIComponent(S.cur.id)}/cues`, { method: "POST", body: { cues: S.cues } });
  S.dirty = false; $("#dirty").hidden = true;
  $("#saveBtn").textContent = "已存 ✓";
  setTimeout(() => $("#saveBtn").textContent = "存檔", 1000);
}
$("#saveBtn").onclick = saveCues;

$$("[data-export]").forEach((a) => a.onclick = async (e) => {
  e.preventDefault();
  if (S.dirty) await saveCues();
  location.href = `/api/projects/${encodeURIComponent(S.cur.id)}/export?kind=${a.dataset.export}`;
});

function applyFilter() {
  const q = $("#search").value.trim().toLowerCase();
  const only = $("#onlyChanged").checked;
  $$("#cues tbody tr").forEach((tr) => {
    const c = S.cues[+tr.dataset.i];
    const hit = (!q || (c.text + " " + (c.tr || "")).toLowerCase().includes(q)) &&
      (!only || (c.asr && normTxt(c.asr) !== normTxt(c.text)));
    tr.hidden = !hit;
  });
}
$("#search").addEventListener("input", applyFilter);
$("#karaoke").addEventListener("change", async (e) => {  // shared with the highlight render
  if (!S.cur) return;
  S.cur.options = { ...(S.cur.options || {}), karaoke: e.target.checked };
  await api(pidURL() + "/options", { method: "POST", body: { karaoke: e.target.checked } }).catch(() => {});
  renderOpts();
});
$("#onlyChanged").addEventListener("change", applyFilter);

// ───────── playback sync
function seek(t) { exitPreview(); video.currentTime = Math.max(0, t); }  // back to the source timeline
function findCue(t) {
  let lo = 0, hi = S.cues.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (S.cues[mid].start <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans >= 0 && t < S.cues[ans].end ? ans : -1;
}
function tick() {
  const t = video.currentTime;
  seqTick(t);
  $("#timecode").textContent = fmt(t);
  if (S.stopAt != null && t >= S.stopAt) { video.pause(); S.stopAt = null; }
  const i = findCue(t);
  if (i !== S.active) {
    $(`#cues tr[data-i="${S.active}"]`)?.classList.remove("active");
    S.active = i;
    const tr = $(`#cues tr[data-i="${i}"]`);
    if (tr) {
      tr.classList.add("active");
      const editing = document.activeElement && tr.parentNode.contains(document.activeElement);
      if ($("#follow").checked && !video.paused && !editing) tr.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
  composite(t, i);
  requestAnimationFrame(tick);
}
// ───────── live composite (non-destructive preview)
// Every layer is a PNG from /overlay, drawn by bin/text-render exactly as the export draws it.
const CARD = 1.7, ZOOM = 1.12;
const ly = { sub: $("#lySub"), card: $("#lyCard"), tag: $("#lyTag") };
function layoutOverlay() {  // cover exactly the visible picture (letterboxing aside)
  const vw = video.videoWidth, vh = video.videoHeight, bw = video.clientWidth, bh = video.clientHeight;
  if (!vw || !vh || !bw) return;
  const k = Math.min(bw / vw, bh / vh), dw = vw * k, dh = vh * k;
  S.u = dh / 1080;
  Object.assign($("#layers").style, { left: (bw - dw) / 2 + "px", top: (bh - dh) / 2 + "px", width: dw + "px", height: dh + "px" });
}
video.addEventListener("loadedmetadata", layoutOverlay);
new ResizeObserver(layoutOverlay).observe(video);

const overlayURL = (kind, lines) => `${pidURL()}/overlay?kind=${kind}` + lines.map((l) => "&l=" + encodeURIComponent(l)).join("");
const preloaded = new Set();
function preload(url) { if (!preloaded.has(url)) { preloaded.add(url); new Image().src = url; } }
function showLayer(el, url) {  // swap only once the new image is decoded → no flicker
  if (!url) { if (el.dataset.want) { el.dataset.want = ""; el.removeAttribute("src"); } return; }
  if (el.dataset.want === url) return;
  el.dataset.want = url;
  const im = new Image();
  im.onload = () => { if (el.dataset.want === url) el.src = url; };
  im.src = url;
  preloaded.add(url);
}
const NOSPACE = "，。、！？；：「」『』（）《》〈〉～·", CJKRE = /[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]/;
const needsSpace = (prev, tok) => !(NOSPACE.includes(prev.at(-1)) || NOSPACE.includes(tok[0]) || (CJKRE.test(prev.at(-1)) && CJKRE.test(tok[0])));
function dialogSplit(c) {  // same as pipeline.dialog_split
  if (c.split) return c.split;
  const first = c.text.split("\n")[0].replace(/^- /, ""), toks = c.words.map((w) => w.t);
  for (let k = 1; k < toks.length; k++) if (joinWords(c.words.slice(0, k)) === first) return k;
  return toks.length;
}
function markedSource(c, t) {  // same as pipeline.marked_source: per-word highlight, \x01 … \x02
  const ws = c.words;
  const lines = c.dialog ? [ws.slice(0, dialogSplit(c)), ws.slice(dialogSplit(c))] : [ws];
  return lines.map((line) => {
    let plain = "", marked = "";
    for (const w of line) {
      const sep = plain && needsSpace(plain, w.t) ? " " : "";
      plain += sep + w.t;
      marked += sep + (w.s <= t + 1e-6 ? "\x01" + w.t + "\x02" : w.t);
    }
    return (c.dialog ? "- " : "") + marked.replaceAll("\x02 \x01", " ").replaceAll("\x02\x01", "");
  }).join("\n");
}
function cueLines(c, t) {  // same rules as highlights.reel_subs
  const opts = { ...S.defaults.options, ...(S.cur?.options || {}) };
  const src = opts.karaoke && c.words?.length ? markedSource(c, t) : c.text;
  const tr = (c.tr || "").replace(/⏎/g, " ").trim();
  return !tr ? [src] : opts.sub_order === "src-first" ? [src, tr] : [tr, src];
}
function prefetchCue(c) {
  if (!c || !S.cur) return;
  const opts = { ...S.defaults.options, ...(S.cur.options || {}) };
  const times = opts.karaoke && c.words?.length ? [c.start - 1, ...c.words.map((w) => w.s)] : [c.start];
  times.forEach((t) => preload(overlayURL("sub", cueLines(c, t))));
}
let lastCue = -2;
function composite(t, i) {
  const on = $("#showOverlay").checked && S.cur;
  const q0 = S.seq, pc0 = q0 && q0.i >= 0 ? q0.pieces[q0.i] : null;
  let c = S.cues[i];
  if (c && pc0 && !spokenIn(c, pc0.start, pc0.end)) c = null;  // reel: no lines from outside the clip
  showLayer(ly.sub, on && c ? overlayURL("sub", cueLines(c, t)) : null);
  if (i !== lastCue) { lastCue = i; prefetchCue(S.cues[i]); prefetchCue(S.cues[i + 1]); }
  // reel preview layers: chapter card (+ blurred picture), tag, punch-in zoom
  const q = S.seq, pc = q && q.i >= 0 ? q.pieces[q.i] : null;
  let card = null, tag = null, zoom = 1, blur = false;
  if (pc && on) {
    zoom = pc.zoom || 1;
    if (pc.title && pc.cards) {
      if (pc.first && t - pc.start < Math.min(CARD, pc.chapterDur)) { card = overlayURL("card", [pc.title, pc.title_en || ""]); blur = true; }
      else tag = overlayURL("tag", [pc.title]);
    }
  }
  showLayer(ly.card, card);
  showLayer(ly.tag, tag);
  const tf = zoom !== 1 ? `scale(${zoom})` : "";
  if (video.style.transform !== tf) { video.style.transform = tf; video.style.transformOrigin = "45% 35%"; }
  const fl = blur ? `blur(${18 * (S.u || 0.5)}px) brightness(0.88) saturate(0.75)` : "";
  if (video.style.filter !== fl) video.style.filter = fl;
}

function joinHTML(spans, words) {
  const cjk = /[㐀-鿿぀-ヿ가-힯]/;
  return spans.reduce((s, h, k) => {
    if (!k) return h;
    const a = words[k - 1].t.at(-1), b = words[k].t[0];
    return s + (cjk.test(a) && cjk.test(b) || "，。、！？；：「」『』（）".includes(a) || "，。、！？；：「」『』（）".includes(b) ? "" : " ") + h;
  }, "");
}

// ───────── keyboard
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); if (S.cur) saveCues(); return; }
  if (!e.altKey || !S.cur) return;
  const row = S.active >= 0 ? S.active : S.focusRow;
  if (e.code === "Space") { e.preventDefault(); S.seq ? $("#rbPlay").click() : video.paused ? video.play() : video.pause(); }
  else if (e.code === "BracketLeft" && row != null && S.cues[row]) { e.preventDefault(); S.cues[row].start = video.currentTime; rerenderRow(row); setDirty(); markOverlaps(); }
  else if (e.code === "BracketRight" && row != null && S.cues[row]) { e.preventDefault(); S.cues[row].end = video.currentTime; rerenderRow(row); setDirty(); markOverlaps(); }
  else if (e.code === "KeyS" && row != null && S.cues[row]) { e.preventDefault(); splitAt(row, video.currentTime); }
  else if (e.code === "ArrowLeft") { e.preventDefault(); S.seq ? reelSeek(Math.max(0, reelTime() - 2)) : seek(video.currentTime - 2); }
  else if (e.code === "ArrowRight") { e.preventDefault(); S.seq ? reelSeek(reelTime() + 2) : seek(video.currentTime + 2); }
});
window.addEventListener("beforeunload", (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });

// ───────── new project dialog
const dlg = $("#newDlg");
$("#newBtn").onclick = () => {
  const o = S.defaults.options;
  $("#newLocale").innerHTML = S.locales.map((l) => `<option ${l === o.locale ? "selected" : ""}>${l}</option>`).join("");
  $("#newRefModel").value = o.ref_model;
  $("#newErr").textContent = "";
  $("#browser").hidden = true;
  dlg.showModal();
};
$("#browseBtn").onclick = () => browse($("#pathInput").value ? $("#pathInput").value.replace(/\/[^/]*$/, "") : "");
async function browse(dir) {
  const r = await api("/api/browse?dir=" + encodeURIComponent(dir || "~")).catch((e) => ({ error: e.message }));
  const b = $("#browser");
  b.hidden = false;
  if (r.error) { b.innerHTML = `<div class="err">${esc(r.error)}</div>`; return; }
  b.innerHTML = `<div class="cur">${esc(r.dir)}</div><div data-dir="${esc(r.parent)}">⬆︎ ..</div>` +
    r.items.map((it) => it.dir ? `<div data-dir="${esc(it.path)}">📁 ${esc(it.name)}</div>`
      : `<div data-file="${esc(it.path)}">🎬 ${esc(it.name)} <span class="muted small">${(it.size / 1e6).toFixed(0)} MB</span></div>`).join("");
}
$("#browser").addEventListener("click", (e) => {
  const d = e.target.closest("[data-dir],[data-file]");
  if (!d) return;
  if (d.dataset.dir) browse(d.dataset.dir);
  else { $("#pathInput").value = d.dataset.file; $("#browser").hidden = true; }
});
const drop = $("#drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault(); drop.classList.remove("over");
  const f = e.dataTransfer.files[0];
  if (!f) return;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.setRequestHeader("X-Filename", encodeURIComponent(f.name));
  xhr.upload.onprogress = (ev) => drop.textContent = `上傳中 ${Math.round(ev.loaded / ev.total * 100)}%…`;
  xhr.onload = () => {
    const j = JSON.parse(xhr.responseText);
    $("#pathInput").value = j.path;
    drop.textContent = `已上傳：${f.name}`;
  };
  xhr.onerror = () => drop.textContent = "上傳失敗";
  xhr.send(f);
});
$("#createBtn").onclick = async (e) => {
  e.preventDefault();
  try {
    const p = await api("/api/projects", { method: "POST", body: { path: $("#pathInput").value.trim(), options: {
      locale: $("#newLocale").value, ref_model: $("#newRefModel").value.trim() || S.defaults.options.ref_model,
      context: $("#newContext").value, glossary: $("#newGlossary").value } } });
    dlg.close();
    await api(`/api/projects/${encodeURIComponent(p.id)}/run`, { method: "POST", body: { stages: S.defaults.stages } }).catch(() => {});
    await refreshProjects();
    selectProject(p.id);
  } catch (err) { $("#newErr").textContent = err.message; }
};

// ───────── views
$$(".views button").forEach((b) => b.onclick = () => {
  $$(".views button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".view").forEach((v) => v.hidden = v.id !== "view-" + b.dataset.view);
});
const pidURL = () => `/api/projects/${encodeURIComponent(S.cur.id)}`;
async function refreshExtras() {
  if (!S.cur) return;
  loadSummary();
  await loadPlans();
  if (S.openNewestPlan && S.plans?.length) {
    S.openNewestPlan = false;
    await openPlan(S.plans[0].id);
    enterPreview(0, false);  // the edit is cued up in the top player, paused at 00:00
    return;
  }
  if (S.plan && !S.planDirty) openPlan(S.plan.id);
}
function needIdle() {
  if (S.cur.running || S.cur.status?.state === "running") { alertBox("目前有工作在執行，等它跑完再試"); return false; }
  return true;
}
async function startTask(path, body) {
  S.openNewestPlan = path === "/highlights";
  try {
    await api(pidURL() + path, { method: "POST", body });
    S.cur.running = true;
    startPolling();
  } catch (e) { alertBox(e.message); }
}

// ───────── summary
async function loadSummary() {
  const s = await api(pidURL() + "/summary").catch(() => ({}));
  S.summary = s.title ? s : null;
  const box = $("#sumBody");
  if (!S.summary) { box.innerHTML = `<p class="muted">還沒有摘要。按「產生摘要」，Gemini 會讀整份字幕寫標題、摘要、章節、重點、金句與社群貼文。</p>`; return; }
  const t = (x) => `<span class="t" data-t="${x}">${fmt(x).slice(0, -4)}</span>`;
  box.innerHTML = `<h2>${esc(s.title)}</h2><p class="logline">${esc(s.logline)}</p>
    <h3>摘要</h3><p>${esc(s.summary)}</p>
    <h3>章節</h3>${s.chapters.map((c) => `<div class="ch">${t(c.t)}<div><b>${esc(c.title)}</b><br><span class="muted small">${esc(c.summary)}</span></div></div>`).join("")}
    <h3>重點</h3><ul>${s.takeaways.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    <h3>金句</h3>${s.quotes.map((q) => `<div class="q">${t(q.t)} “${esc(q.text)}”<div class="tr">${esc(q.tr)}</div></div>`).join("")}
    <h3>社群貼文</h3><pre>${esc(s.social_post)}</pre>`;
}
$("#sumBody").addEventListener("click", (e) => { if (e.target.dataset.t) { seek(+e.target.dataset.t); video.play(); } });
$("#sumBtn").onclick = () => { if (needIdle()) startTask("/summary", { note: $("#sumNote").value }); };
$("#sumMd").onclick = (e) => { e.preventDefault(); if (S.summary) location.href = pidURL() + "/summary?format=md"; };
$("#sumChapters").onclick = async () => {
  if (!S.summary) return;
  await navigator.clipboard.writeText(S.summary.chapters.map((c) => `${fmt(c.t).slice(0, -4)} ${c.title}`).join("\n"));
  $("#sumChapters").textContent = "已複製 ✓"; setTimeout(() => $("#sumChapters").textContent = "複製 YouTube 章節", 1200);
};

// ───────── highlights
async function loadPlans() {
  S.plans = await api(pidURL() + "/highlights").catch(() => []);
  $("#hlList").innerHTML = S.plans.map((p) => `<div data-id="${esc(p.id)}" class="${S.plan?.id === p.id ? "sel" : ""}">
      <b>${esc(p.title || p.id)}</b><div class="meta">${Math.round(p.duration || 0)} 秒${p.rendered ? " · 已輸出" : ""} · ${esc((p.request?.focus || "AI 自選").slice(0, 30))}</div></div>`).join("")
    || `<p class="muted small">還沒有企劃</p>`;
}
$("#hlList").addEventListener("click", (e) => { const d = e.target.closest("[data-id]"); if (d) openPlan(d.dataset.id); });
$("#hlPlanBtn").onclick = () => {
  if (!needIdle()) return;
  startTask("/highlights", { focus: $("#hlFocus").value, seconds: +$("#hlSecs").value || 90,
    chapters: $("#hlChapters").checked, hook: $("#hlHook").checked, zoom: $("#hlZoom").checked,
    review: $("#hlReview").checked });
};
async function openPlan(id) {
  S.plan = await api(pidURL() + "/highlights/" + encodeURIComponent(id)).catch(() => null);
  S.planDirty = false;
  $$("#hlList [data-id]").forEach((d) => d.classList.toggle("sel", d.dataset.id === id));
  renderPlan();
}
const byId = (id) => S.cues.find((c) => c.id === id);
function clipText(c) {
  const cs = S.cues.filter((x) => x.end > c.start + 0.05 && x.start < c.end - 0.05);
  return { src: cs.map((x) => x.text).join(" "), tr: cs.map((x) => x.tr || "").join(" ") };
}
function planDur(pl) {
  const d = (cs) => cs.reduce((a, c) => a + c.end - c.start, 0);
  return d(pl.hook || []) + pl.chapters.reduce((a, ch) => a + d(ch.clips), 0);
}
// same rule as highlights.spoken_in: a line belongs to a clip only if some of its words are spoken inside it
function spokenIn(x, a, b) {
  if (!x.words?.length) return x.end > a + 0.3 && x.start < b - 0.3;
  const inside = x.words.filter((w) => (w.s + w.e) / 2 >= a && (w.s + w.e) / 2 <= b).length;
  return inside * 2 >= x.words.length;  // at least half of the line (same as highlights.spoken_in)
}
function clipCues(c) { return S.cues.filter((x) => x.end > c.start && x.start < c.end && spokenIn(x, c.start, c.end)); }
function tightEdges(c) {  // a cut with no pause on that side would slice into continuous speech
  const r = clipLineRange(c);
  if (!r) return [];
  const out = [];
  if (r[0] > 0 && c.start - wE(S.cues[r[0] - 1]) < 0.03) out.push("開頭");
  if (r[1] + 1 < S.cues.length && wS(S.cues[r[1] + 1]) - c.end < 0.03) out.push("結尾");
  return out;
}
function clipRow(c, ci, k) {
  const tight = tightEdges(c);
  return `<div class="clip" data-ci="${ci}" data-k="${k}">
    <button data-a="play" title="預覽這段">▶</button>
    <input value="${fmt(c.start)}" data-f="start"><input value="${fmt(c.end)}" data-f="end">
    <div class="txt">${clipCues(c).map((x) => `<div class="cl" data-id="${x.id}">
        <button class="drop" data-a="dropLine" title="從這段拿掉這一句（中間的句子會變成跳剪）">✕</button>
        <div contenteditable="plaintext-only" data-cf="text">${esc(x.text)}</div>
        <div contenteditable="plaintext-only" data-cf="tr" class="tr">${esc(x.tr || "")}</div></div>`).join("")}
      ${c.why ? `<div class="why">${esc(c.why)}</div>` : ""}
      ${tight.length ? `<div class="tight" title="講者在這裡沒有停頓，剪在這裡會切到連續的語音；可用＋／－前一句、後一句調整">⚠ ${tight.join("、")}接點沒有停頓</div>` : ""}</div>
    <div class="clip-acts">
      <button data-a="extPrev" title="往前多加一句">＋前一句</button><button data-a="cutFirst" title="去掉開頭那一句">－前一句</button>
      <button data-a="extNext" title="往後多加一句">＋後一句</button><button data-a="cutLast" title="去掉結尾那一句">－後一句</button>
      <button data-a="up" title="上移">↑</button><button data-a="down" title="下移">↓</button><button data-a="del" title="刪除這段">✕</button></div></div>`;
}
function renderPlan() {
  const pl = S.plan, box = $("#hlEdit");
  if (!pl) { box.innerHTML = `<p class="muted">選一個企劃，或在左邊產生新的。</p>`; return; }
  const r = pl.rendered, subs = pl.options?.subs || "burn";
  box.innerHTML = `<div class="hl-bar"><input class="title" id="plTitle" value="${esc(pl.title || "")}">
      <span id="plDur" class="muted">${planDur(pl).toFixed(1)} 秒</span>
      <button id="plPreview" class="primary">▶ 預覽</button><button id="plSave">存檔</button></div>
    <div class="muted small">要求：${esc(pl.request?.focus || "AI 自選最精彩段落")}｜目標 ${pl.request?.seconds || "?"} 秒。
      預覽在上方播放器即時合成（字幕、字卡、標籤、放大都沒有燒進去），下面的字幕可以直接改，改完預覽立刻反映。</div>
    ${(pl.hook || []).length ? `<div class="chap"><b>開頭 hook</b>${pl.hook.map((c, k) => clipRow(c, -1, k)).join("")}</div>` : ""}
    ${pl.chapters.map((ch, ci) => `<div class="chap"><div class="chap-head">
        <input data-ci="${ci}" data-f="title" value="${esc(ch.title)}" placeholder="章節標題">
        <input data-ci="${ci}" data-f="title_en" value="${esc(ch.title_en || "")}" placeholder="English title">
        <button data-ci="${ci}" data-a="addHere" title="先在字幕分頁或播放器找到想要的那一句，再按這裡加進這一章">＋ 在播放點加一段</button>
        <button data-ci="${ci}" data-a="delch" title="刪除整章">✕ 章</button></div>
        ${ch.clips.map((c, k) => clipRow(c, ci, k)).join("")}</div>`).join("")}
    <div class="hl-out"><div class="row gap">
        <label>字幕：<select id="plSubs">
          <option value="burn" ${subs === "burn" ? "selected" : ""}>燒進畫面（含逐字變色）</option>
          <option value="soft" ${subs === "soft" ? "selected" : ""}>內嵌字幕軌（播放器可開關、可再編輯）</option>
          <option value="none" ${subs === "none" ? "selected" : ""}>不加字幕（另附 SRT）</option></select></label>
        <button id="plRender" class="primary">確認，輸出影片</button></div>
      ${r ? `<p class="small">上次輸出：${r.duration} 秒（${{ burn: "燒進畫面", soft: "內嵌字幕軌", none: "無字幕" }[r.subs || "burn"]}）
        <a class="btn" href="${pidURL()}/highlights/${pl.id}/video?ts=${r.at}" target="_blank">開啟</a>
        <a class="btn" href="${pidURL()}/highlights/${pl.id}/download">下載 MP4</a>
        <a class="btn" href="${pidURL()}/highlights/${pl.id}/srt">下載 SRT</a></p>` : ""}</div>`;
}
// cut points between lines — the quietest moment of each pause (server: highlights.cut_points)
const wS = (c) => c.words?.length ? c.words[0].s : c.start;
const wE = (c) => c.words?.length ? c.words.at(-1).e : c.end;
function cutBefore(i) {  // quiet cut point computed by the server (highlights.cut_points)
  const c = S.cues[i];
  return c.cin ?? +(wS(c) - 0.05).toFixed(3);
}
function cutAfter(i) {
  const c = S.cues[i];
  return c.cout ?? +(wE(c) + 0.1).toFixed(3);
}
function clipLineRange(c) {  // indices of the first / last line spoken inside a clip
  const idx = S.cues.map((x, i) => [x, i]).filter(([x]) => x.end > c.start && x.start < c.end && spokenIn(x, c.start, c.end)).map(([, i]) => i);
  return idx.length ? [idx[0], idx.at(-1)] : null;
}
function lineAt(t) {  // the line at (or right after) a source time
  const i = findCue(t);
  if (i >= 0) return i;
  const k = S.cues.findIndex((x) => x.start >= t);
  return k >= 0 ? k : S.cues.length - 1;
}
const clipsOf = (ci) => ci < 0 ? S.plan.hook : S.plan.chapters[ci].clips;
function planChanged() {
  S.planDirty = true;
  refreshPreview();
  $("#plDur").textContent = planDur(S.plan).toFixed(1) + " 秒（未存檔）";
}
$("#hlEdit").addEventListener("input", (e) => {  // subtitle edits inside the plan → the project's cues
  const cf = e.target.dataset.cf;
  if (!cf) return;
  const c = S.cues.find((x) => x.id === +e.target.closest(".cl").dataset.id);
  if (!c) return;
  c[cf] = e.target.innerText.replace(/\n+$/, "");
  if (cf === "text") c.words = null;  // edited wording no longer matches the word clock (no karaoke)
  setDirty(); planChanged();
});
$("#hlEdit").addEventListener("change", (e) => {
  if (e.target.id === "plSubs") { S.plan.options = { ...(S.plan.options || {}), subs: e.target.value }; planChanged(); return; }
  const f = e.target.dataset.f;
  if (e.target.id === "plTitle") { S.plan.title = e.target.value; planChanged(); return; }
  const row = e.target.closest(".clip");
  if (row && (f === "start" || f === "end")) {
    const c = clipsOf(+row.dataset.ci)[+row.dataset.k], v = parseT(e.target.value);
    if (v == null) { e.target.value = fmt(c[f]); return; }
    c[f] = v; e.target.value = fmt(v); planChanged(); return;
  }
  if (f === "title" || f === "title_en") { S.plan.chapters[+e.target.dataset.ci][f] = e.target.value; planChanged(); }
});
$("#hlEdit").addEventListener("click", async (e) => {
  const a = e.target.dataset.a, id = e.target.id;
  if (id === "plPreview") return enterPreview(0, true);
  if (id === "plSave") return savePlan();
  if (id === "plRender") {
    if (!needIdle()) return;
    if (S.planDirty) await savePlan();
    return startTask(`/highlights/${S.plan.id}/render`, {});
  }
  if (a === "delch") { S.plan.chapters.splice(+e.target.dataset.ci, 1); planChanged(); return renderPlan(); }
  if (a === "addHere") {
    const ci = +e.target.dataset.ci, t = video.currentTime, i = lineAt(t);
    if (i < 0) return;
    const c = { start: cutBefore(i), end: cutAfter(i), from: S.cues[i].id, to: S.cues[i].id, why: "手動加入" };
    const list = S.plan.chapters[ci].clips;
    const at = list.findIndex((x) => x.start > c.start);  // keep the chapter in source order
    list.splice(at < 0 ? list.length : at, 0, c);
    planChanged(); return renderPlan();
  }
  const row = e.target.closest(".clip");
  if (!row || !a) return;
  const ci = +row.dataset.ci, k = +row.dataset.k, list = clipsOf(ci);
  if (a === "play") {  // move the reel playhead to this clip
    const idx = seqPieces().findIndex((p) => p.ci === ci && p.k === k);
    return enterPreview(Math.max(0, idx), true);
  }
  if (a === "dropLine") {  // remove one line: trim at the edges, split into a jump cut in the middle
    const c = list[k], r = clipLineRange(c);
    const j = S.cues.findIndex((x) => x.id === +e.target.closest(".cl").dataset.id);
    if (!r || j < 0) return;
    const [f, l] = r;
    if (f === l) list.splice(k, 1);
    else if (j === f) c.start = cutBefore(f + 1);
    else if (j === l) c.end = cutAfter(l - 1);
    else list.splice(k, 1, { ...c, end: cutAfter(j - 1) }, { ...c, start: cutBefore(j + 1), why: "" });
    if (ci >= 0 && !list.length) S.plan.chapters.splice(ci, 1);
    planChanged(); return renderPlan();
  }
  if (["extPrev", "cutFirst", "extNext", "cutLast"].includes(a)) {
    const c = list[k], r = clipLineRange(c);
    if (!r) return;
    const [f, l] = r;
    if (a === "extPrev" && f > 0) c.start = cutBefore(f - 1);
    if (a === "cutFirst" && l > f) c.start = cutBefore(f + 1);
    if (a === "extNext" && l + 1 < S.cues.length) c.end = cutAfter(l + 1);
    if (a === "cutLast" && l > f) c.end = cutAfter(l - 1);
    planChanged(); return renderPlan();
  }
  if (a === "del") list.splice(k, 1);
  if (a === "up" && k > 0) [list[k - 1], list[k]] = [list[k], list[k - 1]];
  if (a === "down" && k < list.length - 1) [list[k + 1], list[k]] = [list[k], list[k + 1]];
  if (ci >= 0 && !list.length) S.plan.chapters.splice(ci, 1);
  planChanged(); renderPlan(); $("#plDur").textContent = planDur(S.plan).toFixed(1) + " 秒（未存檔）";
});
async function savePlan() {
  if (S.dirty) { await saveCues(); renderCues(); }
  S.plan = await api(`${pidURL()}/highlights/${S.plan.id}`, { method: "POST", body: S.plan });
  S.planDirty = false;
  renderPlan(); loadPlans();
}
function seqPieces() {  // same timeline as highlights.timeline()
  const pl = S.plan, o = pl.options || {}, pieces = [];
  (pl.hook || []).forEach((c, k) => pieces.push({ ...c, ci: -1, k }));
  pl.chapters.forEach((ch, ci) => {
    const chapterDur = ch.clips.reduce((a, c) => a + c.end - c.start, 0);
    ch.clips.forEach((c, k) => pieces.push({ ...c, ci, k, first: k === 0, chapterDur, title: ch.title, title_en: ch.title_en,
      cards: o.cards !== false, zoom: o.zoom !== false && k % 2 ? ZOOM : 1 }));
  });
  let off = 0;
  pieces.forEach((p) => { p.off = off; off += p.end - p.start; });
  return pieces;
}

// ───────── reel preview mode: the player shows the highlight's own timeline
// reel time rt ↔ (piece i, source time pieces[i].start + rt - pieces[i].off)
function reelTotal(q) { const l = q.pieces.at(-1); return l ? l.off + l.end - l.start : 0; }
function reelTime() {
  const q = S.seq, pc = q?.pieces[q.i];
  return pc ? pc.off + Math.min(Math.max(video.currentTime - pc.start, 0), pc.end - pc.start) : 0;
}
function enterPreview(startPiece = 0, play = true) {
  if (!S.plan) return;
  const pieces = seqPieces();
  if (!pieces.length) return;
  S.seq = { pieces, i: -1 };
  video.controls = false;
  $("#reelBar").hidden = false;
  const total = reelTotal(S.seq);
  $("#rbRange").max = total;
  $("#rbMarks").innerHTML = pieces.filter((p) => p.first || (p.ci < 0 && p.k === 0)).map((p) =>
    `<span style="left:${(p.off / total) * 100}%" data-t="${esc(p.ci < 0 ? "hook" : p.title || "")}"></span>`).join("");
  seqGo(Math.min(startPiece, pieces.length - 1), 0);
  play ? video.play().catch(() => {}) : video.pause();
}
function exitPreview() {
  if (!S.seq) return;
  S.seq = null;
  video.pause();
  video.controls = true;
  $("#reelBar").hidden = true;
  $$("#hlEdit .clip.playing").forEach((x) => x.classList.remove("playing"));
  video.style.transform = video.style.filter = "";
}
function seqGo(i, within) {
  const q = S.seq, pc = q.pieces[i];
  q.i = i;
  q.ended = false;
  if (!video.muted) {  // silence the jump; unmute once the new position is ready
    video.muted = true;
    video.addEventListener("seeked", () => { video.muted = false; }, { once: true });
  }
  video.currentTime = pc.start + within;
  $("#rbLabel").textContent = `${i + 1}/${q.pieces.length}｜${pc.ci < 0 ? "開頭 hook" : pc.title || ""}`;
  $$("#hlEdit .clip.playing").forEach((x) => x.classList.remove("playing"));
  $(`#hlEdit .clip[data-ci="${pc.ci}"][data-k="${pc.k}"]`)?.classList.add("playing");
}
function seqNext() {
  const q = S.seq;
  if (q.i + 1 < q.pieces.length) return seqGo(q.i + 1, 0);
  video.pause();  // end of the reel: stop here, like an editor's program monitor
  q.ended = true;
}
function seqTick(t) {
  const q = S.seq;
  if (!q || q.i < 0) return;
  const total = reelTotal(q), rt = q.ended ? total : reelTime();
  if (!S.scrubbing) $("#rbRange").value = rt;
  $("#rbTime").textContent = `${fmt(rt).slice(0, -2)} / ${fmt(total).slice(0, -2)}`;
  $("#rbPlay").textContent = video.paused ? "▶" : "❚❚";
  if (!video.paused && !video.seeking && t >= q.pieces[q.i].end - 0.06) seqNext();
}
function reelSeek(rt) {
  const q = S.seq;
  let i = q.pieces.findIndex((p) => rt < p.off + p.end - p.start);
  if (i < 0) i = q.pieces.length - 1;
  seqGo(i, Math.max(0, rt - q.pieces[i].off));
}
$("#rbRange").addEventListener("input", (e) => { S.scrubbing = true; reelSeek(+e.target.value); });
$("#rbRange").addEventListener("change", () => { S.scrubbing = false; });
$("#rbPlay").onclick = () => {
  if (!S.seq) return;
  if (S.seq.ended) { seqGo(0, 0); return video.play(); }
  video.paused ? video.play() : video.pause();
};
$("#rbExit").onclick = exitPreview;
function refreshPreview() {  // plan edited while previewing → rebuild the timeline, keep the playhead
  if (!S.seq || !S.plan) return;
  const rt = reelTime(), wasPlaying = !video.paused;
  enterPreview(0, false);
  reelSeek(Math.min(rt, reelTotal(S.seq)));
  if (wasPlaying) video.play().catch(() => {});
}

window.addEventListener("hashchange", () => {  // links like #<project id> open that project
  const id = decodeURIComponent(location.hash.slice(1));
  if (id && id !== S.cur?.id) selectProject(id);
});

boot();
