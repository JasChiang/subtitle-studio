"""Minimal Gemini REST client (stdlib only): File API upload, generateContent, usage ledger.

Requested model first, then a fallback chain on transient errors.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://generativelanguage.googleapis.com"
FALLBACK = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest"]
TRANSIENT = {408, 429, 500, 502, 503, 504}

# USD per 1M tokens (input, output) — rough, for the cost readout only.
PRICES = {
    "pro": (1.25, 10.0),
    "flash-lite": (0.10, 0.40),
    "flash": (0.30, 2.50),
}


class GeminiError(RuntimeError):
    pass


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


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None, method: str | None = None,
             timeout: float = 600):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def upload_file(path: Path, mime: str, log=print) -> dict:
    """Resumable upload; waits until the file is ACTIVE. Returns the file resource (name, uri, mimeType)."""
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
        raise GeminiError(f"upload start {status}: {body[:300].decode(errors='replace')}")
    upload_url = next((v for h, v in headers.items() if h.lower() == "x-goog-upload-url"), None)
    if not upload_url:
        raise GeminiError("upload: no upload URL")
    status, _, body = _request(
        upload_url,
        data=path.read_bytes(),
        headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
                 "Content-Length": str(size)},
        method="POST",
    )
    if status >= 300:
        raise GeminiError(f"upload {status}: {body[:300]!r}")
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


def delete_file(name: str):
    try:
        _request(f"{BASE}/v1beta/{name}?key={api_key()}", method="DELETE", timeout=30)
    except Exception:
        pass


def generate(model: str, parts: list[dict], *, system: str | None = None, schema: dict | None = None,
             max_tokens: int = 32768, low_res: bool = False, usage: Usage | None = None,
             log=print, temperature: float | None = None, thinking: str | None = None) -> tuple[str, str]:
    """Returns (text, model_that_served). Retries transient errors across the fallback chain."""
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
            status, _, raw = _request(f"{BASE}/v1beta/models/{m}:generateContent?key={k}", data=payload,
                                      headers={"Content-Type": "application/json"}, method="POST")
            if status < 300:
                obj = json.loads(raw)
                if usage and obj.get("usageMetadata"):
                    usage.record(m, obj["usageMetadata"])
                cands = obj.get("candidates") or []
                text = "".join(p.get("text", "") for c in cands[:1]
                               for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
                if text.strip():
                    if m != model:
                        log(f"  ↪ {model} 不可用，改由 {m} 回應")
                    return text, m
                last = GeminiError(f"{m}: empty response ({cands[:1] and cands[0].get('finishReason')})")
            elif status == 429 and (b"spending cap" in raw or b"spend cap" in raw):
                raise GeminiError(f"{m}: Gemini 專案已達月度花費上限（到 https://ai.studio/spend 調整）")
            elif status in TRANSIENT:
                last = GeminiError(f"{m}: HTTP {status}")
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
