#!/usr/bin/env python3
"""Deterministic P1-M2 contract verifier; no Docker or network calls."""

import importlib.util
import json
import tempfile
from copy import deepcopy
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


def expect_rejected(callback, expected_message):
    try:
        callback()
    except ValueError as error:
        check(
            expected_message.lower() in str(error).lower(),
            f"wrong rejection for {expected_message!r}: {error}",
        )
    else:
        raise AssertionError(f"fixture was not rejected: {expected_message}")


def scenario_contract_fixtures(temp_dir):
    fixture = json.loads(
        (ROOT / "fixtures/scenario-input.json").read_text(encoding="utf-8")
    )
    expected = json.loads(
        (ROOT / "fixtures/scenario-expected-shape.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = pipeline.build_observation_manifest(
        fixture["crucix"],
        fixture["supplements"],
        report_id=fixture["report_id"],
        fetched_at=fixture["fetched_at"],
    )
    second_manifest = pipeline.build_observation_manifest(
        fixture["crucix"],
        fixture["supplements"],
        report_id=fixture["report_id"],
        fetched_at=fixture["fetched_at"],
    )
    check(manifest == second_manifest, "manifest hashing is not deterministic")
    check(
        manifest["schema_version"] == expected["manifest_schema"],
        "wrong manifest schema",
    )
    check(
        len(manifest["observations"]) == expected["observation_count"],
        "wrong observation count",
    )
    check(
        all(
            item["schema_version"] == expected["observation_schema"]
            for item in manifest["observations"]
        ),
        "wrong observation schema",
    )

    polymarket_groups = {
        item["independence_group"]
        for item in manifest["observations"]
        if item["source_id"] in {
            "Crucix/Polymarket",
            "Crucix/Adanos/polymarket",
        }
    }
    check(
        polymarket_groups == {"polymarket"},
        "direct and Adanos Polymarket provenance diverged",
    )
    news_observation = next(
        item
        for item in manifest["observations"]
        if item["source_id"] == "Supplement/NewsAggregator"
    )
    check(
        news_observation["staleness_seconds"] == 300,
        "source and fetch timestamps were not kept distinct",
    )

    artifact = pipeline.build_scenario_synthesis(
        manifest,
        fixture["hypotheses"],
        generated_at=fixture["fetched_at"],
    )
    markdown = pipeline.render_scenario_markdown(artifact, manifest)
    check(
        artifact["schema_version"] == expected["scenario_schema"],
        "wrong scenario schema",
    )
    check(
        len(artifact["hypotheses"]) == expected["hypothesis_count"],
        "wrong hypothesis count",
    )
    for section in expected["required_sections"]:
        check(f"## {section}" in markdown, f"render missing section {section}")
    check(
        pipeline.legacy_consumer_diff() == expected["legacy_consumer_diff"],
        "legacy consumer diff changed",
    )

    manifest_path = temp_dir / "prediction_fixture.manifest.json"
    pipeline._atomic_write_json(
        manifest_path,
        manifest,
        pipeline.validate_observation_manifest,
    )
    check(
        json.loads(manifest_path.read_text(encoding="utf-8")) == manifest,
        "atomic manifest changed payload",
    )

    duplicate = deepcopy(manifest)
    duplicate["observations"].append(deepcopy(duplicate["observations"][0]))
    expect_rejected(
        lambda: pipeline.validate_observation_manifest(duplicate),
        "duplicate",
    )

    invalid_status = deepcopy(manifest)
    invalid_status["observations"][0]["status"] = "maybe"
    expect_rejected(
        lambda: pipeline.validate_observation_manifest(invalid_status),
        "invalid status",
    )

    invalid_timestamp = deepcopy(manifest)
    invalid_timestamp["observations"][0]["fetched_at"] = "yesterday"
    expect_rejected(
        lambda: pipeline.validate_observation_manifest(invalid_timestamp),
        "invalid isoformat",
    )

    market_without_provenance = deepcopy(manifest)
    market_item = next(
        item
        for item in market_without_provenance["observations"]
        if item["market_derived"]
    )
    market_item["independence_group"] = ""
    expect_rejected(
        lambda: pipeline.validate_observation_manifest(
            market_without_provenance
        ),
        "independence_group",
    )

    dangling = deepcopy(artifact)
    dangling["hypotheses"][0]["supporting_observation_ids"].append("obs-missing")
    expect_rejected(
        lambda: pipeline.validate_scenario_synthesis(dangling, manifest),
        "dangling evidence",
    )

    market_as_support = deepcopy(artifact)
    market_as_support["hypotheses"][0]["supporting_observation_ids"].append(
        next(
            item["observation_id"]
            for item in manifest["observations"]
            if item["market_derived"]
        )
    )
    expect_rejected(
        lambda: pipeline.validate_scenario_synthesis(
            market_as_support,
            manifest,
        ),
        "market observations must use market evidence",
    )

    invented_probability = deepcopy(artifact)
    invented_probability["hypotheses"][0]["probability"] = 0.75
    expect_rejected(
        lambda: pipeline.validate_scenario_synthesis(
            invented_probability,
            manifest,
        ),
        "hypothesis fields differ",
    )
    return manifest, artifact, markdown


def artifact_endpoint_fixture(temp_dir, manifest, artifact):
    original_output = api.OUTPUT_DIR
    api.OUTPUT_DIR = temp_dir
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    report_id = f"prediction_{today}_120000"
    report_path = temp_dir / f"{report_id}.md"
    report_path.write_text(
        "# Fixture\n" + ("structured evidence\n" * 80),
        encoding="utf-8",
    )
    manifest = deepcopy(manifest)
    artifact = deepcopy(artifact)
    manifest["report_id"] = report_id
    artifact["report_id"] = report_id
    pipeline._atomic_write_json(
        temp_dir / f"{report_id}.manifest.json",
        manifest,
        pipeline.validate_observation_manifest,
    )
    pipeline._atomic_write_json(
        temp_dir / f"{report_id}.json",
        artifact,
        lambda value: pipeline.validate_scenario_synthesis(value, manifest),
    )
    try:
        check(
            api.output_today_manifest()["report_id"] == report_id,
            "manifest endpoint returned wrong artifact",
        )
        check(
            api.output_today_structured()["report_id"] == report_id,
            "structured endpoint returned wrong artifact",
        )
    finally:
        api.OUTPUT_DIR = original_output


def emit_cp0_bundle(manifest, artifact, markdown):
    output_dir = Path("/tmp/mirofish-p1-m3-cp0")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_id = artifact["report_id"]
    pipeline._atomic_write_text(output_dir / f"{report_id}.md", markdown)
    pipeline._atomic_write_json(
        output_dir / f"{report_id}.json",
        artifact,
        lambda value: pipeline.validate_scenario_synthesis(value, manifest),
    )
    pipeline._atomic_write_json(
        output_dir / f"{report_id}.manifest.json",
        manifest,
        pipeline.validate_observation_manifest,
    )
    summary = {
        "schema_version": manifest["schema_version"],
        "report_id": manifest["report_id"],
        "observation_count": len(manifest["observations"]),
        "status_counts": {
            status: sum(
                item["status"] == status
                for item in manifest["observations"]
            )
            for status in sorted(pipeline.OBSERVATION_STATUSES)
        },
        "market_independence_groups": sorted({
            item["independence_group"]
            for item in manifest["observations"]
            if item["market_derived"]
        }),
    }
    pipeline._atomic_write_json(
        output_dir / "manifest-summary.json",
        summary,
    )
    pipeline._atomic_write_json(
        output_dir / "legacy-consumer-diff.json",
        pipeline.legacy_consumer_diff(),
    )
    return output_dir


def main():
    with tempfile.TemporaryDirectory(prefix="mirofish-p1-") as directory:
        temp_dir = Path(directory)
        report_validation_fixtures(temp_dir)
        atomic_write_fixtures(temp_dir)
        checkpoint_and_run_or_skip_fixtures(temp_dir)
        manifest, artifact, markdown = scenario_contract_fixtures(temp_dir)
        artifact_endpoint_fixture(temp_dir, manifest, artifact)
    source_total_fixture()
    compose_fixture()
    cp0_dir = emit_cp0_bundle(manifest, artifact, markdown)
    print(
        "PASS: zero-byte, undersized, invalid UTF-8, partial, valid, atomic-write, "
        "stale/current checkpoint, idempotent skip, observation provenance, "
        "scenario synthesis, schema rejection, and artifact endpoint fixtures"
    )
    print(f"CP0 bundle: {cp0_dir}")


if __name__ == "__main__":
    main()
