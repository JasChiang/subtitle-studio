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

STAGES = ["proxy", "asr", "reference", "correct", "cues", "translate"]

DEFAULT_OPTIONS = {
    "locale": "auto",              # auto = Gemini listens to a few excerpts and picks the Apple ASR language
    "context": "",                 # free text: who/what is in the video
    "glossary": "",                # comma/newline separated names & terms
    "ref_model": "gemini-flash-latest",
    "text_model": "gemini-flash-latest",
    "chunk_minutes": 10,
    "drop_fillers": True,          # um / uh / 嗯 / 呃
    "semantic_breaks": True,       # Gemini watches/listens and picks subtitle breaks; rules only as fallback
    "seg_thinking": "low",         # Gemini thinking level for segmentation: low / medium / high
    "backchannels": "omit",        # omit | keep — listener's mm-hmm / yeah that carry no information
    "energy_snap": True,           # refine word boundaries with the audio loudness (pauses, onsets)
    "correct_with_audio": True,    # correction also listens to the audio of each chunk
    "reasr_segments": True,        # whole sentences in another language → re-run Apple ASR in that language
    "cjk_spacing": True,           # half-width space between CJK and Latin letters / digits in Chinese text
    "max_chars": 0,                # 0 = auto by script: Latin 42, Chinese / Japanese 16, Korean 20 per line
    "sub_scale": 100,              # subtitle font size, % of the default (preview and export alike)
    "loudnorm": True,              # exports: normalise loudness to -14 LUFS (YouTube's reference)
    "proxy": "auto",               # editing proxy for preview: auto (only when needed) | always | never
    "max_duration": 6.5,
    "gap_break": 0.7,
    "translate": True,
    "target_lang": "auto",         # auto = Chinese video → English, anything else → 繁體中文（台灣）
    "karaoke": False,              # word-by-word highlight, in the web preview AND the burned-in video
    "sub_order": "tr-first",       # bilingual line order, shared by the web preview and burned-in video
    "translate_style": "",         # empty = automatic for the target language (Taiwan wording for 繁體中文)
    "workers": 4,
}

# ───────────────────────────── text units ─────────────────────────────

# Han + kana are written without spaces → split per character. Hangul is NOT here: Korean separates words
# with spaces, so it is tokenised like Latin (splitting it per syllable would drop the spaces between words).
CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
HANGUL = "\uac00-\ud7af\u1100-\u11ff\u3130-\u318f"
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
    # keep combining marks (Devanagari / Tamil vowel signs …) — dropping them would make different words equal
    return "".join(c for c in unicodedata.normalize("NFKC", t).lower()
                   if c.isalnum() or unicodedata.category(c).startswith("M"))


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
    """Mostly Han / kana (written without spaces)."""
    return script_of(text) == "cjk"


def script_of(text: str) -> str:
    """'cjk' (Han / kana), 'hangul' or 'latin' (everything space-separated) — by majority of letters."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "latin"
    if sum(is_cjk_char(c) for c in letters) / len(letters) > 0.5:
        return "cjk"
    if sum(bool(re.match(f"[{HANGUL}]", c)) for c in letters) / len(letters) > 0.5:
        return "hangul"
    return "latin"


_ZH_WORDS = ("中文", "華語", "华语", "國語", "国语", "繁體", "繁体", "簡體", "简体", "粵", "粤", "chinese", "mandarin",
             "cantonese", "zh", "yue")


def is_chinese(lang: str) -> bool:
    l = (lang or "").lower()
    return any(w in l for w in _ZH_WORDS) and "japan" not in l and "日" not in l


def han_variant(o: dict) -> str:
    """Which Chinese glyph forms to draw Han characters with: TC (Taiwan), SC (mainland) or HK."""
    t = f"{o.get('target_lang', '')} {o.get('locale', '')}".lower()
    if any(w in t for w in ("簡體", "简体", "simplified", "zh-cn", "zh-hans", "大陆", "大陸")):
        return "SC"
    if any(w in t for w in ("香港", "zh-hk", "粵", "粤", "cantonese", "yue")):
        return "HK"
    return "TC"


def style_for(o: dict) -> str:
    """Translation style: the project's own text, or a sensible default for the target language."""
    if (o.get("translate_style") or "").strip():
        return o["translate_style"].strip()
    t = o.get("target_lang", "")
    if is_chinese(t) and han_variant(o) == "SC":
        return "使用中國大陸通行用語，口語自然通順；保留品牌與專有名詞原文"
    if is_chinese(t) and han_variant(o) == "HK":
        return "香港書面語用字，口語自然；人名用香港慣用譯名；保留品牌與專有名詞原文"
    if is_chinese(t):
        return "台灣用語、口語通順自然；人名用台灣慣用譯名；保留品牌與專有名詞原文"
    return ("natural, idiomatic subtitle language a native speaker of the target language would write; concise; "
            "keep brand and product names as they are usually written in that language")


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

    @property
    def ai_media(self) -> Path:
        """What Gemini watches / listens to: the small editing proxy when there is one (Gemini only needs 360p
        anyway, and cutting excerpts from a 4K ProRes original is slow). Exports and Apple ASR use the original."""
        proxy = self.dir / "proxy.mp4"
        return proxy if self.meta.get("proxy") and proxy.exists() else self.media


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


# ───────────────────────────── proxy (preview / editing) ─────────────────────────────

def probe_media(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=format_name,bit_rate,size,duration:stream=codec_type,codec_name,width,height,pix_fmt,"
                          "avg_frame_rate", "-of", "json", str(path)], capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    v = next((x for x in d.get("streams", []) if x.get("codec_type") == "video"), {})
    f = d.get("format", {})
    return {"container": f.get("format_name", ""), "ext": path.suffix.lower(), "bit_rate": int(f.get("bit_rate") or 0),
            "size": int(f.get("size") or 0), "duration": float(f.get("duration") or 0), "has_video": bool(v),
            "codec": v.get("codec_name", ""), "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
            "pix_fmt": v.get("pix_fmt", ""), "fps": v.get("avg_frame_rate", "")}


