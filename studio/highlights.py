"""Summary + highlight reels, built on the finished subtitles (cues.json).

summary:   Gemini reads the whole transcript → title, summary, timed chapters, takeaways, quotes, social post.
plan:      Gemini picks clips (by cue id) matching the user's request and target length, grouped in chapters.
render:    stock ffmpeg cuts/concats the clips; subtitles, chapter cards and tags are PNG overlays drawn by
           bin/text-render (system fonts), so no libass/freetype build is required.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import gemini
from .pipeline import (HL_OFF, HL_ON, ROOT, Project, fmt_mmss, han_variant, is_cjk_text, join_tokens,
                       marked_source, style_for, ts)

RENDER_BIN = ROOT / "bin" / "text-render"
CARD = 1.7     # seconds of blurred background + chapter card at the start of each chapter
ZOOM = 1.12    # alternate punch-in inside a chapter to hide jump cuts


def cues_of(p: Project) -> list[dict]:
    doc = p.read("cues.json")
    if not doc or not doc.get("cues"):
        raise RuntimeError("還沒有字幕，先跑完轉錄流程")
    return doc["cues"]


def transcript_lines(cues: list[dict], with_tr: bool = False) -> str:
    return "\n".join(f"{c['id']}\t[{fmt_mmss(c['start'])}]\t{c['text']}" + (f"\t｜{c['tr']}" if with_tr and c.get("tr")
                                                                           else "") for c in cues)


# ───────────────────────────── summary ─────────────────────────────

SUM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "summary": {"type": "string"},
        "chapters": {"type": "array", "items": {"type": "object", "properties": {
            "cue": {"type": "integer"}, "title": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["cue", "title", "summary"]}},
        "takeaways": {"type": "array", "items": {"type": "string"}},
        "quotes": {"type": "array", "items": {"type": "object", "properties": {
            "from": {"type": "integer"}, "to": {"type": "integer"}, "text": {"type": "string"},
            "tr": {"type": "string"}}, "required": ["from", "to", "text", "tr"]}},
        "social_post": {"type": "string"},
    },
    "required": ["title", "logline", "summary", "chapters", "takeaways", "quotes", "social_post"],
}


def summarize(p: Project, req: dict):
    o = p.options
    cues = cues_of(p)
    lang = req.get("lang") or o["target_lang"]
    ref = p.read("reference.json") or {}
    system = (
        f"You summarize a video from its timed transcript. Write everything in {lang} "
        f"({style_for(o)}), except `quotes[].text` which stays in the original language.\n"
        "- title: a compelling title for the video. logline: one sentence.\n"
        "- summary: 2–4 short paragraphs covering what is discussed and the main arguments.\n"
        "- chapters: 5–12 chapters covering the whole video in order; `cue` is the id of the line where the "
        "chapter starts (the first chapter starts at the first line).\n"
        "- takeaways: 4–8 key points. quotes: 3–6 memorable, self-contained quotes as cue id ranges with the "
        "exact original text and a translation.\n"
        "- social_post: a ready-to-post social media text (with a few hashtags)."
        + (f"\nSpeakers: {json.dumps(ref.get('speakers', {}), ensure_ascii=False)}" if ref.get("speakers") else "")
        + (f"\nContext: {o['context'].strip()}" if o["context"].strip() else "")
        + (f"\nExtra instructions from the user: {req['note'].strip()}" if req.get("note", "").strip() else "")
    )
    p.log(f"摘要：送出 {len(cues)} 條字幕")
    data, served = gemini.generate_json(o["text_model"], [{"text": transcript_lines(cues)}], system=system,
                                        schema=SUM_SCHEMA, usage=p.usage, log=p.log, max_tokens=32768)
    by_id = {c["id"]: c for c in cues}
    for ch in data.get("chapters", []):
        ch["t"] = by_id.get(ch.get("cue"), cues[0])["start"]
    data["chapters"].sort(key=lambda c: c["t"])
    if data["chapters"]:
        data["chapters"][0]["t"] = 0.0
    for q in data.get("quotes", []):
        q["t"] = by_id.get(q.get("from"), cues[0])["start"]
    data.update(model=served, lang=lang, created=time.time())
    p.write("summary.json", data)
    p.log(f"摘要完成：{len(data['chapters'])} 章、{len(data['quotes'])} 句金句")


def summary_markdown(s: dict) -> str:
    out = [f"# {s['title']}", "", f"> {s['logline']}", "", "## 摘要", "", s["summary"], "", "## 章節", ""]
    out += [f"- {fmt_mmss(c['t'])} {c['title']}：{c['summary']}" for c in s["chapters"]]
    out += ["", "## 重點", ""] + [f"- {t}" for t in s["takeaways"]]
    out += ["", "## 金句", ""] + [f"- [{fmt_mmss(q['t'])}] “{q['text']}”  \n  {q['tr']}" for q in s["quotes"]]
    out += ["", "## 社群貼文", "", s["social_post"], "", "## YouTube 章節", "", "```"]
    out += [f"{fmt_mmss(c['t'])} {c['title']}" for c in s["chapters"]] + ["```", ""]
    return "\n".join(out)


# ───────────────────────────── highlight plan ─────────────────────────────

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "array", "items": {"type": "object", "properties": {
            "from": {"type": "integer"}, "to": {"type": "integer"}}, "required": ["from", "to"]}},
        "chapters": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "title_en": {"type": "string"},
            "clips": {"type": "array", "items": {"type": "object", "properties": {
                "from": {"type": "integer"}, "to": {"type": "integer"}, "why": {"type": "string"}},
                "required": ["from", "to", "why"]}}},
            "required": ["title", "title_en", "clips"]}},
    },
    "required": ["title", "hook", "chapters"],
}


def cut_points(p: Project, cues: list[dict]) -> tuple[list[float], list[float]]:
    """For every line: where a clip should start before it (cin) and end after it (cout). The cut goes at the
    QUIETEST moment of the pause between this line's words and the neighbouring line's (10 ms loudness), never
    more than 0.12 s before / 0.18 s after the words — so a tight join can't leak the next word's onset."""
    from .pipeline import load_energy
    try:
        db = load_energy(p)
    except Exception:
        db = None

    def valley(lo: float, hi: float) -> float:
        if hi - lo < 0.02 or not db:
            return (lo + hi) / 2
        a, b = int(lo * 100), min(int(hi * 100), len(db) - 1)
        if b <= a:
            return (lo + hi) / 2
        mid = (a + b) / 2
        k = min(range(a, b + 1), key=lambda k: (round(db[k], 1), abs(k - mid)))
        return k / 100

    ws = lambda c: c["words"][0]["s"] if c.get("words") else c["start"]
    we = lambda c: c["words"][-1]["e"] if c.get("words") else c["end"]
    cin, cout = [], []
    for i, c in enumerate(cues):
        s, e = ws(c), we(c)
        pe = we(cues[i - 1]) if i > 0 else s - 0.3
        ns = ws(cues[i + 1]) if i + 1 < len(cues) else e + 0.3
        cin.append(round(max(min(valley(min(pe, s), s), s - 0.01), s - 0.12, 0.0), 3))
        cout.append(round(min(max(valley(e, max(ns, e)), e + 0.01), e + 0.18), 3))
    return cin, cout


def clip_bounds(cues: list[dict], a: int, b: int, cuts=None) -> tuple[float, float]:
    """Line-index range → (start, end) at the quiet points around its first / last line."""
    if cuts:
        return cuts[0][a], cuts[1][b]
    s = cues[a]["words"][0]["s"] if cues[a].get("words") else cues[a]["start"]
    e = cues[b]["words"][-1]["e"] if cues[b].get("words") else cues[b]["end"]
    return round(s - 0.05, 3), round(e + 0.1, 3)


def line_range(cues: list[dict], c: dict):
    idx = [i for i, x in enumerate(cues) if x["end"] > c["start"] and x["start"] < c["end"]
           and spoken_in(x, c["start"], c["end"])]
    return (idx[0], idx[-1]) if idx else None


def resnap_plan(plan: dict, cues: list[dict], cuts, tol: float = 0.3) -> int:
    """Move clip edges that sit near a line boundary onto that boundary's quiet cut point. Edges a person
    deliberately placed mid-line (further than `tol` away) are left alone. Returns how many edges moved."""
    moved = 0
    for c in plan.get("hook", []) + [x for ch in plan["chapters"] for x in ch["clips"]]:
        r = line_range(cues, c)
        if not r:
            continue
        ns, ne = cuts[0][r[0]], cuts[1][r[1]]
        if abs(ns - c["start"]) <= tol and abs(ns - c["start"]) > 0.005:
            c["start"], moved = ns, moved + 1
        if abs(ne - c["end"]) <= tol and abs(ne - c["end"]) > 0.005:
            c["end"], moved = ne, moved + 1
    return moved


def plan_duration(plan: dict) -> float:
    return sum(c["end"] - c["start"] for c in plan.get("hook", [])) + \
        sum(c["end"] - c["start"] for ch in plan["chapters"] for c in ch["clips"])


SENT_FINAL = (".", "?", "!", "。", "？", "！", "…", '."', '?"', '!"', "」")


def sentence_units(p: Project, cues: list[dict]) -> list[dict]:
    """Subtitle lines are often half sentences. Merge them into whole sentences (ending in . ? ! 。 or a long
    pause) so the planner picks complete thoughts, and tag each with its speaker from the reference."""
    ref = (p.read("reference.json") or {}).get("lines", [])
    units, cur = [], []
    for i, c in enumerate(cues):
        cur.append(i)
        txt = c["text"].rstrip()
        gap = cues[i + 1]["start"] - c["end"] if i + 1 < len(cues) else 9
        if txt.endswith(SENT_FINAL) or gap > 1.5 or len(cur) >= 6 or c.get("dialog"):
            units.append(cur)
            cur = []
    if cur:
        units.append(cur)
    out = []
    for k, ids in enumerate(units):
        a, b = ids[0], ids[-1]
        t0 = cues[a]["start"]
        spk = next((r["speaker"] for r in reversed(ref) if r["t"] <= t0 + 0.8), "")
        sep = "" if is_cjk_text(cues[a]["text"]) else " "
        out.append({"k": k + 1, "a": a, "b": b, "start": t0, "end": cues[b]["end"], "speaker": spk,
                    "text": sep.join(cues[i]["text"] for i in ids)})
    return out


def lowres_concat(p: Project, pieces: list[tuple[float, float]], out: Path, has_video: bool) -> str:
    """Concatenate source ranges into one small 360p reel for Gemini to watch. Returns the MIME type."""
    args, fc, cat = [], "", ""
    for i, (a, b) in enumerate(pieces):
        args += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(p.media)]
        if has_video:
            fc += f"[{i}:v]setpts=PTS-STARTPTS,scale=-2:360,fps=10,format=yuv420p,setsar=1[v{i}];"
        fc += (f"[{i}:a]asetpts=PTS-STARTPTS,aresample=44100,afade=t=in:d=0.03,"
               f"afade=t=out:st={max(0, b - a - 0.06):.3f}:d=0.06[a{i}];")
        cat += (f"[v{i}]" if has_video else "") + f"[a{i}]"
    fc += f"{cat}concat=n={len(pieces)}:v={1 if has_video else 0}:a=1" + ("[v][a]" if has_video else "[a]")
    vmap = ["-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30"] if has_video else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, "-filter_complex", fc, *vmap, "-map", "[a]",
                    "-c:a", "aac", "-b:a", "64k", str(out)], check=True)
    return "video/mp4" if has_video else "audio/mp4"


def review_rough_cut(p: Project, data: dict, by_k: dict, script: str, rules: str, cues: list[dict], cuts, dur,
                     handle: float = 18.0) -> dict:
    """Paper edit → rough cut → watch. Gemini watches (1) the rough cut of the draft and (2) a CONTEXT reel with
    every clip plus `handle` seconds of source before/after it (overlaps merged), so extending or swapping a
    sentence is judged on what it actually sounds/looks like. Answers in sentence ids only."""
    from .pipeline import media_has_video
    units = sorted(by_k.values(), key=lambda u: u["k"])
    duration = p.meta.get("duration") or cues[-1]["end"]
    parts = [("HOOK", r) for r in data.get("hook") or []] + \
        [(f"CHAPTER {ch.get('title', '')}", r) for ch in data.get("chapters", []) for r in ch.get("clips", [])]
    pieces, cmap, off = [], [], 0.0
    for name, r in parts:
        ua, ub = by_k.get(r.get("from")), by_k.get(r.get("to"))
        if not ua or not ub or ua["k"] > ub["k"]:
            continue
        s, e = cuts[0][ua["a"]], cuts[1][ub["b"]]
        pieces.append((s, e))
        text = " ".join(by_k[k]["text"] for k in range(ua["k"], ub["k"] + 1) if k in by_k)
        cmap.append(f"{fmt_mmss(off)}–{fmt_mmss(off + e - s)}  {name}  sentences {ua['k']}–{ub['k']}: {text}")
        off += e - s
    if not pieces:
        return data
    # context reel: each clip ± handle, merged where they overlap, in source order
    spans = sorted((max(0.0, s - handle), min(duration, e + handle)) for s, e in pieces)
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 1.0:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    xmap, xoff = [], 0.0
    for a, b in merged:
        inside = [u for u in units if u["start"] >= a - 0.5 and u["end"] <= b + 0.5]
        ids = f"sentences {inside[0]['k']}–{inside[-1]['k']}" if inside else "no full sentences"
        used = [f"{fmt_mmss(xoff + max(s, a) - a)}–{fmt_mmss(xoff + min(e, b) - a)}" for s, e in pieces if s < b and e > a]
        xmap.append(f"{fmt_mmss(xoff)}–{fmt_mmss(xoff + b - a)} = source {fmt_mmss(a)}–{fmt_mmss(b)}, {ids}; "
                    f"used in the draft at context time {', '.join(used)}")
        xoff += b - a
    work = p.dir / "highlights" / "_roughcut"
    work.mkdir(parents=True, exist_ok=True)
    rough, ctx = work / "rough.mp4", work / "context.mp4"
    has_video = media_has_video(p)
    mime = lowres_concat(p, pieces, rough, has_video)
    lowres_concat(p, merged, ctx, has_video)
    p.log(f"  看粗剪複查：上傳粗剪 {off:.0f} 秒＋前後文 {xoff:.0f} 秒"
          f"（{(rough.stat().st_size + ctx.stat().st_size) / 1e6:.1f} MB）")
    f1 = gemini.upload_file(rough, mime, log=p.log)
    f2 = gemini.upload_file(ctx, mime, log=p.log)
    try:
        system = ("You are the senior editor reviewing the ROUGH CUT of a highlight reel before it is approved. You "
                  "WATCH it — not just read it.\n" + rules +
                  "\nVIDEO 1 is the rough cut. VIDEO 2 is CONTEXT: every clip with ~" + f"{handle:.0f}" + " seconds of "
                  "the source before and after it, so you can see and hear what you could extend into or swap in.\n"
                  "Watch the rough cut as a viewer would and check every clip: does it start where a thought starts "
                  "and does every explanation actually finish; does anything end mid-thought or on the other "
                  "speaker starting a question; how is the delivery (energy, emotion, a laugh, an awkward pause, a "
                  "weak or mumbled moment); do the jump cuts feel abrupt (picture jumps, voice tone jumps); does the "
                  "reel flow and build? Use the context video to judge extensions and alternatives by how they are "
                  "actually delivered, and the FULL TRANSCRIPT for anything further away. Fix problems by "
                  "extending / trimming clips by whole sentences, replacing a weak take with a better-delivered "
                  "sentence, reordering, or dropping a clip. Keep what already works. In each clip's `why`, say "
                  "briefly what you kept or changed and why. Return the full corrected plan in the same format — "
                  "sentence ids only, never timestamps.")
        prompt = ("VIDEO 1 — ROUGH CUT MAP (time in the rough cut → content):\n" + "\n".join(cmap) +
                  f"\nTotal {off:.0f}s.\n\nVIDEO 2 — CONTEXT MAP (time in the context video → source):\n" +
                  "\n".join(xmap) + "\n\nFULL TRANSCRIPT (sentence id, start, length, speaker: text):\n" + script)
        fixed, _ = gemini.generate_json(
            p.options["text_model"],
            [{"text": "VIDEO 1 (rough cut):"}, {"file_data": {"mime_type": mime, "file_uri": f1["uri"]}},
             {"text": "VIDEO 2 (context with handles):"}, {"file_data": {"mime_type": mime, "file_uri": f2["uri"]}},
             {"text": prompt}],
            system=system, schema=PLAN_SCHEMA, low_res=True, usage=p.usage, log=p.log, max_tokens=32768)
    finally:
        gemini.delete_file(f1["name"])
        gemini.delete_file(f2["name"])
        rough.unlink(missing_ok=True)
        ctx.unlink(missing_ok=True)
    if fixed.get("chapters"):
        p.log(f"  看粗剪複查：{dur(data):.0f} 秒 → {dur(fixed):.0f} 秒")
        return fixed
    return data


def plan_highlight(p: Project, req: dict) -> str:
    o = p.options
    cues = cues_of(p)
    secs = int(req.get("seconds") or 90)
    focus = (req.get("focus") or "").strip()
    use_chapters = req.get("chapters", True)
    lang = o["target_lang"]
    units = sentence_units(p, cues)
    script = "\n".join(f"{u['k']}\t[{fmt_mmss(u['start'])}, {u['end'] - u['start']:.0f}s]\t{u['speaker']}: {u['text']}"
                       for u in units)
    rules = (
        "- Clips are ranges of consecutive SENTENCE ids [from, to] (each id is one complete sentence; the "
        "duration in seconds is shown).\n"
        f"- Target total length: about {secs} seconds (stay within ±15%).\n"
        f"- Content: {focus if focus else 'the most interesting, insightful and quotable moments of the video'}."
        " Only choose material that really matches this request.\n"
        "- EVERY clip must be a complete thought a viewer understands without the rest of the video: include "
        "the sentences that finish an explanation or argument; never end on a setup that is answered later, on "
        "a dangling 'because…', or on the OTHER speaker starting a question. A question from the interviewer "
        "is only kept together with its answer. Prefer fewer, complete clips over many fragments; cutting "
        "filler and digressions out of the middle (jump cuts) is fine as long as the meaning still closes.\n"
        + ("- Group clips into 2–6 chapters, each with a short punchy title in "
           f"{lang} ({style_for(o)}; max ~14 characters) and a short English title. Clips inside a "
           "chapter usually stay in chronological order.\n" if use_chapters else
           "- Return exactly ONE chapter with an empty title containing all clips.\n")
        + ("- `hook`: one short, intriguing complete sentence (under ~8 seconds) played before everything else; "
           "avoid repeating it at the start of a chapter right after.\n" if req.get("hook", True)
           else "- `hook`: empty.\n")
        + "- `title`: a title for the reel in " + lang + "."
        + (f"\nContext: {o['context'].strip()}" if o["context"].strip() else "")
    )
    system = ("You are a senior video editor cutting a short highlight reel from a long interview, working "
              "from its transcript split into numbered sentences with speakers.\n" + rules)
    p.log(f"精華企劃：{secs} 秒｜{focus or '（由 AI 挑選最精彩的段落）'}｜{len(units)} 個句子")
    data, served = gemini.generate_json(o["text_model"], [{"text": script}], system=system, schema=PLAN_SCHEMA,
                                        usage=p.usage, log=p.log, max_tokens=32768)
    by_k = {u["k"]: u for u in units}

    def dur(d):
        tot = 0.0
        for r in (d.get("hook") or []) + [x for ch in d.get("chapters", []) for x in ch.get("clips", [])]:
            ua, ub = by_k.get(r.get("from")), by_k.get(r.get("to"))
            if ua and ub:
                tot += max(0.0, ub["end"] - ua["start"])
        return tot

    cuts = cut_points(p, cues)
    draft = data
    if req.get("review", True):  # second pass: the editor WATCHES a rough cut of the draft and fixes it
        try:
            data = review_rough_cut(p, data, by_k, script, rules, cues, cuts, dur)
        except Exception as e:
            p.log(f"  看粗剪複查失敗，沿用初稿：{e}")


    def clips(raw):
        out = []
        for r in raw:
            ua, ub = by_k.get(r.get("from")), by_k.get(r.get("to"))
            if not ua or not ub:
                continue
            if ua["k"] > ub["k"]:
                ua, ub = ub, ua
            a, b = ua["a"], ub["b"]
            s, e = clip_bounds(cues, a, b, cuts)
            out.append({"start": s, "end": e, "from": cues[a]["id"], "to": cues[b]["id"], "why": r.get("why", "")})
        return out

    plan = {"title": data.get("title", ""), "request": {**req, "seconds": secs}, "model": served,
            "created": time.time(), "hook": clips(data.get("hook", [])) if req.get("hook", True) else [],
            "chapters": [{"title": ch.get("title", ""), "title_en": ch.get("title_en", ""), "clips": clips(ch["clips"])}
                         for ch in data.get("chapters", [])],
            "options": {"cards": bool(use_chapters), "zoom": bool(req.get("zoom", True))},
            "draft": draft if draft is not data else None}  # paper-edit draft kept to compare with the review
    plan["chapters"] = [ch for ch in plan["chapters"] if ch["clips"]]
    if not plan["chapters"]:
        raise RuntimeError("Gemini 沒有挑出任何片段，換個描述再試一次")
    hid = time.strftime("%Y%m%d-%H%M%S")
    plan["id"] = hid
    plan["duration"] = round(plan_duration(plan), 2)
    p.write(f"highlights/{hid}.json", plan)
    p.log(f"精華企劃完成：{len(plan['chapters'])} 章、{sum(len(c['clips']) for c in plan['chapters'])} 段、"
          f"{plan['duration']:.0f} 秒")
    return hid


# ───────────────────────────── render ─────────────────────────────

def timeline(plan: dict):
    """[(start, end, chapter_index (-1 = hook), first_of_chapter, zoom)] in playback order."""
    pieces = [(c["start"], c["end"], -1, False, 1.0) for c in plan.get("hook", [])]
    zoom_on = plan.get("options", {}).get("zoom", True)
    for ci, ch in enumerate(plan["chapters"]):
        for j, c in enumerate(ch["clips"]):
            pieces.append((c["start"], c["end"], ci, j == 0, ZOOM if zoom_on and j % 2 else 1.0))
    return pieces


def spoken_in(c: dict, a: float, b: float) -> bool:
    """A line belongs in a clip only if some of its words are actually SPOKEN inside it — its lead-in / hold
    (display padding) overlapping the clip edge must not flash it on screen."""
    ws = c.get("words") or []
    if not ws:
        return c["end"] > a + 0.3 and c["start"] < b - 0.3
    inside = sum(1 for w in ws if a <= (w["s"] + w["e"]) / 2 <= b)
    return inside * 2 >= len(ws)  # at least half of the line — one bled-in word doesn't pull the line in


def reel_subs(pieces, cues: list[dict], tr_first: bool = True, karaoke: bool = False
              ) -> list[tuple[float, float, list[str]]]:
    """Subtitle events on the reel timeline. With karaoke, a cue is split at every word start and the spoken
    words already spoken are wrapped in HL_ON…HL_OFF (drawn in the highlight colour), per word."""
    out, off = [], 0.0

    def lines_for(c, src):
        tr = c.get("tr", "").replace("⏎", " ").strip()
        return ([tr, src] if tr_first else [src, tr]) if tr else [src]

    def push(a, b, lines):
        if b - a < 0.02:
            return
        if out and out[-1][2] == lines and abs(out[-1][1] - a) < 0.05:
            out[-1] = (out[-1][0], b, lines)
        else:
            out.append((a, b, lines))

    for pa, pb, *_ in pieces:
        for c in cues:
            if not spoken_in(c, pa, pb):
                continue
            s, e = max(c["start"], pa), min(c["end"], pb)
            if e - s <= 0.12:
                continue
            ws = c.get("words") or []
            if not karaoke or not ws:
                push(off + s - pa, off + e - pa, lines_for(c, c["text"]))
                continue
            marks = sorted({s, *[w["s"] for w in ws if s < w["s"] < e]}) + [e]
            for k in range(len(marks) - 1):
                push(off + marks[k] - pa, off + marks[k + 1] - pa, lines_for(c, marked_source(c, marks[k])))
        off += pb - pa
    for i in range(len(out) - 1):  # close tiny gaps so subtitles don't flicker
        if 0 < out[i + 1][0] - out[i][1] < 0.3:
            out[i] = (out[i][0], out[i + 1][0], out[i][2])
    return out


def overlay_png(p: Project, kind: str, lines: list[str]) -> Path:
    """One overlay image (sub / card / tag) exactly as the export draws it; cached by content."""
    import hashlib
    # older pages sent "spoken\x01rest" (prefix style) — convert to "\x01spoken\x02rest"
    lines = [(HL_ON + l.split(HL_ON, 1)[0] + HL_OFF + l.split(HL_ON, 1)[1]) if HL_ON in l and HL_OFF not in l
             else l for l in lines]
    W, H = project_size(p)
    han = han_variant(p.options)
    scale = sub_scale(p)
    key = hashlib.sha1(json.dumps([kind, lines, W, H, han, scale, RENDER_BIN.stat().st_mtime], ensure_ascii=False)
                       .encode()).hexdigest()[:20]
    out = p.dir / "overlays" / f"{key}.png"
    if not out.exists():
        out.parent.mkdir(exist_ok=True)
        tmp = out.with_suffix(f".{threading.get_ident()}.tmp.png")
        subprocess.run([str(RENDER_BIN)], input=json.dumps({"width": W, "height": H, "han": han, "scale": scale, "items": [
            {"out": str(tmp), "kind": kind, "lines": lines}]}).encode(), check=True, capture_output=True)
        tmp.replace(out)
    return out


def project_size(p: Project) -> tuple[int, int]:
    m = p.meta
    if not m.get("size"):
        p.update(size=list(probe_size(p.media)))
        m = p.meta
    return tuple(m["size"])


def probe_size(path: Path) -> tuple[int, int]:
    s = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                 "stream=width,height", "-of", "csv=p=0:s=x", str(path)]).decode().strip()
    w, h = (int(x) for x in s.split("x")[:2])
    return w - w % 2, h - h % 2


def ffconcat(path: Path, events: list[tuple[float, float, str]], total: float, blank: str):
    """Image sequence with exact durations (blank PNG in gaps) → one overlay stream."""
    lines, t = ["ffconcat version 1.0"], 0.0
    for a, b, img in events:
        if a > t + 0.001:
            lines += [f"file '{blank}'", f"duration {a - t:.3f}"]
        lines += [f"file '{img}'", f"duration {max(0.04, b - a):.3f}"]
        t = b
    if total > t:
        lines += [f"file '{blank}'", f"duration {total - t:.3f}"]
    lines.append(f"file '{blank}'")
    path.write_text("\n".join(lines) + "\n")


def render_highlight(p: Project, hid: str):
    plan = p.read(f"highlights/{hid}.json")
    out = p.dir / "highlights" / f"{hid}.mp4"
    total, subs_mode = render_plan(p, plan, out, p.dir / "highlights" / f"{hid}.srt",
                                   p.dir / "highlights" / f"{hid}_work", "精華")
    plan["rendered"] = {"file": out.name, "at": time.time(), "duration": round(total, 2), "subs": subs_mode}
    p.write(f"highlights/{hid}.json", plan)


def render_full(p: Project, req: dict) -> Path:
    """The whole video with this project's subtitles: a toggleable track (no re-encode) or burned in."""
    mode = req.get("subs", "soft")
    cues = cues_of(p)
    duration = p.meta.get("duration") or float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p.media)]))
    exports = p.dir / "exports"
    exports.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = exports / f"full-{mode}-{stamp}.mp4"
    srt = exports / f"full-{stamp}.srt"
    if mode == "soft":
        tr_first = p.options.get("sub_order", "tr-first") != "src-first"
        srt.write_text(reel_srt(reel_subs([(0.0, duration, 0, False, 1.0)], cues, tr_first, False)))
        p.log(f"輸出完整影片：內嵌字幕軌（影音直接複製、不重新編碼），{len(cues)} 條字幕")
        tmp = out.with_suffix(".part.mp4")
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(p.media), "-i", str(srt),
                               "-map", "0:v:0", "-map", "0:a:0?", "-map", "1:s", "-c", "copy", "-c:s", "mov_text",
                               *(["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k"]
                                 if p.options.get("loudnorm", True) else []),
                               "-metadata:s:s:0", "language=chi", "-dn", "-write_tmcd", "0", "-movflags", "+faststart", str(tmp)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg 失敗：" + proc.stderr[-600:])
        tmp.replace(out)
    else:
        plan = {"hook": [], "chapters": [{"title": "", "clips": [{"start": 0.0, "end": duration}]}],
                "options": {"cards": False, "zoom": False, "subs": "burn"}}
        karaoke = bool(req.get("karaoke"))
        render_plan(p, plan, out, srt, exports / f"work-{stamp}", "完整影片", karaoke=karaoke, cleanup=True)
        return out
    p.log(f"完整影片完成：{out.name}（{out.stat().st_size / 1e6:.0f} MB）")
    return out


