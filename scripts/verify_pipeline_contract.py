#!/usr/bin/env python3
"""Deterministic P1-M2 contract verifier; no Docker or network calls."""

import importlib.util
import json
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = load_module("mirofish_api_server", ROOT / "api_server.py")
pipeline = load_module(
    "crucix_to_mirofish_contract",
    ROOT / "scripts/crucix_to_mirofish.py",
)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def write_bytes(path, payload):
    path.write_bytes(payload)
    return path


def report_validation_fixtures(temp_dir):
    zero = write_bytes(temp_dir / "prediction_zero.md", b"")
    small = write_bytes(temp_dir / "prediction_small.md", b"x" * 999)
    invalid_utf8 = write_bytes(
        temp_dir / "prediction_invalid.md",
        b"\xff" * api.REPORT_MIN_BYTES,
    )
    partial = write_bytes(
        temp_dir / "prediction_partial.md.partial",
        b"x" * api.REPORT_MIN_BYTES,
    )
    valid = write_bytes(
        temp_dir / "prediction_valid.md",
        ("# Valid report\n" + ("evidence\n" * 140)).encode("utf-8"),
    )

    expected = [
        (zero, False, "zero-byte"),
        (small, False, "undersized"),
        (invalid_utf8, False, "invalid UTF-8"),
        (partial, False, "partial filename"),
        (valid, True, "valid"),
    ]
    for path, is_valid, reason in expected:
        evidence = api.validate_report(path)
        check(evidence["valid"] is is_valid, f"{path.name}: wrong validity")
        check(reason in evidence["reason"], f"{path.name}: wrong reason")


def atomic_write_fixtures(temp_dir):
    target = temp_dir / "prediction_atomic.md"
    original = "# Original\n" + ("old\n" * 260)
    replacement = "# Replacement\n" + ("new\n" * 260)
    pipeline._atomic_write_text(
        target,
        original,
        pipeline._validate_report_text,
    )

    try:
        pipeline._atomic_write_text(
            target,
            "too small",
            pipeline._validate_report_text,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid atomic write was accepted")

    check(target.read_text(encoding="utf-8") == original, "failed write replaced target")
    check(not list(temp_dir.glob(f".{target.name}.*.tmp")), "temporary sibling leaked")

    pipeline._atomic_write_text(
        target,
        replacement,
        pipeline._validate_report_text,
    )
    check(target.read_text(encoding="utf-8") == replacement, "valid write not published")


def checkpoint_and_run_or_skip_fixtures(temp_dir):
    original = {
        "OUTPUT_DIR": api.OUTPUT_DIR,
        "PIDFILE": api.PIDFILE,
        "STATEFILE": api.STATEFILE,
        "CRUCIX_LATEST": api.CRUCIX_LATEST,
        "launch": api.launch,
    }
    api.OUTPUT_DIR = temp_dir
    api.PIDFILE = temp_dir / ".mirofish.pid"
    api.STATEFILE = temp_dir / ".pipeline_state.json"
    api.CRUCIX_LATEST = temp_dir / "latest.json"
    api.CRUCIX_LATEST.write_text("{}", encoding="utf-8")
    launches = []

    def fake_launch(resume=False):
        launches.append(resume)
        return {"launched": True, "resume": resume}

    api.launch = fake_launch
    today = datetime.now(ZoneInfo("America/New_York"))
    report_name = f"prediction_{today:%Y%m%d}_120000.md"
    report = temp_dir / report_name
    valid_text = "# Current report\n" + ("evidence\n" * 140)

    try:
        pipeline._atomic_write_text(report, valid_text, pipeline._validate_report_text)
        first = api.run_or_skip()
        second = api.run_or_skip()
        check(first["skipped"] and second["skipped"], "valid report was not idempotent")
        check(launches == [], "idempotent skip launched the pipeline")
        health = api.health()
        for key in ["output", "source", "process", "checkpoint", "last_valid_report"]:
            check(key in health, f"health evidence missing {key}")
        check(health["last_valid_report"]["valid"], "health missed valid report")

        report.unlink()
        report.write_bytes(b"x" * (api.REPORT_MIN_BYTES - 1))
        api.STATEFILE.write_text(json.dumps({
            "date": today.strftime("%Y-%m-%d"),
            "completed_step": 3,
            "updated_at": today.isoformat(),
        }), encoding="utf-8")
        current = api.run_or_skip()
        check(current.get("auto_resumed") is True, "current checkpoint did not resume")
        check(launches == [True], "current checkpoint used the wrong launch mode")

        api.STATEFILE.write_text(json.dumps({
            "date": "2000-01-01",
            "completed_step": 4,
            "updated_at": "2000-01-01T00:00:00Z",
        }), encoding="utf-8")
        stale = api.run_or_skip()
        check(stale["launched"] and not stale["resume"], "stale checkpoint did not rerun")
        check(launches == [True, False], "stale checkpoint launch was not fresh")
    finally:
        for name, value in original.items():
            setattr(api, name, value)


def source_total_fixture():
    markdown = pipeline.crucix_to_markdown({
        "crucix": {"timestamp": "2026-07-25T00:00:00Z", "sourcesOk": 2},
        "sources": {"one": {}, "two": {}},
        "sourceHealth": {
            "one": {"status": "ok"},
            "two": {"status": "ok"},
            "three": {"status": "unavailable"},
        },
    })
    check("2/3 sources reporting" in markdown, "source total was not derived from input")


def compose_fixture():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for required in [
        "CRUCIX_URL=http://host.docker.internal:3117",
        "SCHWALPACA_URL=http://host.docker.internal:8855",
        "/crucix/runs:ro",
        "/journals/schwalpaca:ro",
        "/journals/kalshi:ro",
    ]:
        check(required in compose, f"compose contract missing {required}")


def main():
    with tempfile.TemporaryDirectory(prefix="mirofish-p1-m2-") as directory:
        temp_dir = Path(directory)
        report_validation_fixtures(temp_dir)
        atomic_write_fixtures(temp_dir)
        checkpoint_and_run_or_skip_fixtures(temp_dir)
    source_total_fixture()
    compose_fixture()
    print(
        "PASS: zero-byte, undersized, invalid UTF-8, partial, valid, atomic-write, "
        "stale-checkpoint, current-checkpoint, and idempotent-skip fixtures"
    )


if __name__ == "__main__":
    main()
