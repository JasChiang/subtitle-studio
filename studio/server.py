"""Local web GUI server (stdlib only).  python3 -m studio.server [--port 8765]"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import highlights, pipeline

ROOT = pipeline.ROOT
PROJECTS = ROOT / "projects"
STATIC = ROOT / "static"
RUNNING: dict[str, threading.Thread] = {}
LOCALES: list[str] = []
IMPORTS: dict[str, dict] = {}  # YouTube / web video downloads: id → {state, progress, path, title, error}


def start_import(url: str) -> str:
    """Download a web video (YouTube etc.) with yt-dlp into uploads/, reporting progress."""
    import shutil
    import subprocess
    if not shutil.which("yt-dlp"):
        raise RuntimeError("需要 yt-dlp：brew install yt-dlp")
    if not re.match(r"https?://", url):
        raise ValueError("請貼上完整網址（https://…）")
    jid = time.strftime("%Y%m%d-%H%M%S")
    job = IMPORTS[jid] = {"state": "running", "progress": 0.0, "path": None, "title": "", "error": None}
    up = ROOT / "uploads"
    up.mkdir(exist_ok=True)

    def work():
        try:
            proc = subprocess.Popen(
                ["yt-dlp", "--newline", "--no-playlist", "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
                 "--merge-output-format", "mp4", "-o", str(up / "%(title).60s [%(id)s].%(ext)s"),
                 "--print", "before_dl:TITLE\t%(title)s", "--print", "after_move:FILE\t%(filepath)s", url],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lines, path = [], None
            for line in proc.stdout:
                line = line.rstrip("\n")
                m = re.search(r"\[download\]\s+([\d.]+)%", line)
                if m:
                    job["progress"] = min(0.99, float(m.group(1)) / 100)
                elif line.startswith("TITLE\t"):
                    job["title"] = line[6:]
                elif line.startswith("FILE\t"):
                    path = line[5:]
                elif line.strip():
                    lines.append(line.strip())
            if proc.wait() != 0:
                raise RuntimeError("下載失敗：" + (next((l for l in reversed(lines) if "ERROR" in l), None)
                                               or (lines[-1] if lines else "yt-dlp error")))
            path = path if path and Path(path).exists() else None
            if not path:
                raise RuntimeError("下載完成但找不到檔案")
            job.update(state="done", progress=1.0, path=path)
        except Exception as e:
            job.update(state="error", error=str(e))

    threading.Thread(target=work, daemon=True).start()
    return jid


def load_env():
    for f in (ROOT / ".env",):
        if f.exists():
            for line in f.read_text().splitlines():
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)", line)
                if m and not os.environ.get(m.group(1)):
                    os.environ[m.group(1)] = m.group(2).strip().strip("'\"")


def slug(name: str) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "-", Path(name).stem).strip("-")[:40] or "video"
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{s}"


def project(pid: str) -> pipeline.Project:
    d = PROJECTS / pid
    if not re.fullmatch(r"[\w一-鿿-]+", pid) or not (d / "project.json").exists():
        raise KeyError(pid)
    return pipeline.Project(d)


def create_project(media: Path, options: dict) -> dict:
    pid = slug(media.name)
    d = PROJECTS / pid
    d.mkdir(parents=True)
    meta = {"id": pid, "name": media.name, "media": str(media), "created": time.time(),
            "options": {**pipeline.DEFAULT_OPTIONS, **options}, "done": [], "status": {"state": "idle"}}
    (d / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def start_run(p: pipeline.Project, stages: list[str]):
    pid = p.dir.name
    if pid in RUNNING and RUNNING[pid].is_alive():
        raise RuntimeError("這個專案正在執行中")

    def work():
        try:
            pipeline.run_stages(p, stages)
        except Exception:
            traceback.print_exc()

    th = threading.Thread(target=work, daemon=True)
    RUNNING[pid] = th
    th.start()


def start_task(p: pipeline.Project, stage: str, fn):
    """Run one non-pipeline job (summary / highlight plan / render) in the background with status + log."""
    pid = p.dir.name
    if pid in RUNNING and RUNNING[pid].is_alive():
        raise RuntimeError("這個專案正在執行中")
    m = p.meta
    p.update(usage_base={"usd": (m.get("usage") or {}).get("usd", 0)})

    def work():
        p.status(state="running", stage=stage, progress=0, error=None)
        try:
            fn()
            p.status(state="done", stage=None, progress=1)
        except Exception as e:
            traceback.print_exc()
            p.log(f"✗ {e}")
            p.status(state="error", error=str(e))

    RUNNING[pid] = threading.Thread(target=work, daemon=True)
    RUNNING[pid].start()


class Handler(BaseHTTPRequestHandler):
    server_version = "SubtitleStudio/1"

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/projects/") or "/media" not in self.path:
            pass  # keep the console quiet; pipeline logs its own progress

    # ── helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def send_file(self, path: Path, download_name: str | None = None):
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = size - int(m.group(2))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if path.is_relative_to(STATIC):  # always revalidate the page code, so an update can't run half-stale
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(end - start + 1))
        if download_name:
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{urllib.parse.quote(download_name)}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_text(self, text: str, name: str, ctype="text/plain; charset=utf-8"):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}")
        self.end_headers()
        self.wfile.write(body)

    # ── routes
    def do_GET(self):
        try:
            self.route_get()
        except KeyError:
            self.send_json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            self.route_post()
        except KeyError:
            self.send_json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 400)

    do_PUT = do_POST

    def route_get(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        parts = [urllib.parse.unquote(x) for x in u.path.strip("/").split("/") if x]
        if not parts:
            return self.send_file(STATIC / "index.html")
        if parts[0] == "static":
            f = (STATIC / "/".join(parts[1:])).resolve()
            if STATIC in f.parents and f.exists():
                return self.send_file(f)
            raise KeyError
        if parts == ["api", "defaults"]:
            return self.send_json({"options": pipeline.DEFAULT_OPTIONS, "stages": pipeline.STAGES,
                                   "has_key": bool(os.environ.get("GEMINI_API_KEY")),
                                   "has_asr": pipeline.ASR_BIN.exists()})
        if parts == ["api", "locales"]:
            global LOCALES
            if not LOCALES and pipeline.ASR_BIN.exists():
                import subprocess
                LOCALES = subprocess.run([str(pipeline.ASR_BIN), "--list-locales"], capture_output=True,
                                         text=True, timeout=60).stdout.split()
            return self.send_json(LOCALES or ["en-US", "zh-TW"])
        if parts[:2] == ["api", "import-url"] and len(parts) == 3:
            return self.send_json(IMPORTS[parts[2]])
        if parts == ["api", "projects"]:
            out = []
            for d in sorted(PROJECTS.glob("*/project.json"), reverse=True):
                m = json.loads(d.read_text())
                out.append({k: m.get(k) for k in ("id", "name", "created", "done", "status", "duration")})
            return self.send_json(out)
        if parts[:2] == ["api", "browse"]:
            base = Path(q.get("dir", [str(Path.home())])[0]).expanduser()
            exts = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".mp3", ".m4a", ".wav", ".aac", ".flac"}
            items = []
            for c in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                if c.name.startswith("."):
                    continue
                if c.is_dir():
                    items.append({"name": c.name, "path": str(c), "dir": True})
                elif c.suffix.lower() in exts:
                    items.append({"name": c.name, "path": str(c), "dir": False, "size": c.stat().st_size})
            return self.send_json({"dir": str(base), "parent": str(base.parent), "items": items})
        if parts[:2] == ["api", "projects"] and len(parts) >= 3:
            p = project(parts[2])
            sub = parts[3] if len(parts) > 3 else None
            if sub is None:
                m = p.meta
                log = (p.dir / "log.txt").read_text().splitlines()[-200:] if (p.dir / "log.txt").exists() else []
                m["running"] = parts[2] in RUNNING and RUNNING[parts[2]].is_alive()
                m["log"] = log
                return self.send_json(m)
            if sub == "cues":
                doc = p.read("cues.json") or {"cues": []}
                if doc.get("cues"):  # attach the quiet cut points the plan editor snaps clips to
                    cin, cout = highlights.cut_points(p, doc["cues"])
                    for c, a, b in zip(doc["cues"], cin, cout):
                        c["cin"], c["cout"] = a, b
                return self.send_json(doc)
            if sub == "reference":
                return self.send_json({**(p.read("reference.json") or {}), "languages": p.read("languages.json")})
            if sub == "media":  # preview / editing: the proxy when there is one; exports always read the original
                proxy = p.dir / "proxy.mp4"
                use = proxy if p.meta.get("proxy") and proxy.exists() and p.options.get("proxy") != "never" else p.media
                return self.send_file(use)
            if sub == "overlay":  # live-preview layer, drawn by the same renderer as the export
                kind = q.get("kind", ["sub"])[0]
                if kind not in ("sub", "card", "tag"):
                    raise KeyError
                f = highlights.overlay_png(p, kind, q.get("l", []))
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(body)
                return
            if sub == "size":
                return self.send_json(highlights.project_size(p))
            if sub == "summary":
                s = p.read("summary.json")
                if q.get("format", [""])[0] == "md" and s:
                    return self.send_text(highlights.summary_markdown(s), f"{Path(p.meta['name']).stem}.摘要.md",
                                          "text/markdown; charset=utf-8")
                return self.send_json(s or {})
            if sub == "exports":
                if len(parts) == 4:
                    return self.send_json(highlights.list_exports(p))
                f = p.dir / "exports" / parts[4]
                if not re.fullmatch(r"full-[\w-]+\.(mp4|srt)", parts[4]) or not f.exists():
                    raise KeyError
                return self.send_file(f, download_name=f"{Path(p.meta['name']).stem}.{parts[4]}")
            if sub == "highlights":
                if len(parts) == 4:
                    return self.send_json(highlights.list_plans(p))
                hid = parts[4]
                if not re.fullmatch(r"[\w-]+", hid):
                    raise KeyError
                if len(parts) == 5:
                    plan = p.read(f"highlights/{hid}.json")
                    if not plan:
                        raise KeyError
                    return self.send_json(plan)
                f = p.dir / "highlights" / f"{hid}.{'srt' if parts[5] == 'srt' else 'mp4'}"
                if not f.exists():
                    raise KeyError
                name = f"{Path(p.meta['name']).stem}.精華.{hid}{f.suffix}"
                if parts[5] == "download" or parts[5] == "srt":
                    return self.send_file(f, download_name=name)
                return self.send_file(f)
            if sub == "export":
                kind = q.get("kind", ["bi"])[0]
                cues = (p.read("cues.json") or {"cues": []})["cues"]
                pipeline.apply_cjk_spacing(cues, p.options)
                stem = Path(p.meta["name"]).stem
                ext = {"vtt": "vtt", "json": "json"}.get(kind, "srt")
                suffix = {"src": "", "tr": ".zh", "bi": ".bilingual", "bi-tr": ".bilingual", "vtt": "",
                          "json": ".words"}[kind]
                return self.send_text(pipeline.export(cues, kind), f"{stem}{suffix}.{ext}")
        raise KeyError

    def route_post(self):
        parts = [urllib.parse.unquote(x) for x in urllib.parse.urlparse(self.path).path.strip("/").split("/") if x]
        if parts == ["api", "projects"]:
            b = self.body_json()
            media = Path(b["path"]).expanduser()
            if not media.is_file():
                raise ValueError(f"找不到檔案：{media}")
            return self.send_json(create_project(media, b.get("options", {})))
        if parts == ["api", "import-url"]:
            return self.send_json({"id": start_import(self.body_json().get("url", "").strip())})
        if parts == ["api", "upload"]:
            name = urllib.parse.unquote(self.headers.get("X-Filename", "upload.mp4"))
            n = int(self.headers["Content-Length"])
            up = ROOT / "uploads"
            up.mkdir(exist_ok=True)
            dest = up / f"{time.strftime('%Y%m%d-%H%M%S')}-{Path(name).name}"
            with open(dest, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            return self.send_json({"path": str(dest)})
        if parts[:2] == ["api", "projects"] and len(parts) >= 5 and parts[3] == "highlights":
            p = project(parts[2])
            hid = parts[4]
            b = self.body_json()
            if len(parts) == 5:
                return self.send_json(highlights.save_plan(p, hid, b))
            if parts[5] == "render":
                start_task(p, "render", lambda: highlights.render_highlight(p, hid))
                return self.send_json({"ok": True})
            raise KeyError
        if parts[:2] == ["api", "projects"] and len(parts) == 4:
            p = project(parts[2])
            sub = parts[3]
            b = self.body_json()
            if sub == "summary":
                start_task(p, "summary", lambda: highlights.summarize(p, b))
                return self.send_json({"ok": True})
            if sub == "highlights":
                start_task(p, "highlight", lambda: highlights.plan_highlight(p, b))
                return self.send_json({"ok": True})
            if sub == "export-video":
                start_task(p, "render_full", lambda: highlights.render_full(p, b))
                return self.send_json({"ok": True})
            if sub == "options":
                m = p.meta
                m["options"] = {**m.get("options", {}), **b}
                p.update(options=m["options"])
                return self.send_json({"ok": True})
            if sub == "run":
                stages = b.get("stages") or pipeline.STAGES
                bad = [s for s in stages if s not in pipeline.STAGES]
                if bad:
                    raise ValueError(f"unknown stages {bad}")
                if b.get("force"):  # drop caches of the requested stages so they recompute
                    for s in stages:
                        for f in {"asr": ["asr.json"], "correct": ["corrected.json"],
                                  "reference": ["reference.json"]}.get(s, []):
                            (p.dir / f).unlink(missing_ok=True)
                        if s == "reference":
                            for f in (p.dir / "reference").glob("chunk_*"):
                                f.unlink()
                start_run(p, stages)
                return self.send_json({"ok": True})
            if sub == "cues":
                doc = {"cues": b["cues"], "edited": True, "saved": time.time()}
                pipeline.apply_cjk_spacing(doc["cues"], p.options)
                for i, c in enumerate(doc["cues"]):
                    c["id"] = i + 1
                p.write("cues.json", doc)
                return self.send_json({"ok": True, "count": len(doc["cues"])})
            if sub == "translate-missing":
                pid = p.dir.name
                if pid in RUNNING and RUNNING[pid].is_alive():
                    raise RuntimeError("這個專案正在執行中")

                def work():
                    p.status(state="running", stage="translate", progress=0, error=None)
                    try:
                        pipeline.stage_translate(p, only_missing=True)
                        p.status(state="done", stage=None, progress=1)
                    except Exception as e:
                        p.log(f"✗ {e}")
                        p.status(state="error", error=str(e))

                RUNNING[pid] = threading.Thread(target=work, daemon=True)
                RUNNING[pid].start()
                return self.send_json({"ok": True})
        raise KeyError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    load_env()
    PROJECTS.mkdir(exist_ok=True)
    # a server restart orphans any "running" status
    for f in PROJECTS.glob("*/project.json"):
        m = json.loads(f.read_text())
        if (m.get("status") or {}).get("state") == "running":
            m["status"]["state"] = "error"
            m["status"]["error"] = "server 重啟，執行中斷（重跑即可，已完成的區塊有快取）"
            f.write_text(json.dumps(m, ensure_ascii=False, indent=1))
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Subtitle Studio → http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
