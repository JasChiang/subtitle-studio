"""Subtitle pipeline: Apple ASR (word clock) → Gemini video reference → correction → alignment → cues → translation.

Every stage caches its output in the project dir, so any stage can be re-run on its own.
The reference transcription never sees the ASR text (an independent second opinion), and timing always
comes from Apple's per-word clock — Gemini only changes TEXT.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

from . import gemini

ROOT = Path(__file__).resolve().parent.parent
ASR_BIN = ROOT / "bin" / "apple-asr"

STAGES = ["asr", "reference", "correct", "cues", "translate"]

DEFAULT_OPTIONS = {
    "locale": "en-US",
    "context": "",                 # free text: who/what is in the video
    "glossary": "",                # comma/newline separated names & terms
    "ref_model": "gemini-flash-latest",
    "text_model": "gemini-flash-latest",
    "chunk_minutes": 10,
    "drop_fillers": True,          # um / uh / 嗯 / 呃
    "semantic_breaks": True,       # Gemini watches/listens and picks subtitle breaks; rules only as fallback
    "seg_thinking": "low",         # Gemini thinking level for segmentation: low / medium / high
    "max_chars": 0,                # 0 = auto by script (Latin 70, CJK 22)
    "max_duration": 6.5,
    "gap_break": 0.7,
    "translate": True,
    "target_lang": "繁體中文（台灣）",
    "karaoke": False,              # word-by-word highlight, in the web preview AND the burned-in video
    "sub_order": "tr-first",       # bilingual line order, shared by the web preview and burned-in video
    "translate_style": "台灣用語、口語通順自然；人名用台灣慣用譯名；保留品牌與專有名詞原文",
    "workers": 4,
}

# ───────────────────────────── text units ─────────────────────────────

CJK = r"぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
CJK_PUNCT = "，。、！？；：「」『』（）《》〈〉…—～·"
_tok_re = re.compile(rf"[{CJK}]|[^\s{CJK}{re.escape(CJK_PUNCT)}]+|[{re.escape(CJK_PUNCT)}]")


def is_cjk_char(c: str) -> bool:
    return bool(re.match(rf"[{CJK}]", c))


def tokenize(text: str) -> list[str]:
    """Display tokens: Latin words (punctuation attached) or single CJK chars; CJK punctuation attaches left."""
    out: list[str] = []
    for m in _tok_re.finditer(text):
        t = m.group(0)
        if (t in CJK_PUNCT or not norm(t)) and out:
            latin_dash = t in "—–-" and not is_cjk_char(out[-1][-1])
            out[-1] += (" " + t) if latin_dash else t
        else:
            out.append(t)
    return out


def norm(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", t).lower() if c.isalnum())


NOSPACE_PUNCT = "，。、！？；：「」『』（）《》〈〉～·"


def join_tokens(tokens: list[str]) -> str:
    """Spaces between Latin words and between Latin/CJK (Taiwan style: 叫 InterPositive 的), none inside CJK."""
    s = ""
    for t in tokens:
        if s and not (s[-1] in NOSPACE_PUNCT or t[0] in NOSPACE_PUNCT
                      or (is_cjk_char(s[-1]) and is_cjk_char(t[0]))):
            s += " "
        s += t
    return s


def is_cjk_text(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(is_cjk_char(c) for c in letters) / len(letters) > 0.5


def fmt_mmss(t: float) -> str:
    t = max(0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_mmss(s: str) -> float | None:
    try:
        parts = [float(p) for p in str(s).strip().split(":")]
    except ValueError:
        return None
    v = 0.0
    for p in parts:
        v = v * 60 + p
    return v


# ───────────────────────────── project ─────────────────────────────

class Project:
    def __init__(self, path: Path, on_change=None):
        self.dir = path
        self.on_change = on_change
        self.usage = gemini.Usage()

    @property
    def meta(self) -> dict:
        return json.loads((self.dir / "project.json").read_text())

    def update(self, **kw):
        m = self.meta
        m.update(kw)
        (self.dir / "project.json").write_text(json.dumps(m, ensure_ascii=False, indent=1))
        if self.on_change:
            self.on_change()

    def status(self, **kw):
        m = self.meta
        m.setdefault("status", {}).update(kw)
        m["usage"] = self._merged_usage(m.get("usage_base", {}))
        (self.dir / "project.json").write_text(json.dumps(m, ensure_ascii=False, indent=1))

    def _merged_usage(self, base: dict) -> dict:
        cur = self.usage.summary()
        return {"usd": round(base.get("usd", 0) + cur["usd"], 4), "models": cur["models"],
                "previous_usd": base.get("usd", 0)}

    def log(self, msg: str):
        line = time.strftime("%H:%M:%S ") + msg
        with open(self.dir / "log.txt", "a") as f:
            f.write(line + "\n")
        print(f"[{self.dir.name}] {line}", flush=True)

    def read(self, name: str):
        p = self.dir / name
        return json.loads(p.read_text()) if p.exists() else None

    def write(self, name: str, obj):
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=1))

    @property
    def options(self) -> dict:
        return {**DEFAULT_OPTIONS, **self.meta.get("options", {})}

    @property
    def media(self) -> Path:
        return Path(self.meta["media"])


def run_stages(p: Project, stages: list[str]):
    m = p.meta
    p.update(usage_base={"usd": (m.get("usage") or {}).get("usd", 0)})
    p.status(state="running", stage=None, progress=0, error=None, started=time.time())
    try:
        for st in STAGES:
            if st not in stages:
                continue
            if st == "translate" and not p.options["translate"]:
                continue
            p.status(stage=st, progress=0)
            p.log(f"▶ {st}")
            t0 = time.time()
            STAGE_FN[st](p)
            p.log(f"✓ {st}（{time.time() - t0:.1f}s）")
            done = set(p.meta.get("done", [])) | {st}
            p.update(done=sorted(done, key=STAGES.index))
        p.status(state="done", stage=None, progress=1)
    except Exception as e:  # surface to the GUI
        p.log(f"✗ {type(e).__name__}: {e}")
        p.status(state="error", error=str(e))
        raise


# ───────────────────────────── 1. Apple ASR ─────────────────────────────

def stage_asr(p: Project):
    o = p.options
    wav = p.dir / "audio.wav"
    if not wav.exists():
        p.log("抽出音軌（16 kHz mono）")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(p.media), "-vn", "-ac", "1", "-ar", "16000",
                        str(wav)], check=True)
    if not ASR_BIN.exists():
        raise RuntimeError("bin/apple-asr 不存在，先跑 ./setup.sh")
    proc = subprocess.Popen([str(ASR_BIN), str(wav), "--locale", o["locale"]], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    duration = None
    errs = []

    def pump():
        nonlocal duration
        for line in proc.stderr:
            line = line.strip()
            if line.startswith("PROGRESS") and duration:
                p.status(progress=min(0.99, float(line.split()[1]) / duration))
            elif line.startswith("DURATION"):
                duration = float(line.split()[1])
            elif line:
                errs.append(line)
                p.log("  " + line)

    import threading
    th = threading.Thread(target=pump, daemon=True)
    th.start()
    out = proc.stdout.read()
    proc.wait()
    th.join()
    if proc.returncode != 0:
        raise RuntimeError("apple-asr 失敗：" + " / ".join(errs[-3:]))
    data = json.loads(out)
    # fill missing word times by interpolation inside the segment
    for seg in data["segments"]:
        ws = seg["words"]
        for i, w in enumerate(ws):
            if w.get("start") is None:
                prev = next((x["end"] for x in reversed(ws[:i]) if x.get("end") is not None), seg["start"])
                w["start"] = prev
            if w.get("end") is None:
                nxt = next((x["start"] for x in ws[i + 1:] if x.get("start") is not None), seg["end"])
                w["end"] = max(w["start"], nxt)
    p.write("asr.json", data)
    nwords = sum(len(s["words"]) for s in data["segments"])
    p.update(duration=data["duration"])
    p.log(f"ASR：{len(data['segments'])} 段、{nwords} 字（{data['locale']}）")


# ───────────────────────────── 2. Gemini reference ─────────────────────────────

REF_SCHEMA = {
    "type": "object",
    "properties": {
        "speakers": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "who": {"type": "string"}}, "required": ["label", "who"]}},
        "terms": {"type": "array", "items": {"type": "string"}},
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "t": {"type": "string", "description": "start time MM:SS relative to clip start"},
            "speaker": {"type": "string"},
            "text": {"type": "string"}}, "required": ["t", "speaker", "text"]}},
    },
    "required": ["speakers", "terms", "lines"],
}


def chunks(duration: float, minutes: float) -> list[tuple[float, float]]:
    size = max(60.0, minutes * 60)
    n = max(1, round(duration / size))
    step = duration / n
    return [(i * step, duration if i == n - 1 else (i + 1) * step) for i in range(n)]


def media_has_video(p: Project) -> bool:
    return bool(subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=index", "-of", "csv=p=0",
         str(p.media)]).strip())


def make_proxy(p: Project, a: float, b: float, out: Path, has_video: bool) -> str:
    """Low-res (360p / 2 fps) excerpt for Gemini video understanding. Returns the MIME type."""
    vf = ["-vf", "scale=-2:360,fps=2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32"] if has_video \
        else ["-vn"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(p.media),
                    *vf, "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(out)], check=True)
    return "video/mp4" if has_video else "audio/mp4"


def stage_reference(p: Project):
    o = p.options
    duration = p.meta.get("duration") or float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p.media)]))
    spans = chunks(duration, o["chunk_minutes"])
    pad = 8.0
    (p.dir / "reference").mkdir(exist_ok=True)
    has_video = media_has_video(p)
    done = [0]

    def one(i_span):
        i, (a, b) = i_span
        out = p.dir / "reference" / f"chunk_{i:03d}.json"
        if out.exists():
            done[0] += 1
            return
        a0, b0 = max(0, a - pad), min(duration, b + pad)
        proxy = p.dir / "reference" / f"chunk_{i:03d}.mp4"
        mime = make_proxy(p, a0, b0, proxy, has_video)
        p.log(f"  參考稿 {i + 1}/{len(spans)}：上傳 {fmt_mmss(a0)}–{fmt_mmss(b0)}（{proxy.stat().st_size / 1e6:.1f} MB）")
        f = gemini.upload_file(proxy, mime, log=p.log)
        try:
            ctx = f" Context about the video: {o['context'].strip()}" if o["context"].strip() else ""
            gl = f" Names/terms that may appear (use these spellings): {o['glossary'].strip()}" \
                if o["glossary"].strip() else ""
            prompt = (
                f"This clip is a {fmt_mmss(b0 - a0)} excerpt of a longer video.{ctx}{gl}\n"
                "Watch and listen carefully, then produce a VERBATIM transcript of ALL speech in order, exactly "
                "as spoken (keep fillers like 'like', 'you know', repeated words and false starts; do not "
                "paraphrase, summarize, or translate). One line per sentence or short phrase (max ~20 words). "
                "`t` = when that line starts, as MM:SS relative to the START OF THIS CLIP. `speaker` = a stable "
                "label; use the person's real name when the video makes it clear (introductions, on-screen "
                "text, context), otherwise Host / Guest / Speaker 1… List every speaker in `speakers` with a "
                "short description, and every proper noun, brand, title or technical term heard in `terms`, "
                "spelled correctly. Use visual cues (lip movement, on-screen text) to resolve unclear words."
            )
            data, served = gemini.generate_json(
                o["ref_model"], [{"file_data": {"mime_type": mime, "file_uri": f["uri"]}}, {"text": prompt}],
                schema=REF_SCHEMA, low_res=True, usage=p.usage, log=p.log, max_tokens=65536)
        finally:
            gemini.delete_file(f["name"])
            proxy.unlink(missing_ok=True)
        lines = []
        for ln in data.get("lines", []):
            t = parse_mmss(ln.get("t", ""))
            if t is None or not ln.get("text", "").strip():
                continue
            lines.append({"t": round(a0 + t, 2), "speaker": ln.get("speaker", ""), "text": ln["text"].strip()})
        p.write(f"reference/chunk_{i:03d}.json", {"span": [a, b], "padded": [a0, b0], "model": served,
                                                  "speakers": data.get("speakers", []),
                                                  "terms": data.get("terms", []), "lines": lines})
        done[0] += 1
        p.status(progress=done[0] / len(spans))
        p.log(f"  參考稿 {i + 1}/{len(spans)}：{len(lines)} 行（{served}）")

    with ThreadPoolExecutor(max_workers=min(3, o["workers"])) as ex:
        list(ex.map(one, enumerate(spans)))
    p.write("reference.json", load_reference(p))


def load_reference(p: Project) -> dict:
    lines, terms, speakers, spans = [], [], {}, []
    for f in sorted((p.dir / "reference").glob("chunk_*.json")):
        d = json.loads(f.read_text())
        a, b = d["span"]
        spans.append([a, b])
        lines += [ln for ln in d["lines"] if a - 3 <= ln["t"] < b + 3]
        terms += d.get("terms", [])
        for s in d.get("speakers", []):
            speakers.setdefault(s["label"], s["who"])
    lines.sort(key=lambda x: x["t"])
    return {"spans": spans, "lines": lines, "terms": sorted(set(terms), key=str.lower),
            "speakers": speakers}


# ───────────────────────────── 3. correction ─────────────────────────────

CORR_SCHEMA = {
    "type": "object",
    "properties": {"segments": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "text": {"type": "string"}}, "required": ["id", "text"]}}},
    "required": ["segments"],
}


def stage_correct(p: Project):
    o = p.options
    asr = p.read("asr.json")
    ref = p.read("reference.json") or {"spans": [], "lines": [], "terms": [], "speakers": {}}
    segs = asr["segments"]
    duration = asr["duration"]
    spans = ref["spans"] or chunks(duration, o["chunk_minutes"])
    glossary = [t.strip() for t in re.split(r"[,\n，、]", o["glossary"]) if t.strip()]
    terms = sorted(set(glossary + ref.get("terms", [])), key=str.lower)

    filler = ("Remove pure hesitation fillers (um, uh, er, 嗯, 呃) — just omit them. Keep discourse words "
              "that carry speech style (like, you know, I mean, 就是, 然後)." if o["drop_fillers"] else
              "Keep every filler (um, uh, 嗯, 呃) exactly as spoken.")
    system = (
        "You fix speech-recognition (ASR) errors in a transcript. You get ASR segments (with their time "
        "range) and an INDEPENDENT REFERENCE transcript of the same audio made by a video-understanding "
        "model that watched the video. For every ASR segment return the corrected text.\n"
        "Rules:\n"
        "- Fix misheard words, homophones, wrong word boundaries, names, brands and technical terms. When the "
        "ASR and the REFERENCE disagree, prefer whichever is more plausible in context; the REFERENCE is "
        "usually better on names and rare words, the ASR is usually better on exact wording.\n"
        "- If the ASR clearly DROPPED words that the REFERENCE has at that moment, add them back.\n"
        "- Stay VERBATIM: do not paraphrase, summarize, reorder, translate, or tidy the grammar of what was "
        "said. Keep repeated words and false starts. Keep contractions and casual forms exactly as spoken "
        "(gonna, wanna, kinda, 'cause stay as they are).\n"
        "- Quoted or reported speech: use plain double quotes \"…\" (Chinese 「…」) and keep every quote mark "
        "BALANCED inside its segment; never leave a stray or mismatched quote, never mix ' and \".\n"
        f"- {filler}\n"
        "- Fix punctuation, casing and sentence boundaries so the text reads naturally as subtitles. Use the "
        "language's own punctuation (Chinese uses full-width ，。？！ and 「」).\n"
        "- Keep each segment's content in THAT segment — never move words to a neighbouring segment, never "
        "merge or split segments.\n"
        "- If the speaker changes INSIDE one segment (see REFERENCE speaker labels), insert ' || ' at the "
        "exact point of the change.\n"
        "- Return exactly one entry per supplied id, same order."
        + (f"\nKnown names/terms (use these spellings): {', '.join(terms)}" if terms else "")
        + (f"\nContext: {o['context'].strip()}" if o["context"].strip() else "")
    )

    groups: list[list[int]] = [[] for _ in spans]
    for i, s in enumerate(segs):
        k = next((j for j, (a, b) in enumerate(spans) if a <= s["start"] < b), len(spans) - 1)
        groups[k].append(i)

    result: dict[int, str] = {}
    cache = p.read("corrected.json") or {}
    done = [0]

    def one(k):
        idxs = groups[k]
        if not idxs:
            return
        key = f"{k}:{len(idxs)}:{idxs[0]}"
        if cache.get("chunks", {}).get(key):
            for i, t in cache["chunks"][key].items():
                result[int(i)] = t
            done[0] += 1
            return
        a = segs[idxs[0]]["start"] - 10
        b = segs[idxs[-1]]["end"] + 10
        ref_lines = [f"[{fmt_mmss(ln['t'])}] {ln['speaker']}: {ln['text']}" for ln in ref["lines"]
                     if a <= ln["t"] <= b]
        asr_lines = [f"[s{i} {fmt_mmss(segs[i]['start'])}–{fmt_mmss(segs[i]['end'])}] {segs[i]['text']}"
                     for i in idxs]
        prompt = ("REFERENCE:\n" + ("\n".join(ref_lines) or "(none)") + "\n\nASR SEGMENTS:\n"
                  + "\n".join(asr_lines))
        got = {}
        for attempt in range(3):
            data, served = gemini.generate_json(o["text_model"], [{"text": prompt}], system=system,
                                                schema=CORR_SCHEMA, usage=p.usage, log=p.log, max_tokens=65536)
            got = {}
            for item in data.get("segments", []):
                sid = item.get("id", "").lstrip("s")
                if sid.isdigit() and int(sid) in idxs:
                    got[int(sid)] = item.get("text", "")
            missing = [i for i in idxs if i not in got]
            if len(missing) <= max(1, len(idxs) // 50):
                break
            p.log(f"  校正區塊 {k + 1}：缺 {len(missing)} 段，重試")
        for i in idxs:
            result[i] = got.get(i, segs[i]["text"])
        with lock:
            cache.setdefault("chunks", {})[key] = {str(i): result[i] for i in idxs}
            p.write("corrected.json", cache)
        done[0] += 1
        p.status(progress=done[0] / len(groups))
        changed = sum(norm(result[i]) != norm(segs[i]["text"]) for i in idxs)
        p.log(f"  校正區塊 {k + 1}/{len(groups)}：{len(idxs)} 段，改了 {changed} 段")

    import threading
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=o["workers"]) as ex:
        list(ex.map(one, range(len(groups))))
    cache["segments"] = [result.get(i, s["text"]) for i, s in enumerate(segs)]
    p.write("corrected.json", cache)


# ───────────────────────────── 4. alignment + cues ─────────────────────────────

def asr_units(seg: dict) -> list[dict]:
    """Split ASR words into display tokens (CJK → per char), spreading each word's time by length."""
    out = []
    for w in seg["words"]:
        toks = tokenize(w["text"]) or [w["text"]]
        total = sum(max(1, len(norm(t))) for t in toks)
        s, dur = w["start"], max(0.0, w["end"] - w["start"])
        for t in toks:
            d = dur * max(1, len(norm(t))) / total
            out.append({"t": t, "s": s, "e": s + d})
            s += d
    return out