def needs_proxy(info: dict, mode: str = "auto") -> str | None:
    """Why the browser preview should use a proxy instead of the original — or None if it shouldn't."""
    if mode == "never" or not info.get("has_video"):
        return None
    if mode == "always":
        return "設定為一律使用代理檔"
    if info["codec"] not in ("h264", "vp9", "av1"):
        return f"瀏覽器不一定能播放 {info['codec'] or '這種'} 編碼"
    if "10" in info.get("pix_fmt", "") or "12" in info.get("pix_fmt", ""):
        return f"{info['pix_fmt']}（高位元深度）瀏覽器播放吃力"
    # ffprobe names MKV and WebM alike ("matroska,webm"), so tell them apart by extension
    if "matroska" in info["container"] and info.get("ext") != ".webm":
        return "MKV 容器瀏覽器不一定能播放"
    if not any(c in info["container"] for c in ("mp4", "mov", "matroska")):
        return f"{info['container']} 容器瀏覽器不一定能播放"
    if info["height"] > 1080:
        return f"{info['width']}×{info['height']} 解析度太高，預覽會卡"
    if info["bit_rate"] > 20_000_000:
        return f"碼率 {info['bit_rate'] / 1e6:.0f} Mbps 太高，預覽會卡"
    return None


def stage_proxy(p: Project):
    """A 720p H.264 copy for smooth preview / seeking (keyframe every second). Export still uses the original."""
    info = probe_media(p.media)
    why = needs_proxy(info, p.options.get("proxy", "auto"))
    out = p.dir / "proxy.mp4"
    if not why:
        out.unlink(missing_ok=True)
        p.update(proxy=None)
        p.log(f"  原片可以直接預覽（{info['codec']} {info['width']}×{info['height']}，"
              f"{info['bit_rate'] / 1e6:.1f} Mbps），不需要代理檔")
        return
    if out.exists() and p.meta.get("proxy") == "proxy.mp4":
        p.log("  已有代理檔，沿用")
        return
    p.log(f"  建立預覽用代理檔：{why}")
    hw = "h264_videotoolbox" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                                               text=True).stdout
    enc = ["-c:v", "h264_videotoolbox", "-b:v", "3M"] if hw else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    tmp = out.with_suffix(".part.mp4")
    proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(p.media),
                             "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:'min(720,ih)'", *enc,
                             "-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,n_forced*1)", "-fps_mode", "passthrough",
                             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
                             str(tmp)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and line.strip().split("=")[1].isdigit() and info["duration"]:
            p.status(progress=min(0.99, int(line.split("=")[1]) / 1e6 / info["duration"]))
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError("代理檔建立失敗：" + err[-400:])
    tmp.replace(out)
    p.update(proxy="proxy.mp4")
    p.log(f"  代理檔完成：{out.stat().st_size / 1e6:.0f} MB（原片 {info['size'] / 1e6:.0f} MB）")


# ───────────────────────────── 0. languages ─────────────────────────────

BASE_LOCALE = {"en": "en-US", "zh": "zh-TW", "cmn": "zh-TW", "yue": "yue-CN", "ja": "ja-JP", "ko": "ko-KR",
               "es": "es-ES", "fr": "fr-FR", "de": "de-DE", "it": "it-IT", "pt": "pt-BR", "hi": "hi-IN"}
_SUPPORTED: list[str] = []


def supported_locales() -> list[str]:
    global _SUPPORTED
    if not _SUPPORTED and ASR_BIN.exists():
        _SUPPORTED = subprocess.run([str(ASR_BIN), "--list-locales"], capture_output=True, text=True,
                                    timeout=60).stdout.split()
    return _SUPPORTED


def lang_base(code: str) -> str:
    c = (code or "").replace("_", "-").lower()
    if c.startswith(("yue", "zh-hk", "zh-yue")):
        return "yue" if c.startswith("yue") else "zh"
    return c.split("-")[0]


def to_locale(code: str, supported: list[str], prefer_region: str = "") -> str | None:
    """A language code from Gemini (en, zh-TW, zh-Hant, cmn, ja-JP, …) → a locale Apple ASR supports."""
    if not code:
        return None
    c = code.replace("_", "-")
    low = c.lower()
    alias = {"zh-hant": "zh-TW", "zh-hans": "zh-CN", "cmn-tw": "zh-TW", "cmn-cn": "zh-CN", "cmn": "zh-TW",
             "zh-yue": "yue-CN", "yue-hk": "zh-HK"}
    c = alias.get(low, c)
    by_lower = {x.lower(): x for x in supported}
    if c.lower() in by_lower:
        return by_lower[c.lower()]
    base = lang_base(c)
    for cand in ([f"{base}-{prefer_region}"] if prefer_region else []) + [BASE_LOCALE.get(base, "")]:
        if cand and cand.lower() in by_lower:
            return by_lower[cand.lower()]
    same = [x for x in supported if lang_base(x) == base]
    return same[0] if same else None


LANG_SCHEMA = {
    "type": "object",
    "properties": {
        "primary": {"type": "string", "description": "BCP-47 code of the main spoken language, e.g. zh-TW, en-US"},
        "languages": {"type": "array", "items": {"type": "object", "properties": {
            "code": {"type": "string"}, "name": {"type": "string"},
            "share": {"type": "number", "description": "rough share of the speech, 0–1"}},
            "required": ["code", "name", "share"]}},
        "mixing": {"type": "string", "enum": ["none", "words", "segments"],
                   "description": "none = one language; words = other-language words inside sentences; "
                                  "segments = whole sentences / turns switch language"},
    },
    "required": ["primary", "languages", "mixing"],
}


