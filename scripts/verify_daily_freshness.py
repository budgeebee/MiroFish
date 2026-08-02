#!/usr/bin/env python3
"""Read-only verification for daily MiroFish report and pipeline freshness."""

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
DEFAULT_API_URL = "http://127.0.0.1:5010"
DEFAULT_CRUCIX_LATEST = Path.home() / "Projects/Crucix/runs/latest.json"
REPORT_MIN_BYTES = 1000
SOURCE_MAX_AGE = timedelta(hours=1)
REPORT_ID_PATTERN = re.compile(r"^(prediction_\d{8}_\d{6})(?:_en)?\.md$")


class VerificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def parse_report_id(report_path):
    match = REPORT_ID_PATTERN.match(Path(report_path).name)
    if not match:
        raise VerificationError(f"invalid report filename: {report_path}")
    return match.group(1)


def verify_market_direction_contract(manifest, structured):
    observations = manifest.get("observations")
    hypotheses = structured.get("hypotheses")
    require(isinstance(observations, list), "manifest observations must be a list")
    require(isinstance(hypotheses, list), "structured hypotheses must be a list")
    source_by_id = {}
    for observation in observations:
        require(isinstance(observation, dict), "manifest observation is invalid")
        observation_id = observation.get("observation_id")
        source_id = observation.get("source_id")
        require(
            isinstance(observation_id, str) and observation_id,
            "manifest observation ID is invalid",
        )
        require(observation_id not in source_by_id, "duplicate manifest observation ID")
        source_by_id[observation_id] = source_id
    granular_ids = {
        observation_id
        for observation_id, source_id in source_by_id.items()
        if isinstance(source_id, str)
        and source_id.startswith("Crucix/Polymarket/")
    }
    require(granular_ids, "no granular Polymarket observations")

    directional_pairs = 0
    directional_abstentions = 0
    for hypothesis in hypotheses:
        require(isinstance(hypothesis, dict), "structured hypothesis is invalid")
        hypothesis_id = hypothesis.get("hypothesis_id")
        market_ids = hypothesis.get("market_observation_ids")
        directions = hypothesis.get("market_directions")
        require(
            isinstance(market_ids, list),
            f"{hypothesis_id}: market_observation_ids must be a list",
        )
        require(
            isinstance(directions, list),
            f"{hypothesis_id}: market_directions must be a list",
        )
        directional_ids = [item for item in market_ids if item in granular_ids]
        require(
            len(directional_ids) == len(set(directional_ids)),
            f"{hypothesis_id}: duplicate exact Polymarket evidence",
        )
        paired_ids = []
        for entry in directions:
            require(
                isinstance(entry, dict)
                and set(entry) == {"observation_id", "direction"},
                f"{hypothesis_id}: invalid market direction fields",
            )
            direction = entry["direction"]
            require(
                type(direction) is int and direction in {-1, 0, 1},
                f"{hypothesis_id}: invalid market direction value",
            )
            require(
                entry["observation_id"] in granular_ids,
                f"{hypothesis_id}: direction targets non-granular observation",
            )
            paired_ids.append(entry["observation_id"])
            directional_abstentions += direction == 0
        require(
            paired_ids == directional_ids,
            f"{hypothesis_id}: direction pairs do not match ordered Polymarket evidence",
        )
        directional_pairs += len(directions)
    require(directional_pairs > 0, "no hypothesis/contract direction pairs")
    return {
        "granular_polymarket_observations": len(granular_ids),
        "directional_pairs": directional_pairs,
        "directional_abstentions": directional_abstentions,
    }


def verify_snapshot(*, health, status, summary, manifest, structured, source_path, now):
    expected_date = now.astimezone(ET).strftime("%Y%m%d")
    require(health.get("status") == "ok", "MiroFish health is degraded")
    require(status.get("available") is True, "today's report is unavailable")
    require(status.get("date") == expected_date, "report date is not current")
    require(status.get("pipeline_running") is False, "pipeline is still running")
    require(
        health.get("process", {}).get("running") is False,
        "health reports an active pipeline",
    )
    require(
        health.get("checkpoint", {}).get("current") is False,
        "a current incomplete checkpoint remains",
    )

    report_size = status.get("size_bytes")
    require(
        isinstance(report_size, int) and report_size >= REPORT_MIN_BYTES,
        "report is missing or undersized",
    )
    require(summary.get("available") is True, "legacy report endpoint is unavailable")
    report_id = parse_report_id(status.get("path", ""))
    require(
        parse_report_id(summary.get("path", "")) == report_id,
        "status and legacy endpoint select different reports",
    )
    require(
        manifest.get("schema_version") == "observation-manifest.v1",
        "manifest schema is invalid",
    )
    require(
        structured.get("schema_version") == "scenario-synthesis.v1",
        "structured schema is invalid",
    )
    require(
        manifest.get("report_id") == structured.get("report_id") == report_id,
        "Markdown, manifest, and structured report IDs differ",
    )
    direction_counts = verify_market_direction_contract(manifest, structured)

    source_path = Path(source_path)
    require(source_path.is_file(), f"Crucix source is missing: {source_path}")
    source_age = now.astimezone(timezone.utc) - datetime.fromtimestamp(
        source_path.stat().st_mtime,
        tz=timezone.utc,
    )
    require(source_age >= timedelta(0), "Crucix source timestamp is in the future")
    require(
        source_age <= SOURCE_MAX_AGE,
        f"Crucix source is stale ({source_age.total_seconds():.0f}s)",
    )
    return {
        "status": "ok",
        "report_id": report_id,
        "report_date": expected_date,
        "report_size_bytes": report_size,
        "source_age_seconds": int(source_age.total_seconds()),
        "pipeline_running": False,
        "checkpoint_current": False,
        **direction_counts,
    }


