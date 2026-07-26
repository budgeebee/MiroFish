#!/usr/bin/env python3
"""MiroFish management API — pipeline control and report serving over HTTP."""

import json
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

ET = ZoneInfo("America/New_York")
MIROFISH_DIR = Path(os.getenv("MIROFISH_DIR", "/data"))
OUTPUT_DIR = Path(os.getenv("MIROFISH_OUTPUT_DIR", str(MIROFISH_DIR / "output")))
CRUCIX_LATEST = Path(os.getenv("CRUCIX_LATEST", "/crucix/runs/latest.json"))
PIDFILE = OUTPUT_DIR / ".mirofish.pid"
STATEFILE = OUTPUT_DIR / ".pipeline_state.json"
REPORT_MIN_BYTES = 1000

app = FastAPI()


def validate_report(path):
    """Return machine-readable evidence that a report is safe to serve."""
    path = Path(path)
    evidence = {
        "path": str(path),
        "valid": False,
        "size_bytes": None,
        "reason": None,
    }
    if (
        path.name.startswith(".")
        or ".tmp" in path.name
        or ".partial" in path.name
    ):
        evidence["reason"] = "partial filename"
        return evidence
    try:
        raw = path.read_bytes()
        evidence["size_bytes"] = len(raw)
    except OSError as error:
        evidence["reason"] = f"unreadable: {error.strerror or error}"
        return evidence
    if not raw:
        evidence["reason"] = "zero-byte report"
        return evidence
    if len(raw) < REPORT_MIN_BYTES:
        evidence["reason"] = (
            f"undersized report ({len(raw)} < {REPORT_MIN_BYTES} bytes)"
        )
        return evidence
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        evidence["reason"] = "invalid UTF-8"
        return evidence
    evidence["valid"] = True
    evidence["reason"] = "valid"
    return evidence


def find_todays_report(output_dir=None, now=None):
    output_dir = Path(output_dir or OUTPUT_DIR)
    current = now or datetime.now(ET)
    today = current.strftime("%Y%m%d")
    for pattern in [f"prediction_{today}_en.md", f"prediction_{today}*.md"]:
        matches = sorted(
            (
                path for path in output_dir.glob(pattern)
                if "brief" not in path.name and validate_report(path)["valid"]
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0], today
    return None, today


def process_state(pidfile=None):
    pidfile = Path(pidfile or PIDFILE)
    result = {
        "running": False,
        "pid": None,
        "pidfile": str(pidfile),
        "reason": "missing",
    }
    if not pidfile.exists():
        return result
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
        result["pid"] = pid
        os.kill(pid, 0)
        result["running"] = True
        result["reason"] = "running"
        return result
    except PermissionError:
        result["running"] = True
        result["reason"] = "running (permission denied)"
        return result
    except (ValueError, ProcessLookupError, OSError):
        result["reason"] = "stale or invalid pidfile"
        try:
            pidfile.unlink()
        except OSError:
            pass
        return result


def is_running():
    return process_state()["running"]


def checkpoint_state(statefile=None, now=None):
    statefile = Path(statefile or STATEFILE)
    result = {
        "available": False,
        "current": False,
        "path": str(statefile),
        "completed_step": 0,
        "date": None,
        "updated_at": None,
        "reason": "missing",
    }
    if not statefile.exists():
        return result
    try:
        data = json.loads(statefile.read_text(encoding="utf-8"))
        completed_step = int(data.get("completed_step", 0))
        today = (now or datetime.now(ET)).strftime("%Y-%m-%d")
        is_current = data.get("date") == today and completed_step > 0
        result.update({
            "available": completed_step > 0,
            "current": is_current,
            "completed_step": completed_step,
            "date": data.get("date"),
            "updated_at": data.get("updated_at"),
            "reason": (
                "current"
                if is_current
                else "empty"
                if completed_step <= 0
                else "stale"
            ),
        })
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        result["reason"] = "invalid"
    return result


def has_checkpoint():
    return checkpoint_state()["current"]


def find_last_valid_report(output_dir=None):
    output_dir = Path(output_dir or OUTPUT_DIR)
    matches = sorted(
        (
            path for path in output_dir.glob("prediction_*.md")
            if "brief" not in path.name and validate_report(path)["valid"]
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def artifact_sibling(report_path, kind):
    report_path = Path(report_path)
    stem = report_path.stem
    if stem.endswith("_en"):
        stem = stem[:-3]
    suffix = {
        "manifest": ".manifest.json",
        "structured": ".json",
    }[kind]
    return report_path.with_name(f"{stem}{suffix}")


def load_today_artifact(kind):
    report_path, today = find_todays_report()
    if not report_path:
        raise HTTPException(404, f"No report for {today}")
    artifact_path = artifact_sibling(report_path, kind)
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(404, f"No {kind} artifact for {today}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(500, f"Invalid {kind} artifact: {error}") from error
    expected_schema = {
        "manifest": "observation-manifest.v1",
        "structured": "scenario-synthesis.v1",
    }[kind]
    if artifact.get("schema_version") != expected_schema:
        raise HTTPException(500, f"Invalid {kind} schema")
    if artifact.get("report_id") != artifact_path.name.removesuffix(
        ".manifest.json" if kind == "manifest" else ".json"
    ):
        raise HTTPException(500, f"{kind} report ID does not match its filename")
    return artifact


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
    cmd = ["python3", str(script), "--max-rounds", "30"]
    if resume:
        cmd.append("--resume")
    with open(log, "w") as f:
        proc = subprocess.Popen(
            cmd, cwd=str(MIROFISH_DIR), stdout=f,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    PIDFILE.write_text(str(proc.pid))

    def _wait_and_cleanup(p):
        p.wait()
        try:
            PIDFILE.unlink()
        except FileNotFoundError:
            pass

    threading.Thread(target=_wait_and_cleanup, args=(proc,), daemon=True).start()
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
    process = process_state()
    checkpoint = checkpoint_state()
    last_report = find_last_valid_report()
    output = {
        "path": str(OUTPUT_DIR),
        "exists": OUTPUT_DIR.is_dir(),
        "readable": OUTPUT_DIR.is_dir() and os.access(OUTPUT_DIR, os.R_OK),
        "writable": OUTPUT_DIR.is_dir() and os.access(OUTPUT_DIR, os.W_OK),
    }
    source = {
        "path": str(CRUCIX_LATEST),
        "exists": CRUCIX_LATEST.is_file(),
        "readable": CRUCIX_LATEST.is_file() and os.access(CRUCIX_LATEST, os.R_OK),
    }
    report_evidence = (
        validate_report(last_report)
        if last_report
        else {"valid": False, "path": None, "size_bytes": None, "reason": "none"}
    )
    healthy = output["readable"] and output["writable"] and source["readable"]
    return {
        "status": "ok" if healthy else "degraded",
        "output": output,
        "source": source,
        "process": process,
        "checkpoint": checkpoint,
        "last_valid_report": report_evidence,
    }


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
    return {
        "available": False,
        "date": today,
        "pipeline_running": running,
        "has_checkpoint": has_checkpoint(),
        "checkpoint": checkpoint_state(),
    }


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


@app.get("/output/today/manifest")
def output_today_manifest():
    return load_today_artifact("manifest")


@app.get("/output/today/structured")
def output_today_structured():
    return load_today_artifact("structured")


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