def detect_languages(p: Project) -> dict:
    """Gemini listens to a few short excerpts and reports the spoken languages (cheap: audio only)."""
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                         "csv=p=0", str(p.media)]))
    picks = [(0.0, dur)] if dur <= 150 else [(dur * f, 40.0) for f in (0.08, 0.45, 0.8)]
    clip = p.dir / "lang_sample.m4a"
    args, fc = [], ""
    for i, (a, d) in enumerate(picks):
        args += ["-ss", f"{a:.2f}", "-t", f"{min(d, dur - a):.2f}", "-i", str(p.ai_media)]
        fc += f"[{i}:a]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, "-filter_complex", f"{fc}concat=n={len(picks)}:v=0:a=1[a]",
                    "-map", "[a]", "-ac", "1", "-c:a", "aac", "-b:a", "48k", str(clip)], check=True)
    f = gemini.upload_file(clip, "audio/mp4", log=p.log)
    try:
        data, _ = gemini.generate_json(
            p.options["text_model"], [{"file_data": {"mime_type": "audio/mp4", "file_uri": f["uri"]}}, {"text": (
                "These are short excerpts of one video. Which languages are spoken, and roughly what share each? "
                "For Chinese say which variety (Taiwan Mandarin zh-TW, mainland Mandarin zh-CN, Cantonese yue). "
                "Say whether languages are mixed inside sentences (words) or switch by whole sentences / "
                "speakers (segments).")}],
            schema=LANG_SCHEMA, usage=p.usage, log=p.log, max_tokens=4096, thinking="low")
    finally:
        gemini.delete_file(f["name"])
        clip.unlink(missing_ok=True)
    p.write("languages.json", data)
    return data


def auto_target(spoken: str) -> str:
    """Default translation language for a spoken language: never translate a video into its own language."""
    return "English" if lang_base(spoken) in ("zh", "yue", "cmn") else "繁體中文（台灣）"


def resolve_target(p: Project) -> dict:
    """Turn target_lang 'auto' into a concrete language once the spoken language is known."""
    o = p.options
    if o.get("target_lang") == "auto":
        spoken = (p.read("languages.json") or {}).get("primary") or o.get("locale", "")
        tgt = auto_target(spoken)
        p.update(options={**p.meta.get("options", {}), "target_lang": tgt})
        p.log(f"  自動選擇翻譯語言：{tgt}（影片主要是 {spoken or '未知語言'}）")
    return p.options


def languages_note(p: Project) -> str:
    """'Spoken: Chinese (zh-TW) ~80%, English ~20%; mixed inside sentences' — for correction / translation prompts."""
    d = p.read("languages.json") or {}
    langs = [f"{l.get('name') or l.get('code')} ({l.get('code')}) ~{round(100 * float(l.get('share') or 0))}%"
             for l in d.get("languages", [])]
    if not langs:
        return ""
    mix = {"words": "other-language words inside sentences (code-switching)",
           "segments": "whole sentences / speakers switch language"}.get(d.get("mixing", ""), "")
    return "Spoken languages: " + ", ".join(langs) + (f"; {mix}" if mix else "")


def other_language_spans(ref_lines: list[dict], primary: str, words: list[dict],
                         min_len: float = 2.5) -> list[dict]:
    """Stretches where the reference transcript says a whole line is in another language. Consecutive lines
    in the same language are merged; edges snap to the widest pause between ASR words near them."""
    pb = lang_base(primary)
    lines = sorted((l for l in ref_lines if l.get("text", "").strip()), key=lambda l: l["t"])
    spans = []
    for i, l in enumerate(lines):
        lb = lang_base(l.get("lang", "")) or pb
        if lb == pb:
            continue
        end = lines[i + 1]["t"] if i + 1 < len(lines) else l["t"] + max(2.0, 0.35 * len(l["text"].split()))
        if spans and spans[-1]["lang"] == l["lang"] and l["t"] - spans[-1]["end"] <= 1.5:
            spans[-1]["end"] = end
        else:
            spans.append({"lang": l["lang"], "start": l["t"], "end": end})

    def snap(t):
        gaps = [((a["e"] + b["s"]) / 2, b["s"] - a["e"]) for a, b in zip(words, words[1:])
                if abs((a["e"] + b["s"]) / 2 - t) <= 1.2]
        return max(gaps, key=lambda g: g[1])[0] if gaps else t

    out = []
    for sp in spans:
        a, b = snap(sp["start"]), snap(sp["end"])
        if b - a >= min_len:
            out.append({"lang": sp["lang"], "start": round(a, 3), "end": round(b, 3)})
    return out


def merge_reasr(asr: dict, spans: list[dict], redone: list[list[dict]]) -> dict:
    """Replace the primary-language ASR inside each span with that span's re-recognised segments."""
    def inside(w):
        m = (w["start"] + w["end"]) / 2
        return any(sp["start"] <= m < sp["end"] for sp in spans)

    segs = []
    for seg in asr["segments"]:
        keep = [w for w in seg["words"] if not inside(w)]
        if not keep:
            continue
        if len(keep) == len(seg["words"]):
            segs.append(seg)
        else:
            segs.append({**seg, "text": join_tokens([w["text"] for w in keep]), "start": keep[0]["start"],
                         "end": keep[-1]["end"], "words": keep})
    for sp, new in zip(spans, redone):
        segs += [{**s, "lang": sp["lang"]} for s in new]
    segs.sort(key=lambda s: s["start"])
    return {**asr, "segments": segs, "reasr": spans}


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
    if not p.read("languages.json") and os.environ.get("GEMINI_API_KEY"):
        try:
            d = detect_languages(p)
            p.log(f"  語言判斷：{languages_note(p) or d}")
        except Exception as e:
            p.log(f"  語言判斷失敗：{e}")
    if o["locale"] == "auto":
        primary = (p.read("languages.json") or {}).get("primary", "")
        loc = to_locale(primary, supported_locales()) or "en-US"
        p.update(options={**p.meta.get("options", {}), "locale": loc})
        o = p.options
        p.log(f"  自動選擇辨識語言：{loc}" + ("" if primary else "（無法判斷，預設英文）"))
    o = resolve_target(p)
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
            "lang": {"type": "string", "description": "language code of this line, e.g. en, zh-TW, ja"},
            "text": {"type": "string"}}, "required": ["t", "speaker", "lang", "text"]}},
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
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(p.ai_media),
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
                "spelled correctly. Use visual cues (lip movement, on-screen text) to resolve unclear words. "
                "`lang` = the language code of that line (en, zh-TW, ja, …); write every line in the language "
                "it is actually spoken in."
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
            lines.append({"t": round(a0 + t, 2), "speaker": ln.get("speaker", ""), "lang": ln.get("lang", ""),
                          "text": ln["text"].strip()})
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


