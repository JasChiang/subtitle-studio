"""Minimal Gemini REST client (stdlib only): File API upload, generateContent (optionally streamed, for live
progress), usage ledger.

Requested model first, then a fallback chain on transient errors — including dropped connections and timeouts.
"""
from __future__ import annotations

import http.client
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://generativelanguage.googleapis.com"
FALLBACK = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest"]
TRANSIENT = {0, 408, 429, 500, 502, 503, 504}  # 0 = the connection failed / dropped / timed out

# USD per 1M tokens (input, output) — rough, for the cost readout only.
PRICES = {
    "pro": (1.25, 10.0),
    "flash-lite": (0.10, 0.40),
    "flash": (0.30, 2.50),
}


class GeminiError(RuntimeError):
    pass


class StreamBroken(GeminiError):
    """A streamed answer stopped part-way (only raised with resume=True): `partial` is the text received so far,
    so the caller can keep the complete items and ask again only for the rest."""

    def __init__(self, msg: str, partial: str):
        super().__init__(msg)
        self.partial = partial


def api_key() -> str:
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if not k:
        raise GeminiError("GEMINI_API_KEY 未設定（放進 .env 或環境變數）")
    return k


def price_for(model: str) -> tuple[float, float]:
    for tag in ("flash-lite", "pro", "flash"):
        if tag in model:
            return PRICES[tag]
    return PRICES["flash"]


class Usage:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows: dict[str, dict] = {}

    def record(self, model: str, meta: dict):
        p = meta.get("promptTokenCount", 0) or 0
        o = (meta.get("candidatesTokenCount", 0) or 0) + (meta.get("thoughtsTokenCount", 0) or 0)
        with self.lock:
            r = self.rows.setdefault(model, {"calls": 0, "in": 0, "out": 0})
            r["calls"] += 1
            r["in"] += p
            r["out"] += o

    def summary(self) -> dict:
        with self.lock:
            total = 0.0
            out = {}
            for m, r in self.rows.items():
                pi, po = price_for(m)
                cost = r["in"] / 1e6 * pi + r["out"] / 1e6 * po
                total += cost
                out[m] = {**r, "usd": round(cost, 4)}
            return {"models": out, "usd": round(total, 4)}


def _ssl_context() -> ssl.SSLContext:
    """HTTPS trust store. Python from the python.org installer ships without CA certificates until its
    'Install Certificates.command' is run, which makes every Gemini call fail with CERTIFICATE_VERIFY_FAILED.
    When Python's own bundle is missing, fall back to certifi (if installed) or macOS's /etc/ssl/cert.pem."""
    paths = ssl.get_default_verify_paths()
    has_bundle = bool(paths.cafile and os.path.exists(paths.cafile)) or \
        bool(paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath))
    if has_bundle:
        return ssl.create_default_context()
    candidates = []
    try:
        import certifi  # optional
        candidates.append(certifi.where())
    except ImportError:
        pass
    candidates += ["/etc/ssl/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem", "/usr/local/etc/openssl@3/cert.pem"]
    for f in candidates:
        if f and os.path.exists(f):
            return ssl.create_default_context(cafile=f)
    return ssl.create_default_context()


_SSL = _ssl_context()


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None, method: str | None = None,
             timeout: float = 600):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:  # offline, reset, timeout, sleep
        return 0, {}, f"network: {e}".encode()


def _stream(url: str, payload: bytes, on_text, timeout: float = 300):
    """streamGenerateContent (server-sent events). Calls on_text(answer_so_far) as text arrives.
    Returns (status, text, usage, finish_reason, error_body); status 0 = the stream broke (text = what arrived)."""
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    parts: list[str] = []
    usage, finish = None, None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                obj = json.loads(line[5:])
                if obj.get("error"):
                    return int(obj["error"].get("code") or 0), "".join(parts), usage, None, line.encode()
                usage = obj.get("usageMetadata") or usage
                for c in (obj.get("candidates") or [])[:1]:
                    finish = c.get("finishReason") or finish
                    new = [q["text"] for q in (c.get("content") or {}).get("parts", [])
                           if q.get("text") and not q.get("thought")]
                    if new:
                        parts.extend(new)
                        if on_text:
                            on_text("".join(parts))
    except urllib.error.HTTPError as e:
        return e.code, "", None, None, e.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        return 0, "".join(parts), usage, None, f"stream broke: {e}".encode()
    if finish is None:  # the connection closed without the final chunk
        return 0, "".join(parts), usage, None, b"stream ended early"
    return 200, "".join(parts), usage, finish, None


def upload_file(path: Path, mime: str, log=print) -> dict:
    """Resumable upload; waits until the file is ACTIVE. Returns the file resource (name, uri, mimeType)."""
    for attempt in range(3):
        try:
            return _upload(path, mime)
        except GeminiError as e:
            if attempt == 2 or not getattr(e, "transient", False):
                raise
            log(f"  … 上傳失敗（{e}），重試")
            time.sleep(3 * (attempt + 1))