def fetch_json(base_url, path):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise VerificationError(
            f"{path} returned HTTP {error.code}: {detail}"
        ) from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError(f"{path} is unreadable: {error}") from error


def verify_live(api_url, source_path):
    now = datetime.now(timezone.utc)
    return verify_snapshot(
        health=fetch_json(api_url, "/health"),
        status=fetch_json(api_url, "/status"),
        summary=fetch_json(api_url, "/output/today"),
        manifest=fetch_json(api_url, "/output/today/manifest"),
        structured=fetch_json(api_url, "/output/today/structured"),
        source_path=source_path,
        now=now,
    )


def verify_fixture():
    now = datetime(2026, 7, 27, 4, 10, tzinfo=timezone.utc)
    report_id = "prediction_20260727_000500"
    with tempfile.TemporaryDirectory() as temp_name:
        source_path = Path(temp_name) / "latest.json"
        source_path.write_text("{}\n", encoding="utf-8")
        timestamp = (now - timedelta(minutes=10)).timestamp()
        os.utime(source_path, (timestamp, timestamp))
        health = {
            "status": "ok",
            "process": {"running": False},
            "checkpoint": {"current": False},
        }
        status = {
            "available": True,
            "date": "20260727",
            "path": f"/data/output/{report_id}.md",
            "size_bytes": 4096,
            "pipeline_running": False,
        }
        summary = {
            "available": True,
            "path": f"/data/output/{report_id}.md",
        }
        manifest = {
            "schema_version": "observation-manifest.v1",
            "report_id": report_id,
            "observations": [
                {
                    "observation_id": "obs-fixture-contract",
                    "source_id": "Crucix/Polymarket/fixture-contract",
                },
                {
                    "observation_id": "obs-fixture-shift-contract",
                    "source_id": "Crucix/Polymarket/fixture-shift-contract",
                },
                {
                    "observation_id": "obs-polymarket-aggregate",
                    "source_id": "Crucix/Polymarket",
                },
            ],
        }
        structured = {
            "schema_version": "scenario-synthesis.v1",
            "report_id": report_id,
            "hypotheses": [
                {
                    "hypothesis_id": "fixture-directional",
                    "market_observation_ids": [
                        "obs-fixture-contract",
                        "obs-fixture-shift-contract",
                    ],
                    "market_directions": [
                        {
                            "observation_id": "obs-fixture-contract",
                            "direction": 1,
                        },
                        {
                            "observation_id": "obs-fixture-shift-contract",
                            "direction": 0,
                        },
                    ],
                }
            ],
        }
        result = verify_snapshot(
            health=health,
            status=status,
            summary=summary,
            manifest=manifest,
            structured=structured,
            source_path=source_path,
            now=now,
        )
        require(result["report_id"] == report_id, "valid fixture changed report ID")
        require(
            result["granular_polymarket_observations"] == 2
            and result["directional_pairs"] == 2
            and result["directional_abstentions"] == 1,
            "valid fixture returned incorrect direction counts",
        )

        mismatched = dict(structured, report_id="prediction_20260727_999999")
        try:
            verify_snapshot(
                health=health,
                status=status,
                summary=summary,
                manifest=manifest,
                structured=mismatched,
                source_path=source_path,
                now=now,
            )
        except VerificationError as error:
            require("report IDs differ" in str(error), "wrong mismatch rejection")
        else:
            raise VerificationError("mismatched report IDs were accepted")
        incomplete = json.loads(json.dumps(structured))
        incomplete["hypotheses"][0]["market_directions"].pop()
        try:
            verify_market_direction_contract(manifest, incomplete)
        except VerificationError as error:
            require("ordered Polymarket evidence" in str(error), "wrong pairing rejection")
        else:
            raise VerificationError("incomplete market direction pairing was accepted")
    return {**result, "fixture": True}


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument(
        "--api-url",
        default=os.getenv("MIROFISH_API_URL", DEFAULT_API_URL),
    )
    parser.add_argument(
        "--crucix-latest",
        type=Path,
        default=Path(os.getenv("CRUCIX_LATEST", DEFAULT_CRUCIX_LATEST)),
    )
    args = parser.parse_args()
    try:
        result = (
            verify_fixture()
            if args.fixture
            else verify_live(args.api_url, args.crucix_latest)
        )
    except VerificationError as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
