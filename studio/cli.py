"""Headless run:  python3 -m studio.cli <media> [--stages asr,reference,...] [--locale en-US] [--project ID]"""
import argparse
import json
from pathlib import Path

from . import pipeline, server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media", nargs="?")
    ap.add_argument("--project")
    ap.add_argument("--stages", default=",".join(pipeline.STAGES))
    ap.add_argument("--opt", action="append", default=[], help="key=value (JSON value or string)")
    a = ap.parse_args()
    server.load_env()
    server.PROJECTS.mkdir(exist_ok=True)
    opts = {}
    for kv in a.opt:
        k, v = kv.split("=", 1)
        try:
            opts[k] = json.loads(v)
        except json.JSONDecodeError:
            opts[k] = v
    if a.project:
        p = server.project(a.project)
        if opts:
            p.update(options={**p.meta.get("options", {}), **opts})
    else:
        p = pipeline.Project(server.PROJECTS / server.create_project(Path(a.media).resolve(), opts)["id"])
    print("project", p.dir.name)
    pipeline.run_stages(p, a.stages.split(","))
    print(json.dumps(p.meta.get("usage"), indent=1))


if __name__ == "__main__":
    main()