def _upload(path: Path, mime: str) -> dict:
    k = api_key()
    size = path.stat().st_size
    status, headers, body = _request(
        f"{BASE}/upload/v1beta/files?key={k}",
        data=json.dumps({"file": {"display_name": path.name}}).encode(),
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    if status >= 300:
        if b"spend" in body and b"cap" in body:
            raise GeminiError("Gemini 專案已達月度花費上限（到 https://ai.studio/spend 調整）")
        raise _err(f"upload start {status}: {body[:300].decode(errors='replace')}", status)
    upload_url = next((v for h, v in headers.items() if h.lower() == "x-goog-upload-url"), None)
    if not upload_url:
        raise _err("upload: no upload URL", 0)
    status, _, body = _request(
        upload_url,
        data=path.read_bytes(),
        headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
                 "Content-Length": str(size)},
        method="POST",
    )
    if status >= 300:
        raise _err(f"upload {status}: {body[:300].decode(errors='replace')}", status)
    f = json.loads(body)["file"]
    for _ in range(300):
        if f.get("state") == "ACTIVE":
            return f
        if f.get("state") == "FAILED":
            raise GeminiError("file processing FAILED")
        time.sleep(2)
        status, _, body = _request(f"{BASE}/v1beta/{f['name']}?key={k}")
        if status < 300:
            f = json.loads(body)
    raise GeminiError("file never became ACTIVE")


def _err(msg: str, status: int) -> GeminiError:
    e = GeminiError(msg)
    e.transient = status in TRANSIENT
    return e


def delete_file(name: str):
    try:
        _request(f"{BASE}/v1beta/{name}?key={api_key()}", method="DELETE", timeout=30)
    except Exception:
        pass


def generate(model: str, parts: list[dict], *, system: str | None = None, schema: dict | None = None,
             max_tokens: int = 32768, low_res: bool = False, usage: Usage | None = None,
             log=print, temperature: float | None = None, thinking: str | None = None,
             on_text=None, resume: bool = False) -> tuple[str, str]:
    """Returns (text, model_that_served). Retries transient errors (incl. dropped connections) across the fallback
    chain. on_text: stream the answer and call on_text(answer_so_far) as it arrives (live progress). resume: when
    a stream breaks after some text arrived, raise StreamBroken(partial) instead of asking for everything again."""
    k = api_key()
    gen: dict = {"maxOutputTokens": max_tokens}
    if temperature is not None:
        gen["temperature"] = temperature
    if thinking:  # "minimal" | "low" | "medium" | "high" — cheap mechanical tasks don't need deep thinking
        gen["thinkingConfig"] = {"thinkingLevel": thinking}
    if low_res:
        gen["mediaResolution"] = "MEDIA_RESOLUTION_LOW"
    if schema:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = schema
    body: dict = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    payload = json.dumps(body).encode()

    chain = [model] + [m for m in FALLBACK if m != model]
    last = None
    for m in chain:
        for attempt in range(3):
            if on_text:
                status, text, meta, finish, raw = _stream(
                    f"{BASE}/v1beta/models/{m}:streamGenerateContent?alt=sse&key={k}", payload, on_text)
                if usage and meta:
                    usage.record(m, meta)
                if status == 0 and resume and text.strip():
                    raise StreamBroken(f"{m}: {raw.decode(errors='replace')}", text)
            else:
                status, _, raw = _request(f"{BASE}/v1beta/models/{m}:generateContent?key={k}", data=payload,
                                          headers={"Content-Type": "application/json"}, method="POST")
                text, finish = "", None
                if status < 300:
                    obj = json.loads(raw)
                    if usage and obj.get("usageMetadata"):
                        usage.record(m, obj["usageMetadata"])
                    cands = obj.get("candidates") or []
                    finish = cands[:1] and cands[0].get("finishReason")
                    text = "".join(p.get("text", "") for c in cands[:1]
                                   for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
            if status and status < 300:
                if text.strip():
                    if m != model:
                        log(f"  ↪ {model} 不可用，改由 {m} 回應")
                    return text, m
                last = GeminiError(f"{m}: empty response ({finish})")
            elif status == 429 and (b"spending cap" in raw or b"spend cap" in raw):
                raise GeminiError(f"{m}: Gemini 專案已達月度花費上限（到 https://ai.studio/spend 調整）")
            elif status in TRANSIENT:
                last = GeminiError(f"{m}: " + (raw[:200].decode(errors="replace") if status == 0 else f"HTTP {status}"))
            else:
                raise GeminiError(f"{m}: HTTP {status}: {raw[:400].decode(errors='replace')}")
            log(f"  … {last}，重試")
            time.sleep(2 * (attempt + 1))
    raise last or GeminiError("generate failed")


def generate_json(model: str, parts: list[dict], **kw):
    text, served = generate(model, parts, **kw)
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").removeprefix("json").strip()
    return json.loads(t), served


def partial_items(text: str, key: str) -> list[dict]:
    """The complete objects of the JSON array `key` in a possibly cut-off answer: '{"items":[{…},{…},{"id":3,"t'
    → the first two. Used to keep what a broken stream already delivered."""
    i = text.find(f'"{key}"')
    i = text.find("[", i) if i >= 0 else -1
    if i < 0:
        return []
    out, depth, start, in_str, esc = [], 0, None, False, False
    for j in range(i + 1, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = j
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    out.append(json.loads(text[start:j + 1]))
                except ValueError:
                    pass
                start = None
        elif ch == "]" and depth == 0:
            break
    return out