def reasr_segments(p: Project) -> int:
    """Whole sentences in another language → Apple ASR again in THAT language, so those words get real
    word timings instead of being guessed by the corrector. Returns how many stretches were redone."""
    o = p.options
    ref = p.read("reference.json") or {}
    asr0 = p.read("asr_primary.json") or p.read("asr.json")
    if not ref.get("lines") or not asr0:
        return 0
    words = [{"s": w["start"], "e": w["end"]} for seg in asr0["segments"] for w in seg["words"]]
    spans = other_language_spans(ref["lines"], o["locale"], words)
    sup = supported_locales()
    spans = [sp for sp in spans if to_locale(sp["lang"], sup)]
    if not spans:
        return 0
    redone = []
    for sp in spans:
        loc = to_locale(sp["lang"], sup)
        part = p.dir / f"reasr_{sp['start']:.0f}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{sp['start']:.3f}", "-t", f"{sp['end'] - sp['start']:.3f}",
                        "-i", str(p.dir / "audio.wav"), str(part)], check=True)
        out = subprocess.run([str(ASR_BIN), str(part), "--locale", loc], capture_output=True, text=True)
        part.unlink(missing_ok=True)
        segs = json.loads(out.stdout)["segments"] if out.returncode == 0 and out.stdout.strip() else []
        for seg in segs:  # back onto the full video's clock
            seg["start"] += sp["start"]
            seg["end"] += sp["start"]
            for w in seg["words"]:
                w["start"] = (w["start"] or 0) + sp["start"]
                w["end"] = (w["end"] or w["start"] - sp["start"]) + sp["start"]
        redone.append(segs)
        p.log(f"  {fmt_mmss(sp['start'])}–{fmt_mmss(sp['end'])} 是 {sp['lang']}，改用 {loc} 重新辨識："
              f"{' '.join(s['text'] for s in segs)[:60]}")
    if not p.read("asr_primary.json"):
        p.write("asr_primary.json", asr0)
    p.write("asr.json", merge_reasr(asr0, spans, redone))
    return len(spans)


def keep_asr_spacing(asr_text: str, corrected: str) -> str:
    """Korean word spacing (띄어쓰기) is easy for the corrector to 'fix' wrongly (태웅님 → 태웅 님). Wherever the
    corrected text has the same letters as the ASR, use the ASR's spacing; keep the corrector's spacing only
    where it actually changed words."""
    def split(t):
        chars, space_before = [], []
        pending = False
        for ch in t:
            if ch.isspace():
                pending = True
                continue
            chars.append(ch)
            space_before.append(pending and bool(chars[:-1]))
            pending = False
        return chars, space_before
    a, sa = split(asr_text)
    c, sc = split(corrected)
    from difflib import SequenceMatcher
    out_space = list(sc)
    for blk in SequenceMatcher(None, a, c, autojunk=False).get_matching_blocks():
        for k in range(1, blk.size):  # inside a matching run (its first char keeps the corrector's choice)
            out_space[blk.b + k] = sa[blk.a + k]
    return "".join((" " if sp else "") + ch for ch, sp in zip(c, out_space))