def align_segment(seg: dict, corrected: str) -> list[dict]:
    return align_window([seg], [corrected])


def align_window(segs: list[dict], texts: list[str], seg_offset: int = 0) -> list[dict]:
    """Map corrected text onto the ASR word clock across a window of segments. The LLM sometimes moves words
    into a neighbouring segment, so we match the whole window's text at once instead of segment by segment.
    Equal / 1:1-replaced tokens keep ASR times; the rest are spread over the matching ASR span or gap."""
    src: list[dict] = []
    for si, seg in enumerate(segs):
        for u in asr_units(seg):
            u["seg"] = seg_offset + si
            src.append(u)
    dst: list[dict] = []
    for text in texts:
        for pi, part in enumerate(text.split("||")):
            for j, t in enumerate(tokenize(part.strip())):
                dst.append({"t": t, "spk": pi > 0 and j == 0})
    if not dst:
        return []
    w_lo, w_hi = segs[0]["start"], segs[-1]["end"]
    if not src:
        spread(dst, w_lo, w_hi)
        for u in dst:
            u["seg"] = seg_offset
        return dst
    sm = SequenceMatcher(None, [norm(u["t"]) for u in src], [norm(u["t"]) for u in dst], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(j2 - j1):
                u = src[i1 + k]
                dst[j1 + k].update(s=u["s"], e=u["e"], seg=u["seg"])
        elif tag == "replace":
            spread(dst[j1:j2], src[i1]["s"], src[i2 - 1]["e"])
            for u in dst[j1:j2]:
                u["seg"] = src[i1]["seg"]
        elif tag == "insert":
            lo = src[i1 - 1]["e"] if i1 > 0 else w_lo
            hi = src[i1]["s"] if i1 < len(src) else w_hi
            if hi - lo < 0.08 * (j2 - j1):  # no room: borrow a little from both neighbours
                lo, hi = max(w_lo, lo - 0.12), min(w_hi, hi + 0.12)
            spread(dst[j1:j2], lo, max(lo, hi))
            for u in dst[j1:j2]:
                u["seg"] = src[i1 - 1]["seg"] if i1 > 0 else src[0]["seg"]
    last = w_lo
    for u in dst:  # monotonic clean-up
        u["s"] = max(u.get("s", last), last)
        u["e"] = max(u.get("e", u["s"]), u["s"])
        u.setdefault("seg", seg_offset)
        last = u["s"]
    unsqueeze(dst, w_lo, w_hi)
    return dst


def unsqueeze(dst: list[dict], lo: float, hi: float, min_word: float = 0.04):
    """Words the correction added where the ASR heard nothing get crammed into tiny gaps. Give such a run the
    silence around it plus the tail of the previous word — but never move the START of a word the ASR timed
    (that is what keeps subtitles and karaoke in sync). Runs that still don't fit are marked as estimated."""
    i, n = 0, len(dst)
    while i < n:
        if dst[i]["e"] - dst[i]["s"] >= min_word:
            i += 1
            continue
        j = i
        while j + 1 < n and dst[j + 1]["e"] - dst[j + 1]["s"] < min_word:
            j += 1
        if j == i:  # a single short word is normal (a, I, the)
            i += 1
            continue
        a0 = dst[i - 1]["s"] + 0.6 * (dst[i - 1]["e"] - dst[i - 1]["s"]) if i > 0 else lo
        b0 = dst[j + 1]["s"] if j + 1 < n else hi
        a0 = min(a0, dst[i]["s"])
        if b0 - a0 > dst[j]["e"] - dst[i]["s"]:
            if i > 0:
                dst[i - 1]["e"] = min(dst[i - 1]["e"], a0)
            spread(dst[i:j + 1], a0, b0)
        for u in dst[i:j + 1]:
            u["est"] = True
        i = j + 1

def spread(units: list[dict], lo: float, hi: float):
    total = sum(max(1, len(norm(u["t"]))) for u in units) or 1
    s = lo
    for u in units:
        d = (hi - lo) * max(1, len(norm(u["t"]))) / total
        u["s"], u["e"] = s, s + d
        s += d


SENT_END = tuple(".?!。？！…」")
CLAUSE_END = tuple(",;:，；：、—")
CONJ = {"and", "but", "so", "because", "which", "that", "or", "when", "if", "where", "who", "then", "like"}


def cue_limits(words: list[dict], o: dict) -> tuple[bool, int]:
    cjk = is_cjk_text(join_tokens([w["t"] for w in words[:400]]))
    return cjk, o["max_chars"] or (22 if cjk else 70)


def build_cues(words: list[dict], o: dict) -> list[dict]:
    return finalize_cues(heuristic_groups(words, o))


def heuristic_groups(words: list[dict], o: dict) -> list[list[dict]]:
    """Rule-based grouping: punctuation, pauses, speaker changes, length — used as fallback and to split
    over-long semantic groups."""
    cjk, max_chars = cue_limits(words, o)
    max_dur, gap_break = o["max_duration"], o["gap_break"]

    def clen(ws):
        return len(join_tokens([w["t"] for w in ws]))

    cues: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        if cur:
            gap = w["s"] - cur[-1]["e"]
            prev = cur[-1]["t"]
            force = w.get("spk") or gap > gap_break or w.get("seg") != cur[-1].get("seg") and gap > 0.35
            sentence = prev.endswith(SENT_END) and (clen(cur) >= max_chars * 0.4 or gap > 0.25)
            too_long = clen(cur + [w]) > max_chars or w["e"] - cur[0]["s"] > max_dur
            if force or sentence:
                cues.append(cur)
                cur = []
            elif too_long:
                # backtrack to the best break point inside cur
                best, best_score = len(cur), -1.0
                for k in range(1, len(cur)):
                    left = clen(cur[:k])
                    if left < max_chars * 0.35:
                        continue
                    t = cur[k - 1]["t"]
                    score = (3 if t.endswith(SENT_END) else 2 if t.endswith(CLAUSE_END) else
                             1 if norm(cur[k]["t"]) in CONJ else 0) + left / max_chars * 0.5
                    score += min(cur[k]["s"] - cur[k - 1]["e"], 0.5)
                    if score > best_score:
                        best, best_score = k, score
                cues.append(cur[:best])
                cur = cur[best:]
        cur.append(w)
    if cur:
        cues.append(cur)
    return cues


def finalize_cues(cues: list[list[dict]]) -> list[dict]:
    # a cue whose successor starts almost at once (an overlapping "Yeah." from the other speaker) can't be
    # shown on its own — fold it into the next cue
    merged: list[list[dict]] = []
    for ws in reversed(cues):
        if merged and merged[0][0]["s"] - ws[0]["s"] < 0.3:
            merged[0] = ws + merged[0]
        else:
            merged.insert(0, ws)
    cues = merged

    LEAD, HOLD, MIN_SHOW, GAP = 0.1, 0.6, 1.0, 0.04
    # pass 1 — starts: a little lead-in, but only into silence (never before the previous line's last word)
    starts = []
    for i, ws in enumerate(cues):
        floor = cues[i - 1][-1]["e"] + GAP if i else 0.0
        starts.append(max(min(ws[0]["s"] - LEAD, ws[0]["s"]), min(floor, ws[0]["s"]), 0.0))
    # pass 2 — ends: hold after the last word, readable minimum, never past the next line's start
    out = []
    for i, ws in enumerate(cues):
        start = starts[i]
        limit = starts[i + 1] - GAP if i + 1 < len(cues) else ws[-1]["e"] + 10
        end = min(ws[-1]["e"] + HOLD, limit)
        if end - start < MIN_SHOW:
            end = min(start + MIN_SHOW, limit)
        end = max(end, min(ws[-1]["e"], limit), start + 0.05)
        out.append({"start": round(start, 3), "end": round(end, 3),
                    "text": join_tokens([w["t"] for w in ws]),
                    "words": [{"t": w["t"], "s": round(w["s"], 3), "e": round(w["e"], 3)} for w in ws],
                    "spk": bool(ws[0].get("spk")), "est": sum(1 for w in ws if w.get("est")) >= 2})
    return out


SEG_SCHEMA = {"type": "object", "properties": {"breaks": {"type": "array", "items": {"type": "integer"}}},
              "required": ["breaks"]}


def seg_windows(words: list[dict], minutes: float) -> list[list[int]]:
    """~N-minute windows of word indices, each ending at the longest pause near its end so no window
    boundary falls mid-sentence."""
    size = max(60.0, minutes * 60)
    out, start = [], 0
    while start < len(words):
        t_end = words[start]["s"] + size
        end = next((i for i in range(start, len(words)) if words[i]["s"] >= t_end), len(words))
        if end < len(words):
            lo = next((i for i in range(start + 1, end) if words[i]["s"] >= t_end - 30), start + 1)
            end = max(range(lo, end + 1), key=lambda i: words[i]["s"] - words[i - 1]["e"])
        out.append(list(range(start, end)))
        start = end
    return out


def semantic_groups(p: Project, words: list[dict], o: dict) -> list[list[dict]]:
    """Gemini watches and listens to each stretch of the video together with the numbered transcript words,
    and splits them into subtitle lines the way a human subtitler would — by rhythm, pauses and meaning."""
    cjk, max_chars = cue_limits(words, o)
    windows = seg_windows(words, o["chunk_minutes"])
    has_video = media_has_video(p)
    (p.dir / "segment").mkdir(exist_ok=True)
    system = (
        "You are a professional subtitler. Watch and listen to the clip, then split the given transcript "
        "words into subtitle lines exactly as a skilled human subtitler would for this video: follow the "
        "speaker's rhythm, pauses, emphasis and meaning, so every line reads naturally on screen and appears "
        f"in step with the speech. A comfortable line is roughly {max_chars} characters or less. Words are "
        "given as [index@seconds]word, with seconds relative to the start of the clip. Return `breaks`: the "
        "indices of words AFTER which a new subtitle line starts, in ascending order."
        + (f"\nContext: {o['context'].strip()}" if o["context"].strip() else "")
    )
    done, result = [0], {}

    def one(k):
        idxs = windows[k]
        a0 = max(0.0, words[idxs[0]]["s"] - 1.0)
        b0 = words[idxs[-1]]["e"] + 1.0
        proxy = p.dir / "segment" / f"window_{k:03d}.mp4"
        try:
            mime = make_proxy(p, a0, b0, proxy, has_video)
            f = gemini.upload_file(proxy, mime, log=p.log)
            try:
                text = " ".join(f"[{i}@{words[i]['s'] - a0:.1f}]{words[i]['t']}" for i in idxs)
                data, _ = gemini.generate_json(
                    o["text_model"], [{"file_data": {"mime_type": mime, "file_uri": f["uri"]}}, {"text": text}],
                    system=system, schema=SEG_SCHEMA, low_res=True, usage=p.usage, log=p.log,
                    max_tokens=32768, thinking=o.get("seg_thinking") or None)
            finally:
                gemini.delete_file(f["name"])
            brks = {int(x) for x in data.get("breaks", []) if idxs[0] <= int(x) < idxs[-1]}
            if not brks and len(idxs) > 30:
                raise ValueError("no breaks returned")
        except Exception as e:
            p.log(f"  語意斷句區塊 {k + 1} 失敗，這段改用規則斷句：{e}")
            result[k] = heuristic_groups([words[i] for i in idxs], o)
            return
        finally:
            proxy.unlink(missing_ok=True)
        groups, cur = [], []
        for i in idxs:
            if cur and i - 1 in brks:
                groups.append(cur)
                cur = []
            cur.append(words[i])
        if cur:
            groups.append(cur)
        # safety net only for clearly broken output (a "line" far too long to display)
        out = []
        for g in groups:
            broken = len(join_tokens([w["t"] for w in g])) > max_chars * 2.5
            out += heuristic_groups(g, o) if broken else [g]
        result[k] = out
        done[0] += 1
        p.status(progress=done[0] / len(windows))
        p.log(f"  語意斷句 {k + 1}/{len(windows)}：{len(out)} 條")

    with ThreadPoolExecutor(max_workers=min(3, o["workers"])) as ex:
        list(ex.map(one, range(len(windows))))
    return [g for k in range(len(windows)) for g in result[k]]


def stage_cues(p: Project):
    o = p.options
    asr = p.read("asr.json")
    corr = (p.read("corrected.json") or {}).get("segments")
    if not corr or len(corr) != len(asr["segments"]):
        p.log("  （沒有校正結果，直接用 ASR 原文）")
        corr = [s["text"] for s in asr["segments"]]
    words = []
    win = 60
    segs = asr["segments"]
    for k in range(0, len(segs), win):
        words += align_window(segs[k:k + win], corr[k:k + win], seg_offset=k)
    old_doc = p.read("cues.json") or {}
    old_cues = old_doc.get("cues", [])
    reuse = o.get("reuse_breaks", True) and not old_doc.get("edited") and old_cues and \
        [w["t"] for c in old_cues for w in (c.get("words") or [])] == [w["t"] for w in words]
    if reuse:
        p.log("  字詞與上次相同，沿用上次的斷句（只更新時間）")
        groups, k = [], 0
        for c in old_cues:
            groups.append(words[k:k + len(c["words"])])
            k += len(c["words"])
    elif o.get("semantic_breaks", True) and words and os.environ.get("GEMINI_API_KEY"):
        groups = semantic_groups(p, words, o)
    else:
        groups = heuristic_groups(words, o)
    cues = finalize_cues(groups)
    # keep the raw ASR text per cue for the "what changed" view
    asr_words = [(u["s"], u["t"]) for seg in asr["segments"] for u in asr_units(seg)]
    for c in cues:
        c["asr"] = join_tokens([t for s, t in asr_words if c["start"] - 0.01 <= s < c["end"]])
    old = p.read("cues.json")
    if old and o["translate"]:  # carry translations over when text is unchanged
        tr = {norm(c["text"]): c.get("tr", "") for c in old.get("cues", [])}
        for c in cues:
            c["tr"] = tr.get(norm(c["text"]), "")
    for i, c in enumerate(cues):
        c["id"] = i + 1
    p.write("cues.json", {"cues": cues, "edited": False})
    p.log(f"字幕：{len(cues)} 條")


# ───────────────────────────── 5. translation ─────────────────────────────

TR_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "integer"}, "tr": {"type": "string"}}, "required": ["id", "tr"]}}},
    "required": ["items"],
}


