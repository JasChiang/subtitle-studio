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
  cues: ["斷句", "對齊回字級時間碼，切成字幕條"],
  translate: ["翻譯", "逐條翻譯"],
};
const OPT_FIELDS = [
  ["locale", "ASR 語言", "locale"], ["context", "影片脈絡", "textarea"], ["glossary", "詞彙表", "textarea"],
  ["ref_model", "參考稿模型（看影片）", "text"], ["text_model", "校正／翻譯模型", "text"],
  ["chunk_minutes", "每段分鐘數", "number"], ["drop_fillers", "移除 um / uh 等語助詞", "bool"],
  ["max_chars", "每條最多字元（0 = 自動：英 70／中 22）", "number"], ["max_duration", "每條最長秒數", "number"],
  ["gap_break", "停頓多久強制斷開（秒）", "number"], ["translate", "要翻譯", "bool"],
  ["target_lang", "目標語言", "text"], ["translate_style", "翻譯風格", "textarea"], ["workers", "並行數", "number"],
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
  kb.textContent = S.defaults.has_key ? "Gemini key ✓" : "缺 GEMINI_API_KEY";
  kb.className = "badge " + (S.defaults.has_key ? "ok" : "bad");
  if (!S.defaults.has_asr) { kb.textContent += " · 缺 bin/apple-asr（跑 ./setup.sh）"; kb.className = "badge bad"; }
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
      : st === "error" ? "⚠ 錯誤" : (p.done || []).map((d) => STAGE_INFO[d][0]).join(" · ") || "尚未執行";
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
  $("#projMedia").textContent = meta.media;
  const src = `/api/projects/${encodeURIComponent(id)}/media`;
  if (video.dataset.src !== src) { video.src = src; video.dataset.src = src; }
  renderStages();
  renderOpts();
  renderStatus(meta);
  await loadCues();
  loadRef();
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
  $("#costBadge").textContent = u ? `Gemini 花費約 $${u.usd.toFixed(3)}` : "";
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
    <td class="n ${changed ? "chg" : ""}" title="${changed ? "ASR 原文：" + esc(c.asr) : ""}">${i + 1}</td>
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
$("#onlyChanged").addEventListener("change", applyFilter);

// ───────── playback sync
function seek(t) { video.currentTime = Math.max(0, t); }
function findCue(t) {
  let lo = 0, hi = S.cues.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (S.cues[mid].start <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans >= 0 && t < S.cues[ans].end ? ans : -1;
}
let lastOv = "";
function tick() {
  const t = video.currentTime;
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
  const showOv = $("#showOverlay").checked;
  $("#overlay").hidden = !showOv;
  if (showOv) {
    const c = S.cues[i];
    let src = "", tr = "";
    if (c) {
      tr = esc(c.tr || "").replace(/⏎/g, "<br>");
      src = $("#karaoke").checked && c.words?.length
        ? joinHTML(c.words.map((w) => `<span class="w${t >= w.s ? " on" : ""}">${esc(w.t)}</span>`), c.words)
        : esc(c.text);
    }
    const key = src + "|" + tr;
    if (key !== lastOv) { $("#ovSrc").innerHTML = src; $("#ovTr").innerHTML = tr; lastOv = key; }
  }
  requestAnimationFrame(tick);
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
  if (e.code === "Space") { e.preventDefault(); video.paused ? video.play() : video.pause(); }
  else if (e.code === "BracketLeft" && row != null && S.cues[row]) { e.preventDefault(); S.cues[row].start = video.currentTime; rerenderRow(row); setDirty(); markOverlaps(); }
  else if (e.code === "BracketRight" && row != null && S.cues[row]) { e.preventDefault(); S.cues[row].end = video.currentTime; rerenderRow(row); setDirty(); markOverlaps(); }
  else if (e.code === "KeyS" && row != null && S.cues[row]) { e.preventDefault(); splitAt(row, video.currentTime); }
  else if (e.code === "ArrowLeft") { e.preventDefault(); seek(video.currentTime - 2); }
  else if (e.code === "ArrowRight") { e.preventDefault(); seek(video.currentTime + 2); }
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

boot();