def stage_correct(p: Project):
    o = p.options
    if o.get("reasr_segments", True) and reasr_segments(p):
        (p.dir / "corrected.json").unlink(missing_ok=True)  # the segments changed → correct afresh
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
        "model that watched the video" + (", plus the AUDIO itself (its start time in the video is given) — "
        "LISTEN to decide every disputed word, and never change a word the audio clearly supports"
        if o.get("correct_with_audio", True) else "") + ". For every ASR segment return the corrected text.\n"
        "Rules:\n"
        "- Fix misheard words, homophones, wrong word boundaries, names, brands and technical terms. When the "
        "ASR and the REFERENCE disagree, prefer whichever is more plausible in context; the REFERENCE is "
        "usually better on names and rare words, the ASR is usually better on exact wording.\n"
        "- If the ASR clearly DROPPED words that the REFERENCE has at that moment, add them back.\n"
        "- Only change words that are actually WRONG. Keep the ASR's word spacing (e.g. Korean 띄어쓰기) and "
        "its formatting of numbers, dates and units (keep digits as digits) wherever the words are right.\n"
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
        + (f"\n{languages_note(p)}. When speech mixes languages, keep every word in the language it was SPOKEN "
           "in — English stays English, Chinese stays Chinese; never turn a spoken foreign word into "
           "same-sounding characters or the reverse. The ASR listened in ONE language, so foreign words often "
           "come out as garbled nonsense: repair them to the intended word (often a product name or technical "
           "term) when the audio and REFERENCE make it clear, but never invent words. Segments marked "
           "lang=… were recognised in that language." if languages_note(p) else "")
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
        asr_lines = [f"[s{i} {fmt_mmss(segs[i]['start'])}–{fmt_mmss(segs[i]['end'])}"
                     f"{' lang=' + segs[i]['lang'] if segs[i].get('lang') else ''}] {segs[i]['text']}" for i in idxs]
        prompt = ("REFERENCE:\n" + ("\n".join(ref_lines) or "(none)") + "\n\nASR SEGMENTS:\n"
                  + "\n".join(asr_lines))
        parts, upl = [{"text": prompt}], None
        if o.get("correct_with_audio", True):
            a0 = max(0.0, a)
            clip = p.dir / "reference" / f"audio_{k:03d}.m4a"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a0:.3f}", "-t", f"{b - a0:.3f}", "-i",
                            str(p.ai_media), "-vn", "-c:a", "aac", "-b:a", "48k", "-ac", "1", str(clip)], check=True)
            upl = gemini.upload_file(clip, "audio/mp4", log=p.log)
            clip.unlink(missing_ok=True)
            parts = [{"file_data": {"mime_type": "audio/mp4", "file_uri": upl["uri"]}},
                     {"text": f"The audio starts at {fmt_mmss(a0)} of the video.\n\n" + prompt}]
        got = {}
        for attempt in range(3):
            data, served = gemini.generate_json(o["text_model"], parts, system=system,
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
        if upl:
            gemini.delete_file(upl["name"])
        for i in idxs:
            result[i] = got.get(i, segs[i]["text"])
            if script_of(segs[i]["text"]) == "hangul":
                result[i] = keep_asr_spacing(segs[i]["text"], result[i])
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


def unsqueeze(dst: list[dict], lo: float, hi: float, fast: float = 0.08):
    """Words the correction added where the ASR heard nothing (often the quieter voice under overlapping
    speech) get crammed into tiny gaps. A run of ≥3 words averaging under `fast` s/word is impossible speech:
    give it the silence around it plus the tail of the previous word — never moving the START of a word the
    ASR timed — and mark it estimated so the editor can flag it."""
    def too_fast(u):  # Chinese / Japanese tokens are single characters: ~0.06–0.12 s each is normal speech
        limit = 0.03 if len(norm(u["t"])) == 1 and is_cjk_char(u["t"][0]) else fast
        return u["e"] - u["s"] < limit

    i, n = 0, len(dst)
    while i < n:
        if not too_fast(dst[i]):
            i += 1
            continue
        j = i
        while j + 1 < n and too_fast(dst[j + 1]):
            j += 1
        if j - i + 1 < 3:  # one or two short words (a, I, the) are normal speech
            i = j + 1
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


_CJK_CH = "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"  # Han only: the spacing rule is a Chinese convention
_LATIN_NUM = r"[A-Za-z0-9]"


def cjk_spacing(text: str) -> str:
    """Half-width space between CJK characters and Latin letters / digits (both directions):
    「我54歲，在AI公司」→「我 54 歲，在 AI 公司」. Full-width punctuation is left alone."""
    if not text:
        return text
    text = re.sub(rf"({_CJK_CH})({_LATIN_NUM})", r"\1 \2", text)
    return re.sub(rf"({_LATIN_NUM})({_CJK_CH})", r"\1 \2", text)


def apply_cjk_spacing(cues: list[dict], o: dict) -> int:
    """Applies the Chinese CJK–Latin/digit spacing rule — to translations only when the target language is
    Chinese, to the original only when the spoken language is Chinese (Japanese keeps its own convention).
    Returns how many cues changed."""
    if not o.get("cjk_spacing", True):
        return 0
    keys = (("tr",) if is_chinese(o.get("target_lang", "")) else ()) + \
        (("text",) if str(o.get("locale", "")).lower().startswith(("zh", "yue")) else ())
    if not keys:
        return 0
    n = 0
    for c in cues:
        for k in keys:
            v = c.get(k)
            if v and cjk_spacing(v) != v:
                c[k] = cjk_spacing(v)
                n += 1
    return n


HL_ON, HL_OFF = "\x01", "\x02"  # karaoke markup understood by bin/text-render and the web preview


def _needs_space(prev: str, tok: str) -> bool:
    return not (prev[-1] in NOSPACE_PUNCT or tok[0] in NOSPACE_PUNCT or (is_cjk_char(prev[-1]) and is_cjk_char(tok[0])))


def dialog_split(c: dict) -> int:
    """How many of a two-speaker cue's words belong to its first line (older cues don't store it)."""
    if c.get("split"):
        return c["split"]
    first = c["text"].split("\n")[0].removeprefix("- ")
    toks = [w["t"] for w in c.get("words") or []]
    for k in range(1, len(toks)):
        if join_tokens(toks[:k]) == first:
            return k
    return len(toks)


def marked_source(c: dict, t: float) -> str:
    """The original-language line(s) of a cue with every word already spoken at time t wrapped in
    HL_ON…HL_OFF. Per-word (not a prefix), so two people speaking at once each get their own highlight."""
    ws = c.get("words") or []
    if not ws:
        return c["text"]
    lines = [ws[:dialog_split(c)], ws[dialog_split(c):]] if c.get("dialog") else [ws]
    out = []
    for line in lines:
        plain, marked = "", ""
        for w in line:
            sep = " " if plain and _needs_space(plain, w["t"]) else ""
            plain += sep + w["t"]
            marked += sep + (HL_ON + w["t"] + HL_OFF if w["s"] <= t + 1e-6 else w["t"])
        out.append(("- " if c.get("dialog") else "") + marked.replace(HL_OFF + " " + HL_ON, " ").replace(HL_OFF + HL_ON, ""))
    return "\n".join(out)


SENT_END = tuple(".?!。？！…」")
CLAUSE_END = tuple(",;:，；：、—")
CONJ = {"and", "but", "so", "because", "which", "that", "or", "when", "if", "where", "who", "then", "like"}


LINE_LIMIT = {"cjk": 16, "hangul": 20, "latin": 42}  # common subtitle line lengths (characters per line)


def target_line_limit(o: dict) -> int:
    """Line length for TRANSLATIONS, by target language."""
    t = (o.get("target_lang") or "").lower()
    if is_chinese(t) or any(w in t for w in ("日本", "japan", "ja")):
        return LINE_LIMIT["cjk"]
    if any(w in t for w in ("韓", "한국", "korean", "ko")):
        return LINE_LIMIT["hangul"]
    return LINE_LIMIT["latin"]


def cue_limits(words: list[dict], o: dict) -> tuple[bool, int]:
    script = script_of(join_tokens([w["t"] for w in words[:400]]))
    return script == "cjk", o["max_chars"] or LINE_LIMIT.get(script, 42)


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


def finalize_cues(groups: list[list[dict]]) -> list[dict]:
    # 1. drop listener backchannels Gemini marked as carrying no information
    groups = [g for g in groups if not g[0].get("line", {}).get("omit")]
    # 2. overlapping speech from another speaker → one two-speaker cue ("- A" / "- B"), shown together
    items: list[dict] = []
    for g in groups:
        meta = g[0].get("line") or {}
        prev = items[-1] if items else None
        if prev and meta.get("with_prev") and not prev["parts"] and \
                meta.get("speaker") != (prev["ws"][0].get("line") or {}).get("speaker"):
            prev["parts"] = [prev["ws"], g]
            prev["ws"] = prev["ws"] + g
        else:
            items.append({"ws": g, "parts": None})
    # 3. a cue whose successor starts almost at once can't be shown on its own — fold it into the next one
    merged: list[dict] = []
    for it in reversed(items):
        if merged and merged[0]["ws"][0]["s"] - it["ws"][0]["s"] < 0.3 and not merged[0]["parts"] and not it["parts"]:
            merged[0] = {"ws": it["ws"] + merged[0]["ws"], "parts": None}
        else:
            merged.insert(0, it)
    cues = [it["ws"] for it in merged]
    parts = [it["parts"] for it in merged]

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
        text = ("- " + join_tokens([w["t"] for w in parts[i][0]]) + "\n- " + join_tokens([w["t"] for w in parts[i][1]])
                if parts[i] else join_tokens([w["t"] for w in ws]))
        out.append({"start": round(start, 3), "end": round(end, 3), "text": text, "dialog": bool(parts[i]),
                    **({"split": len(parts[i][0])} if parts[i] else {}),
                    "words": [{"t": w["t"], "s": round(w["s"], 3), "e": round(w["e"], 3)} for w in ws],
                    "spk": bool(ws[0].get("spk")), "est": sum(1 for w in ws if w.get("est")) >= 2})
    return out


def load_energy(p: Project):
    """10 ms loudness (dBFS) of audio.wav, computed once by bin/apple-asr --energy."""
    import array
    f = p.dir / "energy.f32"
    if not f.exists():
        data = subprocess.run([str(ASR_BIN), "--energy", str(p.dir / "audio.wav")], capture_output=True,
                              check=True).stdout
        f.write_bytes(data)
    a = array.array("f")
    a.frombytes(f.read_bytes())
    return a


def snap_to_energy(words: list[dict], db) -> int:
    """Apple's word times are coarse (0.06 s steps) and a pause is usually absorbed into the word before it.
    Between each pair of words, find the pause in the loudness and put the boundary there: the earlier word
    ends where the sound stops, the next one starts where it resumes. Returns how many boundaries moved."""
    if not db:
        return 0
    n, moved = len(db), 0
    for a, b in zip(words, words[1:]):
        lo = int(max(a["s"] + 0.05, a["e"] - 0.8) * 100)
        hi = int(min(b["s"] + 0.35, max(b["e"] - 0.03, b["s"])) * 100)
        if hi - lo < 12 or hi >= n:
            continue
        # local threshold: room tone differs per recording / scene, so judge a pause against the speech
        # around THIS pair of words: quieter than (loudest nearby − 14 dB) and clearly above the quietest
        around = db[max(0, int(a["s"] * 100)):min(n, int(b["e"] * 100) + 1)]
        if not around:
            continue
        loud, quiet_min = max(around), min(around)
        thr = max(loud - 14, quiet_min + 6)
        if loud - quiet_min < 12:  # no real dip anywhere → nothing to snap to
            continue
        best, run_start = None, None
        for k in range(lo, hi + 1):
            quiet = k < n and db[k] < thr
            if quiet and run_start is None:
                run_start = k
            if (not quiet or k == hi) and run_start is not None:
                # only a pause that ENDS inside the range counts: the sound must resume (the next word's onset);
                # a quiet run still going at the range end is a pause inside / after the next word instead
                if not quiet and k - run_start >= 12 and (best is None or k - run_start > best[1] - best[0]):
                    best = (run_start, k)
                run_start = None
        if not best:
            continue
        p0, p1 = best[0] / 100, best[1] / 100
        if p0 <= a["s"] + 0.04 or p1 >= max(b["e"], b["s"] + 0.35):
            continue
        a["e"] = round(p0 + 0.02, 3)
        b["s"] = round(min(p1, b["e"] - 0.02) if b["e"] > p1 else p1, 3)
        b["e"] = max(b["e"], b["s"] + 0.04)
        moved += 1
    return moved


SEG_SCHEMA = {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "object", "properties": {
    "last": {"type": "integer", "description": "index of the last word of this subtitle line"},
    "speaker": {"type": "string"},
    "with_prev": {"type": "boolean"},
    "omit": {"type": "boolean"}}, "required": ["last", "speaker", "with_prev", "omit"]}}},
    "required": ["lines"]}


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
    """Gemini watches and listens to each stretch of the video together with the numbered, timed words, and
    splits them into subtitle lines the way a human subtitler would — by rhythm, pauses and meaning. It also
    says who speaks each line, which lines overlap with the other speaker (shown together as a two-speaker
    subtitle) and which listener backchannels to leave out. Line metadata rides on the line's first word."""
    cjk, max_chars = cue_limits(words, o)
    windows = seg_windows(words, o["chunk_minutes"])
    has_video = media_has_video(p)
    (p.dir / "segment").mkdir(exist_ok=True)
    omit_rule = ("true for a short listener backchannel or acknowledgement (mm-hmm, uh-huh, yeah, right, "
                 "嗯、對) said while the other person is talking and carrying no information; false for anything "
                 "meaningful (e.g. a 'No.' that answers a question, a real reaction, or the only speech at that "
                 "moment)") if o.get("backchannels", "omit") == "omit" else "always false"
    system = (
        "You are a professional subtitler. Watch and listen to the clip, then split ALL the given transcript "
        "words, in order, into subtitle lines exactly as a skilled human subtitler would for this video: follow "
        "the speaker's rhythm, pauses, emphasis and meaning, so every line reads naturally and appears in step "
        f"with the speech. A comfortable line is roughly {max_chars} characters or less, and each line should "
        "stay on screen long enough to be read comfortably — as a guide about a second or more — unless the "
        "speaker clearly pauses. Words are given as [index@seconds]word (seconds from the start of the clip).\n"
        "For each line, in order, return:\n"
        "- `last`: index of the line's last word (lines are consecutive; the last line ends at the last word)\n"
        "- `speaker`: who says it (a name if the video makes it clear, otherwise A / B …)\n"
        "- `with_prev`: true only when this line is said by a DIFFERENT speaker AT THE SAME TIME as the "
        "previous line (overlapping speech), so both belong on screen together as one two-speaker subtitle\n"
        f"- `omit`: {omit_rule}"
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
                    max_tokens=65536, thinking=o.get("seg_thinking") or None)
            finally:
                gemini.delete_file(f["name"])
            lines = sorted((ln for ln in data.get("lines", []) if idxs[0] <= int(ln.get("last", -1)) <= idxs[-1]),
                           key=lambda ln: int(ln["last"]))
            if not lines and len(idxs) > 30:
                raise ValueError("no lines returned")
        except Exception as e:
            p.log(f"  語意斷句區塊 {k + 1} 失敗，這段改用規則斷句：{e}")
            result[k] = heuristic_groups([words[i] for i in idxs], o)
            return
        finally:
            proxy.unlink(missing_ok=True)
        groups, start = [], idxs[0]
        for ln in lines:
            last = int(ln["last"])
            if last < start:
                continue
            g = [words[i] for i in range(start, last + 1)]
            g[0]["line"] = {"speaker": str(ln.get("speaker", "")), "with_prev": bool(ln.get("with_prev")),
                            "omit": bool(ln.get("omit"))}
            groups.append(g)
            start = last + 1
        if start <= idxs[-1]:
            groups.append([words[i] for i in range(start, idxs[-1] + 1)])
        # safety net only for clearly broken output (a "line" far too long to display)
        out = []
        for g in groups:
            broken = len(join_tokens([w["t"] for w in g])) > max_chars * 2.5
            if broken:
                meta = g[0].get("line")
                sub = heuristic_groups(g, o)
                if meta:
                    sub[0][0]["line"] = meta
                out += sub
            else:
                out.append(g)
        result[k] = out
        done[0] += 1
        p.status(progress=done[0] / len(windows))
        omitted = sum(1 for g in out if g[0].get("line", {}).get("omit"))
        overl = sum(1 for g in out if g[0].get("line", {}).get("with_prev"))
        p.log(f"  語意斷句 {k + 1}/{len(windows)}：{len(out)} 行（重疊 {overl}、省略附和 {omitted}）")

    with ThreadPoolExecutor(max_workers=min(3, o["workers"])) as ex:
        list(ex.map(one, range(len(windows))))
    return [g for k in range(len(windows)) for g in result[k]]


