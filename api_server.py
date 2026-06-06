#!/usr/bin/env python3
"""MiroFish management API — pipeline control and report serving over HTTP."""

import glob
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

ET = ZoneInfo("America/New_York")
MIROFISH_DIR = Path(os.getenv("MIROFISH_DIR", str(Path.home() / "Projects/MiroFish")))
OUTPUT_DIR = Path(os.getenv("MIROFISH_OUTPUT_DIR", str(MIROFISH_DIR / "output")))
PIDFILE = OUTPUT_DIR / ".mirofish.pid"

app = FastAPI()


def find_todays_report():
    today = datetime.now(ET).strftime("%Y%m%d")
    for pattern in [f"prediction_{today}_EN.md", f"prediction_{today}*.md"]:
        matches = sorted(
            [m for m in glob.glob(str(OUTPUT_DIR / pattern)) if "brief" not in Path(m).name],
            key=os.path.getmtime, reverse=True,
        )
        if matches:
            return Path(matches[0]), today
    return None, today


def is_running():
    if not PIDFILE.exists():
        return False
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        try:
            PIDFILE.unlink()
        except FileNotFoundError:
            pass
        return False


def has_checkpoint():
    state = OUTPUT_DIR / ".pipeline_state.json"
    if not state.exists():
        return False
    try:
        data = json.loads(state.read_text())
        today = datetime.now(ET).strftime("%Y-%m-%d")
        return data.get("date") == today and data.get("completed_step", 0) > 0
    except (ValueError, KeyError):
        return False


def kill_zombie_sims():
    try:
        r = subprocess.run(
            ["docker", "exec", "mirofish", "pgrep", "-f", "run_parallel_simulation.py"],
            capture_output=True, text=True, timeout=10,
        )
        pids = r.stdout.strip().split() if r.stdout.strip() else []
        if pids:
            subprocess.run(
                ["docker", "exec", "mirofish", "kill", "-9"] + pids,
                capture_output=True, timeout=10,
            )
        return [int(p) for p in pids]
    except Exception:
        return []


def launch(resume=False):
    script = MIROFISH_DIR / "scripts/crucix_to_mirofish.py"
    if not script.exists():
        raise HTTPException(500, f"Pipeline script not found: {script}")
    killed = kill_zombie_sims()
    log = OUTPUT_DIR / ".mirofish_run.log"
    cmd = ["python3", str(script), "--max-rounds", "40"]
    if resume:
        cmd.append("--resume")
    with open(log, "w") as f:
        proc = subprocess.Popen(
            cmd, cwd=str(MIROFISH_DIR), stdout=f,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    PIDFILE.write_text(str(proc.pid))
    result = {"launched": True, "pid": proc.pid, "log": str(log), "resume": resume}
    if killed:
        result["zombie_sims_killed"] = killed
    return result


def build_summary(path):
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    title = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), "")
    chapters, current, body = [], None, []
    for line in lines:
        if line.startswith("## "):
            if current:
                chapters.append({"heading": current, "summary": " ".join(body)[:500]})
            current, body = line.lstrip("# ").strip(), []
        elif current and line.startswith("> "):
            body.append(line.lstrip("> ").strip())
        elif current and line.startswith("**") and not body:
            body.append(line.strip())
    if current:
        chapters.append({"heading": current, "summary": " ".join(body)[:500]})
    return {
        "available": True, "date": path.name, "path": str(path),
        "title": title, "chapters": chapters, "total_chars": len(text),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    path, today = find_todays_report()
    running = is_running()
    if path:
        return {
            "available": True, "date": today, "path": str(path),
            "size_bytes": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            "pipeline_running": running,
        }
    return {"available": False, "date": today, "pipeline_running": running,
            "has_checkpoint": has_checkpoint()}


@app.get("/output/today")
def output_today():
    path, today = find_todays_report()
    if not path:
        raise HTTPException(404, f"No report for {today}")
    return build_summary(path)


@app.get("/output/today/full", response_class=PlainTextResponse)
def output_today_full():
    path, today = find_todays_report()
    if not path:
        raise HTTPException(404, f"No report for {today}")
    return path.read_text(encoding="utf-8")


@app.post("/run")
def run():
    if is_running():
        return {"already_running": True}
    return launch()


@app.post("/resume")
def resume():
    if is_running():
        return {"already_running": True}
    return launch(resume=True)


@app.post("/run-or-skip")
def run_or_skip():
    path, today = find_todays_report()
    if path:
        return {"skipped": True, "reason": "report already exists", "path": str(path)}
    if is_running():
        return {"skipped": True, "reason": "pipeline already running"}
    if has_checkpoint():
        result = launch(resume=True)
        result["auto_resumed"] = True
        return result
    return launch()
