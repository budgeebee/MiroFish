#!/usr/bin/env python3
"""Deterministic P1-M2 contract verifier; no Docker or network calls."""

import importlib.util
import json
import os
import subprocess
import sys
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


def main_manifest_input_fixture():
    source = (ROOT / "scripts/crucix_to_mirofish.py").read_text(encoding="utf-8")
    check(
        "manifest = build_observation_manifest(\n        crucix_data,"
        in source,
        "main no longer passes the loaded Crucix document to the manifest",
    )
    check(
        "for sym, data in sym_results.items()" not in source,
        "sentiment loop can overwrite the loaded Crucix document",
    )


def compose_fixture():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for required in [
        "CRUCIX_URL=http://host.docker.internal:3117",
        "SCHWALPACA_URL=http://host.docker.internal:8855",
        "LLAMA_SWAP_URL=http://host.docker.internal:8090",
        "/crucix/runs:ro",
        "/journals/schwalpaca:ro",
        "/journals/kalshi:ro",
    ]:
        check(required in compose, f"compose contract missing {required}")


def host_default_fixture():
    environment = dict(os.environ)
    for name in [
        "MIROFISH_DIR",
        "MIROFISH_OUTPUT_DIR",
        "MIROFISH_URL",
        "CRUCIX_LATEST",
        "CRUCIX_URL",
        "SCHWALPACA_URL",
        "SCHWALPACA_JOURNAL",
        "KALSHI_JOURNAL",
    ]:
        environment.pop(name, None)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, json, pathlib;"
                f"p=pathlib.Path({str(ROOT / 'scripts/crucix_to_mirofish.py')!r});"
                "s=importlib.util.spec_from_file_location('host_defaults',p);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "print(json.dumps({"
                "'root':str(m.MIROFISH_DIR),"
                "'output':str(m.OUTPUT_DIR),"
                "'crucix':str(m.CRUCIX_LATEST),"
                "'mirofish_url':m.MIROFISH_URL,"
                "'schwalpaca_url':m.SCHWALPACA_URL}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    defaults = json.loads(probe.stdout)
    check(defaults["root"] == str(ROOT), "host default lost the MiroFish root")
    check(
        defaults["output"] == str(ROOT / "output"),
        "host default lost the MiroFish output directory",
    )
    check(
        defaults["crucix"].endswith("/Projects/Crucix/runs/latest.json"),
        "host default lost the Crucix latest path",
    )
    check(
        defaults["mirofish_url"] == "http://localhost:5005",
        "host default does not reach MiroFish",
    )
    check(
        defaults["schwalpaca_url"] == "http://localhost:8855",
        "host default does not reach Schwalpaca",
    )


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


def polymarket_selection_fixtures():
    fetched_at = "2026-07-25T12:00:00Z"

    def market(contract_id, question):
        return {
            "venueContractId": contract_id,
            "question": question,
            "outcomeLabel": "Yes",
            "observedAt": "2026-07-25T11:58:00Z",
            "fetchedAt": fetched_at,
            "marketDerived": True,
            "independenceGroup": "polymarket",
        }

    top = [market(f"top-{index}", f"Top question {index}?") for index in range(11)]
    shifts = [deepcopy(top[0])]
    shifts.extend(
        market(f"shift-{index}", f"Shift question {index}?")
        for index in range(1, 6)
    )
    crucix = {
        "crucix": {"timestamp": fetched_at},
        "sourceHealth": {
            "Polymarket": {
                "status": "ok",
                "sourceType": "market",
                "independenceGroup": "polymarket",
                "marketDerived": True,
            }
        },
        "sources": {
            "Polymarket": {
                "status": "ok",
                "timestamp": fetched_at,
                "sourceType": "market",
                "independenceGroup": "polymarket",
                "marketDerived": True,
                "top": top,
                "highProbShifts": shifts,
            }
        },
    }
    manifest = pipeline.build_observation_manifest(
        crucix,
        report_id="prediction_selection_fixture",
        fetched_at=fetched_at,
    )
    granular = [
        item
        for item in manifest["observations"]
        if item["source_id"].startswith("Crucix/Polymarket/")
    ]
    expected_sources = [
        *(f"Crucix/Polymarket/top-{index}" for index in range(10)),
        *(f"Crucix/Polymarket/shift-{index}" for index in range(1, 5)),
    ]
    granular_sources = [item["source_id"] for item in granular]
    check(
        granular_sources == expected_sources,
        "Polymarket selection lost caps, order, or first-seen deduplication",
    )
    check(
        granular[0]["payload_ref"] == "/sources/Polymarket/top/0"
        and granular[-1]["payload_ref"]
        == "/sources/Polymarket/highProbShifts/4",
        "Polymarket selection emitted the wrong payload refs",
    )
    check(
        "Crucix/Polymarket/top-10" not in granular_sources
        and "Crucix/Polymarket/shift-5" not in granular_sources,
        "Polymarket selection exceeded the 10/5 caps",
    )
    check(
        sum(
            item["source_id"] == "Crucix/Polymarket"
            for item in manifest["observations"]
        ) == 1,
        "Polymarket aggregate provenance was not retained exactly once",
    )
    check(
        granular[0]["content_sha256"] == pipeline._sha256(top[0]),
        "granular Polymarket observation did not hash the individual market",
    )

    malformed = deepcopy(crucix)
    del malformed["sources"]["Polymarket"]["top"][0]["venueContractId"]
    expect_rejected(
        lambda: pipeline.build_observation_manifest(
            malformed,
            report_id="prediction_malformed_fixture",
            fetched_at=fetched_at,
        ),
        "venueContractId",
    )

    missing_question = deepcopy(crucix)
    del missing_question["sources"]["Polymarket"]["top"][0]["question"]
    expect_rejected(
        lambda: pipeline._polymarket_observation_labels(missing_question),
        "missing question",
    )

    empty = deepcopy(crucix)
    empty["sources"]["Polymarket"]["top"] = []
    empty["sources"]["Polymarket"]["highProbShifts"] = []
    aggregate_only = pipeline.build_observation_manifest(
        empty,
        report_id="prediction_aggregate_fixture",
        fetched_at=fetched_at,
    )
    check(
        [item["source_id"] for item in aggregate_only["observations"]]
        == ["Crucix/Polymarket"],
        "empty Polymarket arrays did not retain aggregate-only provenance",
    )
    pipeline.build_scenario_synthesis(
        aggregate_only,
        [{
            "hypothesis_id": "aggregate-fallback",
            "claim": "Only aggregate market provenance is available.",
            "epistemic_status": "unknown",
            "confidence": "low",
            "supporting_observation_ids": [],
            "contradicting_observation_ids": [],
            "unknowns": ["No exact contract was supplied."],
            "falsifiers": ["An exact contract appears."],
            "watch_conditions": ["Watch for a populated market payload."],
            "affected_entities": [],
            "market_source_ids": ["Crucix/Polymarket"],
        }],
        generated_at=fetched_at,
    )


def scenario_repair_backend_fixture():
    calls = []
    original_session = pipeline._session
    original_key = os.environ.get("LLM_API_KEY")
    original_model = os.environ.get("LLM_MODEL_NAME")

    class FixtureResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"fixture":true}'}}]}

    class FixtureSession:
        def post(self, endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            return FixtureResponse()

    try:
        pipeline._session = FixtureSession()
        os.environ["LLM_API_KEY"] = "fixture-primary-key"
        os.environ["LLM_MODEL_NAME"] = "MiniMax-M3"
        content = pipeline._request_scenario_repair("fixture prompt")
    finally:
        pipeline._session = original_session
        if original_key is None:
            os.environ.pop("LLM_API_KEY", None)
        else:
            os.environ["LLM_API_KEY"] = original_key
        if original_model is None:
            os.environ.pop("LLM_MODEL_NAME", None)
        else:
            os.environ["LLM_MODEL_NAME"] = original_model

    check(content == '{"fixture":true}', "primary repair response changed")
    check(len(calls) == 1, "scenario repair did not use exactly one backend")
    endpoint, request = calls[0]
    check(
        endpoint == f"{pipeline.LLM_BASE_URL.rstrip('/')}/chat/completions"
        and endpoint != f"{pipeline.LLAMA_SWAP_URL.rstrip('/')}/v1/chat/completions",
        "scenario repair did not route exclusively to the primary API",
    )
    check(
        request["headers"].get("Authorization") == "Bearer fixture-primary-key"
        and request["json"]["model"] == "MiniMax-M3"
        and request["json"]["temperature"] == 0.1
        and request["timeout"] == 300,
        "scenario repair lost the configured MiniMax M3 request contract",
    )


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

    granular_polymarket = [
        item
        for item in manifest["observations"]
        if item["source_id"].startswith("Crucix/Polymarket/")
    ]
    check(
        [item["source_id"] for item in granular_polymarket] == [
            "Crucix/Polymarket/fixture-contract",
            "Crucix/Polymarket/fixture-shift-contract",
        ],
        "fixture did not emit exact per-contract Polymarket observations",
    )
    check(
        [item["payload_ref"] for item in granular_polymarket] == [
            "/sources/Polymarket/top/0",
            "/sources/Polymarket/highProbShifts/1",
        ],
        "fixture emitted incorrect per-contract payload refs",
    )
    check(
        granular_polymarket[0]["source_type"] == "market"
        and granular_polymarket[0]["market_derived"] is True
        and granular_polymarket[0]["status"] == "ok"
        and granular_polymarket[0]["observed_at"]
        == "2026-07-25T11:58:00Z"
        and granular_polymarket[0]["fetched_at"]
        == "2026-07-25T12:00:00Z"
        and granular_polymarket[0]["staleness_seconds"] == 120,
        "fixture lost exact Polymarket provenance or timestamps",
    )
    check(
        sum(
            item["source_id"] == "Crucix/Polymarket"
            for item in manifest["observations"]
        ) == 1,
        "fixture lost aggregate Polymarket provenance",
    )
    polymarket_groups = {
        item["independence_group"]
        for item in manifest["observations"]
        if item["source_id"] == "Crucix/Adanos/polymarket"
        or item["source_id"].startswith("Crucix/Polymarket")
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
    source_labels = pipeline._polymarket_observation_labels(fixture["crucix"])
    evidence_index = pipeline.render_observation_reference_index(
        manifest,
        source_labels,
    )
    check(
        all(
            item["observation_id"] in evidence_index
            for item in manifest["observations"]
        ),
        "live evidence index omitted an observation ID",
    )
    check(
        "question=Will the fixture agreement be signed?" in evidence_index
        and "direction target=YES outcome" in evidence_index
        and "ends=2026-07-27T12:00:00Z" in evidence_index,
        "Polymarket reference labels omitted question, YES semantics, or end date",
    )
    check(
        "scope=aggregate-provenance-only; do not cite" in evidence_index,
        "aggregate Polymarket reference was not labeled provenance-only",
    )
    long_brief = ("Long fixture body. " * 700) + "\n\n" + evidence_index
    repair_prompt = pipeline._scenario_repair_prompt(
        "fixture report",
        long_brief,
        manifest,
    )
    check(
        evidence_index.strip() in repair_prompt
        and "[...middle of brief omitted...]" in repair_prompt,
        "scenario repair truncation did not preserve the complete reference index",
    )
    for prompt in (pipeline.SIMULATION_REQUIREMENT, repair_prompt):
        check(
            "Crucix/Polymarket/<venueContractId>" in prompt
            and "aggregate provenance only" in prompt
            and "market_observation_ids" in prompt,
            "scenario prompt lost exact Polymarket citation rules",
        )
        check(
            "market_directions" in prompt
            and "YES outcome" in prompt
            and "-1" in prompt
            and "0" in prompt
            and "1" in prompt
            and "abstention" in prompt,
            "scenario prompt lost signed direction or abstention semantics",
        )
        check(
            '"direction":-1|0|1' not in prompt
            and '{"observation_id":"obs-...","direction":-1}' in prompt
            and '{"observation_id":"obs-...","direction":0}' in prompt
            and '{"observation_id":"obs-...","direction":1}' in prompt,
            "scenario prompt contains invalid JSON direction examples",
        )
    embedded_report = (
        "# Backend intermediate\n\n"
        "Simulation narrative omitted from the canonical publication.\n\n"
        "## Scenario Synthesis (Machine-Readable)\n\n"
        "```json\n"
        + json.dumps(artifact, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
    extracted = pipeline.extract_scenario_synthesis(
        embedded_report,
        manifest,
        report_id=fixture["report_id"],
        generated_at=fixture["fetched_at"],
    )
    check(extracted == artifact, "live scenario extraction changed the artifact")

    aggregate_id = next(
        item["observation_id"]
        for item in manifest["observations"]
        if item["source_id"] == "Crucix/Polymarket"
    )
    aggregate_citation = deepcopy(artifact)
    aggregate_citation["hypotheses"][1]["market_observation_ids"].append(
        aggregate_id
    )
    expect_rejected(
        lambda: pipeline.validate_scenario_synthesis(
            aggregate_citation,
            manifest,
        ),
        "aggregate Polymarket",
    )

    exact_ids = [item["observation_id"] for item in granular_polymarket]
    aggregate_id = next(
        item["observation_id"]
        for item in manifest["observations"]
        if item["source_id"] == "Crucix/Polymarket"
    )
    adanos_market_id = next(
        item["observation_id"]
        for item in manifest["observations"]
        if item["source_id"] == "Crucix/Adanos/polymarket"
    )
    non_market_id = next(
        item["observation_id"]
        for item in manifest["observations"]
        if not item["market_derived"]
    )

    direction_values = sorted({
        entry["direction"]
        for hypothesis in artifact["hypotheses"]
        for entry in hypothesis["market_directions"]
    })
    check(
        direction_values == expected["direction_values"],
        "static fixture did not cover -1, 0, and +1 directions",
    )
    check(
        all("market_directions" in hypothesis for hypothesis in artifact["hypotheses"])
        and artifact["hypotheses"][3]["market_directions"] == [],
        "every hypothesis did not receive a market_directions list",
    )

    def reject_direction(mutator, message):
        candidate = deepcopy(artifact)
        mutator(candidate)
        expect_rejected(
            lambda: pipeline.validate_scenario_synthesis(candidate, manifest),
            message,
        )

    reject_direction(
        lambda value: value["hypotheses"][0].update(market_directions=[]),
        "exactly match ordered",
    )
    reject_direction(
        lambda value: value["hypotheses"][3]["market_directions"].append({
            "observation_id": exact_ids[0], "direction": 1,
        }),
        "exactly match ordered",
    )

    reordered = deepcopy(artifact)
    reordered["hypotheses"][0]["market_observation_ids"] = exact_ids
    reordered["hypotheses"][0]["market_directions"] = [
        {"observation_id": exact_ids[1], "direction": 0},
        {"observation_id": exact_ids[0], "direction": 1},
    ]
    expect_rejected(
        lambda: pipeline.validate_scenario_synthesis(reordered, manifest),
        "ordered",
    )

    duplicated = deepcopy(artifact)
    duplicated["hypotheses"][0]["market_observation_ids"].append(exact_ids[0])
    duplicated["hypotheses"][0]["market_directions"].append({
        "observation_id": exact_ids[0], "direction": 1,
    })
    expect_rejected(
        lambda: pipeline.validate_scenario_synthesis(duplicated, manifest),
        "duplicate exact Polymarket",
    )

    for invalid_id in (
        "obs-dangling-direction",
        aggregate_id,
        adanos_market_id,
        non_market_id,
    ):
        reject_direction(
            lambda value, invalid_id=invalid_id: value["hypotheses"][0]
            ["market_directions"][0].update(observation_id=invalid_id),
            "exactly match ordered",
        )

    invalid_fields = deepcopy(artifact)
    invalid_fields["hypotheses"][0]["market_directions"][0]["confidence"] = "high"
    expect_rejected(
        lambda: pipeline.validate_scenario_synthesis(invalid_fields, manifest),
        "direction fields",
    )
    not_a_list = deepcopy(artifact)
    not_a_list["hypotheses"][0]["market_directions"] = None
    expect_rejected(
        lambda: pipeline.validate_scenario_synthesis(not_a_list, manifest),
        "must be a list",
    )
    for invalid_direction in (-2, 2, "1", 1.0, None, True, False):
        reject_direction(
            lambda value, invalid_direction=invalid_direction: value["hypotheses"][0]
            ["market_directions"][0].update(direction=invalid_direction),
            "must be integer",
        )

    repair_calls = []
    invalid_artifact = deepcopy(artifact)
    invalid_artifact["hypotheses"][0]["supporting_observation_ids"] = [
        "invented-observation-id"
    ]

    def repair_completion(prompt):
        repair_calls.append(prompt)
        value = invalid_artifact if len(repair_calls) == 1 else artifact
        return "```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"

    repaired = pipeline.repair_scenario_synthesis(
        embedded_report,
        "# Original evidence brief",
        manifest,
        report_id=fixture["report_id"],
        generated_at=fixture["fetched_at"],
        completion_fn=repair_completion,
    )
    check(repaired == artifact, "constrained scenario repair changed the artifact")
    check(len(repair_calls) == 2, "invalid repaired evidence was not retried")
    check(
        "invented-observation-id" in repair_calls[1],
        "repair retry omitted local validation feedback",
    )
    pairing_calls = []
    missing_pair = deepcopy(artifact)
    missing_pair["hypotheses"][0]["market_directions"] = []

    def pairing_completion(prompt):
        pairing_calls.append(prompt)
        value = missing_pair if len(pairing_calls) == 1 else artifact
        return json.dumps(value, ensure_ascii=False)

    pairing_repaired = pipeline.repair_scenario_synthesis(
        embedded_report,
        "# Original evidence brief",
        manifest,
        report_id=fixture["report_id"],
        generated_at=fixture["fetched_at"],
        completion_fn=pairing_completion,
    )
    check(pairing_repaired == artifact, "pairing repair changed valid directions")
    check(len(pairing_calls) == 2, "missing direction pairing was not retried")
    check(
        "exactly match ordered" in pairing_calls[1],
        "pairing repair retry omitted validation feedback",
    )
    market_id = next(
        item["observation_id"]
        for item in manifest["observations"]
        if item["market_derived"]
    )
    market_misplaced = deepcopy(artifact)
    market_misplaced["hypotheses"][0]["supporting_observation_ids"] = [market_id]
    relocated = pipeline.repair_scenario_synthesis(
        embedded_report,
        "# Original evidence brief",
        manifest,
        report_id=fixture["report_id"],
        generated_at=fixture["fetched_at"],
        completion_fn=lambda prompt: json.dumps(market_misplaced),
    )
    check(
        market_id in relocated["hypotheses"][0]["market_observation_ids"]
        and market_id not in relocated["hypotheses"][0]["supporting_observation_ids"],
        "market evidence was not relocated to market_observation_ids",
    )
    markdown = pipeline.render_scenario_markdown(artifact, manifest)
    check(
        artifact["report_id"] == manifest["report_id"]
        and artifact["report_id"] in markdown,
        "Markdown, structured artifact, and manifest report IDs differ",
    )
    for label in ("direction=UP (+1)", "direction=DOWN (-1)", "direction=ABSTAIN (0)"):
        check(label in markdown, f"Markdown omitted {label}")
    preserved = pipeline.append_preserved_simulation(
        markdown,
        (
            "# Full simulation\n\n"
            + ("Narrative context remains available. " * 35)
            + "\n\n## Trading Signals (Structured)\n\n"
            + "```json\n"
            + json.dumps([{
                "asset": "Fixture asset",
                "direction": "conditional",
                "reasoning": "Model-derived implication",
            }])
            + "\n```\n"
        ),
    )
    check(markdown in preserved, "scenario layer was lost during preservation")
    check(
        "Narrative context remains available" in preserved,
        "simulation narrative was discarded",
    )
    check(
        "## Trading Signals (Structured)" in preserved,
        "trading signals were discarded",
    )
    check(
        "model-derived hypotheses" in preserved,
        "preserved trading implications were not qualified",
    )
    simulation_path = temp_dir / "simulation_fixture.md"
    pipeline._atomic_write_text(
        simulation_path,
        embedded_report,
        pipeline._validate_report_text,
    )
    check(
        not simulation_path.name.startswith("prediction_"),
        "raw simulation archive entered legacy prediction discovery",
    )
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

    sourced_percentage = deepcopy(artifact)
    sourced_percentage["hypotheses"][0]["claim"] = (
        "The observed fixture price moved 3.2%."
    )
    check(
        pipeline.validate_scenario_synthesis(sourced_percentage, manifest)
        is sourced_percentage,
        "valid sourced percentage was discarded",
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
    polymarket_selection_fixtures()
    scenario_repair_backend_fixture()
    main_manifest_input_fixture()
    compose_fixture()
    host_default_fixture()
    cp0_dir = emit_cp0_bundle(manifest, artifact, markdown)
    print(
        "PASS: zero-byte, undersized, invalid UTF-8, partial, valid, atomic-write, "
        "stale/current checkpoint, idempotent skip, observation provenance, "
        "granular Polymarket identity/deduplication, aggregate-citation "
        "rejection, signed market directions, abstention, invalid-type "
        "rejection, ordered pairing, MiniMax M3 repair routing, repair retry, "
        "scenario synthesis, schema rejection, and artifact endpoint fixtures"
    )
    print(f"CP0 bundle: {cp0_dir}")


if __name__ == "__main__":
    main()