def reuse_lines(old_cues: list[dict], words: list[dict]):
    """Rebuild the previous run's lines on freshly timed words when the wording is unchanged. Words that sit
    between old cues are the backchannels that run omitted; two-speaker cues keep their split. Returns None
    if the words no longer match (then the lines are recomputed)."""
    if not old_cues:
        return None
    groups, k = [], 0
    for c in old_cues:
        toks = [w["t"] for w in c.get("words") or []]
        if not toks:
            return None
        start = k
        while k < len(words) and words[k]["t"] != toks[0]:
            k += 1
        if k > start:  # omitted backchannel(s) in between
            g = words[start:k]
            g[0]["line"] = {"omit": True}
            groups.append(g)
        if [w["t"] for w in words[k:k + len(toks)]] != toks:
            return None
        g = words[k:k + len(toks)]
        if c.get("dialog"):
            sp = dialog_split(c)
            a, b = g[:sp], g[sp:]
            a[0]["line"] = {"speaker": "A", "with_prev": False, "omit": False}
            b[0]["line"] = {"speaker": "B", "with_prev": True, "omit": False}
            groups += [a, b]
        else:
            groups.append(g)
        k += len(toks)
    if k < len(words):
        if len(words) - k > 5:
            return None
        groups.append(words[k:])
    return groups


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
    if o.get("energy_snap", True) and (p.dir / "audio.wav").exists():
        moved = snap_to_energy(words, load_energy(p))
        p.log(f"  依聲音音量校正字的邊界：{moved} 處")
    old_doc = p.read("cues.json") or {}
    old_cues = old_doc.get("cues", [])
    groups = reuse_lines(old_cues, words) if o.get("reuse_breaks", True) and not old_doc.get("edited") else None
    if groups is not None:
        p.log("  字詞與上次相同，沿用上次的斷句（含雙人字幕與省略的附和，只更新時間）")
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
        "id": {"type": "integer"},
        "echo": {"type": "string", "description": "the first 3 words (or first 4 characters) of THIS cue's source"},
        "same": {"type": "boolean", "description": "true if this cue is ALREADY in the target language"},
        "tr": {"type": "string"}}, "required": ["id", "echo", "same", "tr"]}}},
    "required": ["items"],
}
TR_END = (".", "?", "!", "。", "？", "！", "…", '."', '?"', '!"', "」")