def stage_translate(p: Project, only_missing: bool = False):
    o = p.options
    doc = p.read("cues.json")
    cues = doc["cues"]
    ref = p.read("reference.json") or {}
    glossary = o["glossary"].strip()
    system = (
        f"You translate subtitles into {o['target_lang']}. Style: {o['translate_style']}.\n"
        "Each cue is one fragment of continuous speech. Translate EACH cue so its translation covers that "
        "cue's own content (do not move content into neighbouring cues), while the sequence reads naturally. "
        "Keep it concise for on-screen reading; no trailing full stop on a cue unless it is a question or "
        "exclamation. Return every id."
        + (f"\nGlossary / names: {glossary}" if glossary else "")
        + (f"\nSpeakers: {json.dumps(ref.get('speakers', {}), ensure_ascii=False)}" if ref.get("speakers") else "")
        + (f"\nContext: {o['context'].strip()}" if o["context"].strip() else "")
    )
    todo = [i for i, c in enumerate(cues) if not (only_missing and c.get("tr"))]
    batches = [todo[i:i + 60] for i in range(0, len(todo), 60)]
    done = [0]
    import threading
    lock = threading.Lock()

    def one(batch):
        ctx_before = cues[max(0, batch[0] - 4):batch[0]]
        ctx_after = cues[batch[-1] + 1:batch[-1] + 3]
        prompt = ("Context before (do not translate): " + " / ".join(c["text"] for c in ctx_before) + "\n"
                  "Context after (do not translate): " + " / ".join(c["text"] for c in ctx_after) + "\n\n"
                  "CUES:\n" + "\n".join(f"{cues[i]['id']}\t{cues[i]['text']}" for i in batch))
        data, _ = gemini.generate_json(o["text_model"], [{"text": prompt}], system=system, schema=TR_SCHEMA,
                                       usage=p.usage, log=p.log, max_tokens=32768, thinking="low")
        got = {it["id"]: it["tr"].strip() for it in data.get("items", [])}
        with lock:
            for i in batch:
                if cues[i]["id"] in got:
                    cues[i]["tr"] = got[cues[i]["id"]]
            p.write("cues.json", doc)
            done[0] += 1
            p.status(progress=done[0] / len(batches))

    with ThreadPoolExecutor(max_workers=o["workers"]) as ex:
        list(ex.map(one, batches))
    missing = sum(1 for c in cues if not c.get("tr"))
    p.log(f"翻譯完成，{len(cues) - missing}/{len(cues)} 條" + (f"（{missing} 條缺譯，可再跑一次）" if missing else ""))


STAGE_FN = {"asr": stage_asr, "reference": stage_reference, "correct": stage_correct, "cues": stage_cues,
            "translate": stage_translate}


# ───────────────────────────── export ─────────────────────────────

def ts(t: float, sep=",") -> str:
    ms = int(round(max(0, t) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def export(cues: list[dict], kind: str) -> str:
    """kind: src | tr | bi (src over tr) | bi-tr (tr over src) | vtt | json"""
    if kind == "json":
        return json.dumps(cues, ensure_ascii=False, indent=1)
    vtt = kind == "vtt"
    out = ["WEBVTT", ""] if vtt else []
    n = 0
    for c in cues:
        tr = (c.get("tr") or "").replace("⏎", "\n")
        lines = {"src": [c["text"]], "tr": [tr], "bi": [c["text"], tr], "bi-tr": [tr, c["text"]],
                 "vtt": [c["text"]]}[kind]
        lines = [l for l in lines if l.strip()]
        if not lines:
            continue
        n += 1
        if not vtt:
            out.append(str(n))
        out.append(f"{ts(c['start'], '.' if vtt else ',')} --> {ts(c['end'], '.' if vtt else ',')}")
        out += lines
        out.append("")
    return "\n".join(out)