def list_exports(p: Project) -> list[dict]:
    d = p.dir / "exports"
    if not d.exists():
        return []
    return [{"file": f.name, "size": f.stat().st_size, "at": f.stat().st_mtime,
             "subs": "內嵌字幕軌" if "-soft-" in f.name else "燒進畫面"}
            for f in sorted(d.glob("full-*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not f.name.endswith(".part.mp4")]


def probe_fps(path: Path) -> str:
    r = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                 "stream=avg_frame_rate", "-of", "csv=p=0", str(path)]).decode().strip()
    return r if r and r != "0/0" else "30000/1001"


def sub_scale(p: Project) -> float:
    try:
        return round(max(50, min(200, float(p.options.get("sub_scale") or 100))) / 100, 2)
    except (TypeError, ValueError):
        return 1.0


def draw_overlays(items: list[dict], W: int, H: int, procs: int = 4, han: str = "TC", scale: float = 1.0):
    """Run bin/text-render on several batches at once — a full hour of word-by-word subtitles is thousands
    of images."""
    from concurrent.futures import ThreadPoolExecutor
    size = max(1, -(-len(items) // procs))
    chunks = [items[k:k + size] for k in range(0, len(items), size)]

    def run(chunk):
        subprocess.run([str(RENDER_BIN)], input=json.dumps({"width": W, "height": H, "han": han, "scale": scale,
                                                            "items": chunk}).encode(), check=True, capture_output=True)

    with ThreadPoolExecutor(max_workers=procs) as ex:
        list(ex.map(run, chunks))


def render_plan(p: Project, plan: dict, out: Path, srt_path: Path, work: Path, label: str,
                karaoke: bool | None = None, cleanup: bool = False) -> tuple[float, str]:
    """Cut / concat the plan's pieces from the source, burn subtitles / chapter cards / tags as PNG overlays
    drawn by bin/text-render, keep the source frame rate. Returns (duration, subtitle mode)."""
    cues = cues_of(p)
    if not RENDER_BIN.exists():
        raise RuntimeError("bin/text-render 不存在，先跑 ./setup.sh")
    pieces = timeline(plan)
    W, H = probe_size(p.media)
    fps = probe_fps(p.media)
    work.mkdir(parents=True, exist_ok=True)
    for f in work.glob("*.png"):
        f.unlink()

    starts, off = [], 0.0
    for a, b, *_ in pieces:
        starts.append(off)
        off += b - a
    total = off
    cards_on = plan.get("options", {}).get("cards", True)

    # overlay images
    items = [{"out": str(work / "blank.png"), "kind": "blank", "lines": []}]
    sub_mode = plan.get("options", {}).get("subs", "burn")   # burn | soft | none
    kar = bool(p.options.get("karaoke")) if karaoke is None else karaoke
    subs = reel_subs(pieces, cues, p.options.get("sub_order", "tr-first") != "src-first", kar and sub_mode == "burn")
    sub_ev, card_ev, tag_ev = [], [], []
    for i, (a, b, lines) in enumerate(subs if sub_mode == "burn" else []):
        f = str(work / f"sub_{i:05d}.png")
        items.append({"out": f, "kind": "sub", "lines": lines})
        sub_ev.append((a, b, f))
    blur_spans = []
    for ci, ch in enumerate(plan["chapters"]):
        if not cards_on or not ch.get("title"):
            continue
        idx = [i for i, pc in enumerate(pieces) if pc[2] == ci]
        t0 = starts[idx[0]]
        t1 = starts[idx[-1]] + pieces[idx[-1]][1] - pieces[idx[-1]][0]
        cd = min(CARD, t1 - t0)
        fc_, ft = str(work / f"card_{ci:02d}.png"), str(work / f"tag_{ci:02d}.png")
        items += [{"out": fc_, "kind": "card", "lines": [ch["title"], ch.get("title_en", "")]},
                  {"out": ft, "kind": "tag", "lines": [ch["title"]]}]
        card_ev.append((t0, t0 + cd, fc_))
        tag_ev.append((t0 + cd, t1, ft))
        blur_spans.append((t0, t0 + cd))
    p.log(f"  繪製 {len(items) - 1} 張字幕／字卡圖")
    draw_overlays(items, W, H, han=han_variant(p.options), scale=sub_scale(p))
    blank = str(work / "blank.png")
    tracks = []
    for name, ev in (("subs", sub_ev), ("cards", card_ev), ("tags", tag_ev)):
        if ev:
            ffconcat(work / f"{name}.ffconcat", ev, total, blank)
            tracks.append(work / f"{name}.ffconcat")

    # filter graph
    args, fc, cat = [], "", ""
    for i, (a, b, ci, first, z) in enumerate(pieces):
        d = b - a
        args += ["-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", str(p.media)]
        v = f"[{i}:v]setpts=PTS-STARTPTS,fps={fps},scale={W}:{H},setsar=1,format=yuv420p"
        if z != 1.0:
            v += f",crop=iw/{z}:ih/{z}:(iw-iw/{z})*0.45:(ih-ih/{z})*0.35,scale={W}:{H}:flags=lanczos,setsar=1"
        fc += v + f"[v{i}];"
        fc += (f"[{i}:a]asetpts=PTS-STARTPTS,aresample=48000,afade=t=in:d=0.03,"
               f"afade=t=out:st={max(0, d - 0.06):.3f}:d=0.06[a{i}];")
        cat += f"[v{i}][a{i}]"
    n = len(pieces)
    fc += f"{cat}concat=n={n}:v=1:a=1[vc][ac0];"
    fc += ("[ac0]loudnorm=I=-14:TP=-1.5:LRA=11[ac];" if p.options.get("loudnorm", True) else "[ac0]anull[ac];")
    cur = "vc"
    if blur_spans:
        en = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in blur_spans)
        fc += f"[vc]boxblur=22:2:enable='{en}',eq=brightness=-0.12:saturation=0.75:enable='{en}'[vb];"
        cur = "vb"
    for k, tr in enumerate(tracks):
        args += ["-f", "concat", "-safe", "0", "-i", str(tr)]
        fc += f"[{n + k}:v]format=rgba[o{k}];[{cur}][o{k}]overlay=eof_action=pass:format=auto[x{k}];"
        cur = f"x{k}"
    fc = fc.rstrip(";")
    srt_path.write_text(reel_srt(subs))
    soft = []
    if sub_mode == "soft":  # a subtitle track players can toggle (no karaoke in soft subs)
        args += ["-i", str(srt_path)]
        soft = ["-map", f"{n + len(tracks)}:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=chi"]
    tmp = out.with_suffix(".part.mp4")
    enc = ["-c:v", "h264_videotoolbox", "-b:v", "8M"] if hw_encoder() else ["-c:v", "libx264", "-crf", "19",
                                                                             "-preset", "medium"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, "-filter_complex", fc, "-map", f"[{cur}]",
           "-map", "[ac]", *soft, *enc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-dn",
           "-map_chapters", "-1", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(tmp)]
    p.log(f"輸出{label}：{n} 段、{len(subs)} 條字幕、{total:.1f} 秒（{'硬體' if hw_encoder() else '軟體'}編碼，{fps} fps）")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and line.strip().split("=")[1].isdigit():
            p.status(progress=min(0.99, int(line.split("=")[1]) / 1e6 / total))
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg 失敗：" + err[-600:])
    tmp.replace(out)
    if cleanup:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    p.log(f"{label}完成：{out.name}（{out.stat().st_size / 1e6:.1f} MB）")
    return total, sub_mode


def reel_srt(subs) -> str:
    plain = []  # karaoke steps collapse back to one cue each in the SRT
    for a, b, lines in subs:
        lines = [l.replace(HL_ON, "").replace(HL_OFF, "") for l in lines]
        if plain and plain[-1][2] == lines and abs(plain[-1][1] - a) < 0.05:
            plain[-1] = (plain[-1][0], b, lines)
        else:
            plain.append((a, b, lines))
    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n" + "\n".join(lines) + "\n" for i, (a, b, lines) in
                     enumerate(plain, 1))


_HW = None


def hw_encoder() -> bool:
    global _HW
    if _HW is None:
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        _HW = "h264_videotoolbox" in enc
    return _HW


def list_plans(p: Project) -> list[dict]:
    out = []
    for f in sorted((p.dir / "highlights").glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        d = json.loads(f.read_text())
        out.append({k: d.get(k) for k in ("id", "title", "duration", "request", "rendered", "created")})
    return out


def save_plan(p: Project, hid: str, plan: dict):
    if not re.fullmatch(r"[\w-]+", hid):
        raise ValueError("bad id")
    old = p.read(f"highlights/{hid}.json") or {}
    plan = {**old, **plan, "id": hid}
    try:
        cues = cues_of(p)
        resnap_plan(plan, cues, cut_points(p, cues))
    except Exception:
        pass
    plan["duration"] = round(plan_duration(plan), 2)
    plan.pop("rendered", None)  # edited → the old render no longer matches
    p.write(f"highlights/{hid}.json", plan)
    return plan