def can_be_same(text: str, target: str) -> bool:
    """Could this cue already be in the target language? A script check the model's `same` must pass: for an
    English target a line with any Chinese / Japanese / Korean in it still needs translating (code-switching),
    for a Chinese target a line without Chinese characters does."""
    t = (target or "").lower()
    han = bool(re.search("[\u3400-\u9fff]", text))
    kana = bool(re.search("[\u3040-\u30ff]", text))
    hangul = bool(re.search(f"[{HANGUL}]", text))
    if is_chinese(t):
        return han and not kana and not hangul
    if any(w in t for w in ("日本", "japan")) or t.startswith("ja"):
        return kana or han
    if any(w in t for w in ("韓", "한국", "korean")) or t.startswith("ko"):
        return hangul
    return not (han or kana or hangul)  # Latin-script targets (English, Español, …)


def sentence_groups(cues: list[dict], idxs: list[int]) -> list[list[int]]:
    """Consecutive cue indices that together form one sentence (cues are often half sentences)."""
    groups, cur = [], []
    for n, i in enumerate(idxs):
        cur.append(i)
        nxt = idxs[n + 1] if n + 1 < len(idxs) else None
        gap = cues[nxt]["start"] - cues[i]["end"] if nxt is not None else 9
        if cues[i]["text"].rstrip().endswith(TR_END) or cues[i].get("dialog") or gap > 1.5 or len(cur) >= 6 \
                or nxt is None or nxt != i + 1:
            groups.append(cur)
            cur = []
    return groups


def echo_ok(echo: str, text: str) -> bool:
    """Did the model answer for THIS cue? Its echo must match the start of the cue's source."""
    e = norm(echo)
    src = norm(text.replace("- ", " "))
    if not e:
        return False
    k = min(len(e), 10)
    return src.startswith(e[:k]) or e[:k] in src[:k + 6]


def stage_translate(p: Project, only_missing: bool = False):
    o = resolve_target(p)
    p.log(f"  翻成：{o['target_lang']}")
    doc = p.read("cues.json")
    cues = doc["cues"]
    ref = p.read("reference.json") or {}
    glossary = o["glossary"].strip()
    system = (
        f"You translate subtitles into {o['target_lang']}. Style: {style_for(o)}.\n"
        "Cues come grouped by SENTENCE. For each sentence: first understand / translate the whole sentence, then "
        "split your translation across that sentence's cues so each cue's translation says what is spoken IN "
        "THAT CUE. If the target language needs a different word order, restructure the translation so every "
        "part still matches its own cue — never put one cue's meaning into a neighbouring cue, and never swap "
        "content between cues. Keep it concise for on-screen reading; no trailing full stop on a cue unless it "
        "is a question or exclamation. Keep each cue's translation short enough to read on screen — about "
        f"{target_line_limit(o)} characters or less (condense the wording if needed, never drop meaning). "
        "A cue whose text has two lines starting with '- ' is two people speaking "
        "at once: translate it as '－第一人的話　－第二人的話' on ONE line. For every cue also return `echo` = the "
        "first 3 words (or first 4 characters) of that cue's own source text, copied exactly — it is used to "
        "check that each translation is attached to the right cue. If a cue is ALREADY entirely in the target "
        "language, set `same` = true and `tr` = '' (it is shown as a single line). A cue MIXING languages "
        "(code-switching, e.g. Chinese with English words) is NOT the same: translate the whole cue into the "
        "target language, keeping brand / product names as usually written. Return every id."
        + (f"\n{languages_note(p)}." if languages_note(p) else "")
        + (f"\nGlossary / names: {glossary}" if glossary else "")
        + (f"\nSpeakers: {json.dumps(ref.get('speakers', {}), ensure_ascii=False)}" if ref.get("speakers") else "")
        + (f"\nContext: {o['context'].strip()}" if o["context"].strip() else "")
    )
    todo = [i for i, c in enumerate(cues) if not (only_missing and (c.get("tr") or c.get("same")))]
    import threading
    lock = threading.Lock()
    stats = {"retried": 0, "rejected": 0}

    def make_batches(idxs, size):
        out, cur = [], []
        for g in sentence_groups(cues, idxs):
            if cur and len(cur) + len(g) > size:
                out.append(cur)
                cur = []
            cur += g
        if cur:
            out.append(cur)
        return out

    def translate(batch) -> dict[int, str]:
        """Translate one batch; returns {cue index: translation} for answers that pass the echo check."""
        ctx_before = cues[max(0, batch[0] - 4):batch[0]]
        ctx_after = cues[batch[-1] + 1:batch[-1] + 3]
        payload = {
            "context_before (do not translate)": [c["text"] for c in ctx_before],
            "sentences": [[{"id": cues[i]["id"], "text": cues[i]["text"]} for i in g]
                          for g in sentence_groups(cues, batch)],
            "context_after (do not translate)": [c["text"] for c in ctx_after],
        }
        prompt = json.dumps(payload, ensure_ascii=False, indent=1)  # JSON: a line break in a cue can't shift ids
        by_id = {cues[i]["id"]: i for i in batch}
        good: dict[int, str] = {}
        for attempt in range(3):
            data, _ = gemini.generate_json(o["text_model"], [{"text": prompt}], system=system, schema=TR_SCHEMA,
                                           usage=p.usage, log=p.log, max_tokens=32768, thinking="low")
            bad = 0
            for it in data.get("items", []):
                i = by_id.get(it.get("id"))
                same = bool(it.get("same"))
                if i is None or (not it.get("tr", "").strip() and not same):
                    continue
                if same and not can_be_same(cues[i]["text"], o["target_lang"]):
                    bad += 1  # e.g. a code-switched line marked "already English" — translate it again
                    continue
                if echo_ok(it.get("echo", ""), cues[i]["text"]):
                    good.setdefault(i, None if same else it["tr"].strip())
                else:
                    bad += 1
            if bad == 0 and len(good) == len(batch):
                break
            with lock:
                stats["retried"] += 1
            p.log(f"  翻譯對位檢查：{bad} 條對不上、{len(batch) - len(good)} 條缺，重譯這批（第 {attempt + 2} 次）")
        return good

    def one(batch):
        good = translate(batch)
        missing = [i for i in batch if i not in good]
        if missing:  # still unverified → smaller batches give the model less to keep in step
            for small in make_batches(missing, 8):
                good.update(translate(small))
        with lock:
            for i in batch:
                if i in good:
                    cues[i]["tr"] = good[i] or ""
                    cues[i]["same"] = good[i] is None  # already in the target language → shown as one line
                else:
                    stats["rejected"] += 1
            p.write("cues.json", doc)
            done[0] += 1
            p.status(progress=done[0] / len(batches))

    batches = make_batches(todo, 50)
    done = [0]
    with ThreadPoolExecutor(max_workers=o["workers"]) as ex:
        list(ex.map(one, batches))
    fixed = apply_cjk_spacing(cues, o)
    if fixed:
        p.write("cues.json", doc)
        p.log(f"  中英數之間補半形空格：{fixed} 條")
    missing = sum(1 for c in cues if not c.get("tr") and not c.get("same"))
    same = sum(1 for c in cues if c.get("same"))
    p.log(f"翻譯完成，{len(cues) - missing}/{len(cues)} 條（對位重譯 {stats['retried']} 次"
          + (f"，{same} 條本來就是目標語言、不翻" if same else "") + "）"
          + (f"（{missing} 條缺譯，可按「補翻缺少的譯文」）" if missing else ""))


STAGE_FN = {"proxy": stage_proxy, "asr": stage_asr, "reference": stage_reference, "correct": stage_correct, "cues": stage_cues,
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
