#!/usr/bin/env python3
"""
crucix_to_mirofish.py

Reads Crucix OSINT latest.json, converts it to a markdown intelligence brief,
and drives a full MiroFish simulation pipeline via the REST API.

Usage:
    python3 scripts/crucix_to_mirofish.py                    # defaults
    python3 scripts/crucix_to_mirofish.py --max-rounds 20    # fewer rounds
    python3 scripts/crucix_to_mirofish.py --dry-run           # just generate the markdown, don't run

Environment:
    MIROFISH_URL        MiroFish graph/simulation API (host default: http://localhost:5005)
    CRUCIX_LATEST       Path to latest.json (host default: ~/Projects/Crucix/runs/latest.json)
    SCHWALPACA_URL      Schwalpaca API (host default: http://localhost:8855)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Module-level Session — connection pooling, keep-alive, thread-safe.
# Saves ~1-2s per MiroFish pipeline run vs opening a new TCP+TLS handshake per call.
_session = requests.Session()

# Runtime settings are defined once. Defaults preserve direct host execution;
# docker-compose supplies explicit container paths and network URLs.
ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECTS_DIR = Path.home() / "Projects"
MIROFISH_DIR = Path(os.getenv("MIROFISH_DIR", str(PROJECT_ROOT)))
OUTPUT_DIR = Path(os.getenv("MIROFISH_OUTPUT_DIR", str(MIROFISH_DIR / "output")))
MIROFISH_URL = os.getenv("MIROFISH_URL", "http://localhost:5005")
CRUCIX_LATEST = os.getenv(
    "CRUCIX_LATEST",
    str(PROJECTS_DIR / "Crucix/runs/latest.json"),
)
CRUCIX_URL = os.getenv("CRUCIX_URL", "http://localhost:3117")
SCHWALPACA_URL = os.getenv("SCHWALPACA_URL", "http://localhost:8855")
INSIDER_CAPITOL_URL = os.getenv("INSIDER_CAPITOL_URL", "http://localhost:9700")
NEWS_AGGREGATOR_URL = os.getenv("NEWS_AGGREGATOR_URL", "http://localhost:8000")
SCHWALPACA_JOURNAL = Path(os.getenv(
    "SCHWALPACA_JOURNAL",
    str(PROJECTS_DIR / "schwalpaca/journal/trading-journal.json"),
))
KALSHI_JOURNAL = Path(os.getenv(
    "KALSHI_JOURNAL",
    str(PROJECTS_DIR / "kalshimarket/journal/kalshi-trading-journal.json"),
))
LLAMA_SWAP_URL = os.getenv("LLAMA_SWAP_URL", "http://localhost:8090")
LLM_BOOST_BASE_URL = os.getenv("LLM_BOOST_BASE_URL", "https://api.moonshot.ai/v1")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.minimax.io/v1")
AI_BACKEND_URL = os.getenv("AI_BACKEND_URL", "http://localhost:9400")
SCHWALPACA_API_KEY = os.getenv("SCHWALPACA_API_KEY", "")
REPORT_MIN_BYTES = 1000
OBSERVATION_STATUSES = {"ok", "degraded", "unavailable", "error", "timeout"}
SOURCE_TYPES = {"market", "osint", "news", "macro", "portfolio", "model"}
EPISTEMIC_STATUSES = {"observed", "inferred", "unknown"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
OBSERVATION_FIELDS = {
    "schema_version",
    "observation_id",
    "source_id",
    "source_type",
    "independence_group",
    "observed_at",
    "fetched_at",
    "as_of",
    "staleness_seconds",
    "content_sha256",
    "market_derived",
    "status",
    "payload_ref",
}
HYPOTHESIS_FIELDS = {
    "hypothesis_id",
    "claim",
    "horizon",
    "epistemic_status",
    "confidence",
    "supporting_observation_ids",
    "contradicting_observation_ids",
    "unknowns",
    "falsifiers",
    "watch_conditions",
    "affected_entities",
    "market_observation_ids",
    "market_directions",
}


def _now():
    return datetime.now(ET)


def _validate_report_text(text: str):
    size = len(text.encode("utf-8"))
    if not text.strip():
        raise ValueError("report is empty")
    if size < REPORT_MIN_BYTES:
        raise ValueError(
            f"report is undersized ({size} bytes; minimum {REPORT_MIN_BYTES})"
        )


def _atomic_write_text(path: Path, text: str, validator=None):
    """Validate and atomically publish text through a temporary sibling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        published_text = temp_path.read_text(encoding="utf-8")
        if published_text != text:
            raise OSError("temporary sibling content did not match requested artifact")
        if validator:
            validator(published_text)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, value, validator=None):
    """Atomically publish JSON after parsing and schema validation."""
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"

    def validate_text(published_text):
        parsed = json.loads(published_text)
        if validator:
            validator(parsed)

    _atomic_write_text(path, text, validate_text)


def _is_valid_report(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
        _validate_report_text(text)
        return True
    except (OSError, UnicodeDecodeError, ValueError):
        return False


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256(value) -> str:
    if not isinstance(value, str):
        value = _canonical_json(value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid timestamp: {value!r}")
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp lacks timezone: {value}")
    return parsed


def _iso_or_none(value):
    if value is None:
        return None
    return _parse_timestamp(value).isoformat().replace("+00:00", "Z")


def _latest_market_observed_at(payload):
    candidates = []
    for key in ("markets", "top", "signals"):
        for item in payload.get(key, []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            observed = item.get("observedAt") or item.get("observed_at")
            if observed:
                try:
                    candidates.append(_parse_timestamp(observed))
                except ValueError:
                    continue
    if not candidates:
        return None
    return max(candidates).isoformat().replace("+00:00", "Z")


def _selected_polymarket_markets(payload):
    """Return the market rows shown in the brief, deduplicated in display order."""
    if not isinstance(payload, dict):
        return []
    selected = []
    seen_contract_ids = set()
    for collection, limit in (("top", 10), ("highProbShifts", 5)):
        rows = payload.get(collection) or []
        if not isinstance(rows, list):
            raise ValueError(f"Polymarket {collection} must be a list")
        for index, market in enumerate(rows[:limit]):
            if not isinstance(market, dict):
                raise ValueError(f"Polymarket {collection}[{index}] must be an object")
            contract_id = market.get("venueContractId")
            if not isinstance(contract_id, str) or not contract_id.strip():
                raise ValueError(
                    f"Polymarket {collection}[{index}] is missing venueContractId"
                )
            contract_id = contract_id.strip()
            if contract_id in seen_contract_ids:
                continue
            seen_contract_ids.add(contract_id)
            selected.append((
                contract_id,
                market,
                f"/sources/Polymarket/{collection}/{index}",
            ))
    return selected


def _polymarket_observation_labels(crucix_data):
    """Build prompt-only question labels keyed by granular Polymarket source ID."""
    payload = crucix_data.get("sources", {}).get("Polymarket")
    labels = {}
    for contract_id, market, _ in _selected_polymarket_markets(payload):
        question = market.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(
                f"Polymarket contract {contract_id} is missing question"
            )
        question = " ".join(question.split())
        end_date = market.get("endDate")
        end_note = f" | ends={end_date}" if end_date else ""
        labels[f"Crucix/Polymarket/{contract_id}"] = (
            f"question={question} | direction target=YES outcome{end_note}"
        )
    return labels


def _observation(
    source_id,
    payload,
    *,
    source_type,
    independence_group,
    market_derived,
    status,
    fetched_at,
    payload_ref,
    observed_at=None,
    as_of=None,
):
    fetched = _parse_timestamp(fetched_at)
    observed = _parse_timestamp(observed_at) if observed_at else None
    as_of_value = _parse_timestamp(as_of) if as_of else None
    payload_sha = _sha256(payload)
    observation_key = f"{source_id}\0{payload_sha}"
    observation_id = f"obs-{_sha256(observation_key)[:20]}"
    staleness = (
        max(0, int((fetched - observed).total_seconds()))
        if observed
        else None
    )
    return {
        "schema_version": "observation.v1",
        "observation_id": observation_id,
        "source_id": source_id,
        "source_type": source_type,
        "independence_group": independence_group,
        "observed_at": _iso_or_none(observed_at),
        "fetched_at": fetched.isoformat().replace("+00:00", "Z"),
        "as_of": (
            as_of_value.isoformat().replace("+00:00", "Z")
            if as_of_value
            else None
        ),
        "staleness_seconds": staleness,
        "content_sha256": payload_sha,
        "market_derived": bool(market_derived),
        "status": status,
        "payload_ref": payload_ref,
    }


def build_observation_manifest(
    crucix_data: dict,
    supplements: dict | None = None,
    *,
    report_id: str,
    fetched_at: str | None = None,
):
    """Convert Crucix health and supplemental inputs to observation.v1."""
    health_by_source = crucix_data.get("sourceHealth", {})
    sources = crucix_data.get("sources", {})
    crucix_fetched_at = (
        fetched_at
        or crucix_data.get("crucix", {}).get("timestamp")
        or _now().isoformat()
    )
    observations = []

    for name in sorted(health_by_source):
        health = health_by_source[name] or {}
        payload = sources.get(name)
        status = health.get("status", "error")
        if name == "Adanos" and isinstance(payload, dict):
            section_provenance = payload.get("sectionProvenance", {})
            emitted_section = False
            for section in sorted(section_provenance):
                if section not in payload:
                    continue
                provenance = section_provenance[section] or {}
                section_payload = payload.get(section)
                observations.append(_observation(
                    f"Crucix/Adanos/{section}",
                    section_payload,
                    source_type=provenance.get("sourceType", "osint"),
                    independence_group=provenance.get(
                        "independenceGroup",
                        f"adanos-{section}",
                    ),
                    market_derived=provenance.get("marketDerived", False),
                    status=status,
                    fetched_at=payload.get("timestamp") or crucix_fetched_at,
                    payload_ref=f"/sources/Adanos/{section}",
                    observed_at=(
                        _latest_market_observed_at({"markets": section_payload})
                        if provenance.get("marketDerived")
                        else None
                    ),
                ))
                emitted_section = True
            if emitted_section:
                continue

        if name == "Polymarket" and isinstance(payload, dict):
            for contract_id, market, payload_ref in _selected_polymarket_markets(
                payload
            ):
                observations.append(_observation(
                    f"Crucix/Polymarket/{contract_id}",
                    market,
                    source_type="market",
                    independence_group=(
                        market.get("independenceGroup")
                        or payload.get("independenceGroup")
                        or health.get("independenceGroup")
                        or "polymarket"
                    ),
                    market_derived=True,
                    status=status,
                    fetched_at=(
                        market.get("fetchedAt")
                        or payload.get("fetchedAt")
                        or payload.get("timestamp")
                        or crucix_fetched_at
                    ),
                    payload_ref=payload_ref,
                    observed_at=(
                        market.get("observedAt") or market.get("observed_at")
                    ),
                ))

        source_type = health.get("sourceType") or (
            payload.get("sourceType") if isinstance(payload, dict) else None
        ) or "osint"
        independence_group = health.get("independenceGroup") or (
            payload.get("independenceGroup") if isinstance(payload, dict) else None
        ) or name.lower()
        market_derived = health.get("marketDerived", False) or (
            payload.get("marketDerived", False)
            if isinstance(payload, dict)
            else False
        )
        observed_at = (
            _latest_market_observed_at(payload)
            if market_derived and isinstance(payload, dict)
            else None
        )
        source_fetched_at = (
            payload.get("fetchedAt") or payload.get("timestamp")
            if isinstance(payload, dict)
            else None
        ) or crucix_fetched_at
        payload_for_hash = payload if name in sources else health
        observations.append(_observation(
            f"Crucix/{name}",
            payload_for_hash,
            source_type=source_type,
            independence_group=independence_group,
            market_derived=market_derived,
            status=status,
            fetched_at=source_fetched_at,
            payload_ref=(
                f"/sources/{name}"
                if name in sources
                else f"/sourceHealth/{name}"
            ),
            observed_at=observed_at,
            as_of=(
                payload.get("asOf") or payload.get("as_of")
                if isinstance(payload, dict)
                else None
            ),
        ))

    for source_id in sorted(supplements or {}):
        item = supplements[source_id]
        payload = item.get("payload")
        observations.append(_observation(
            source_id,
            payload,
            source_type=item["source_type"],
            independence_group=item["independence_group"],
            market_derived=item.get("market_derived", False),
            status=item.get("status", "ok"),
            fetched_at=item.get("fetched_at") or crucix_fetched_at,
            payload_ref=item.get("payload_ref", f"/supplements/{source_id}"),
            observed_at=item.get("observed_at"),
            as_of=item.get("as_of"),
        ))

    manifest = {
        "schema_version": "observation-manifest.v1",
        "report_id": report_id,
        "generated_at": _iso_or_none(crucix_fetched_at),
        "observations": observations,
    }
    validate_observation_manifest(manifest)
    return manifest


def validate_observation_manifest(manifest):
    if set(manifest) != {
        "schema_version",
        "report_id",
        "generated_at",
        "observations",
    }:
        raise ValueError("manifest fields do not match observation-manifest.v1")
    if manifest.get("schema_version") != "observation-manifest.v1":
        raise ValueError("unknown manifest schema")
    if not isinstance(manifest.get("report_id"), str) or not manifest["report_id"]:
        raise ValueError("manifest report ID is missing")
    _parse_timestamp(manifest.get("generated_at"))
    if not isinstance(manifest.get("observations"), list):
        raise ValueError("manifest observations must be a list")
    ids = set()
    for observation in manifest.get("observations", []):
        if set(observation) != OBSERVATION_FIELDS:
            raise ValueError("observation fields do not match observation.v1")
        observation_id = observation.get("observation_id")
        if not observation_id or observation_id in ids:
            raise ValueError(f"duplicate or missing observation ID: {observation_id}")
        ids.add(observation_id)
        if observation.get("schema_version") != "observation.v1":
            raise ValueError(f"{observation_id}: unknown observation schema")
        if observation.get("status") not in OBSERVATION_STATUSES:
            raise ValueError(f"{observation_id}: invalid status")
        if observation.get("source_type") not in SOURCE_TYPES:
            raise ValueError(f"{observation_id}: invalid source type")
        for field in ("source_id", "independence_group", "payload_ref"):
            if not isinstance(observation.get(field), str) or not observation[field]:
                raise ValueError(f"{observation_id}: missing {field}")
        staleness = observation.get("staleness_seconds")
        if staleness is not None and (
            not isinstance(staleness, int) or staleness < 0
        ):
            raise ValueError(f"{observation_id}: invalid staleness")
        for field in ("fetched_at", "observed_at", "as_of"):
            if observation.get(field) is not None:
                _parse_timestamp(observation[field])
        content_sha = observation.get("content_sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha):
            raise ValueError(f"{observation_id}: invalid content hash")
        if (
            observation.get("source_type") == "market"
            or observation.get("market_derived")
        ) and (
            observation.get("source_type") != "market"
            or not observation.get("market_derived")
            or not observation.get("independence_group")
        ):
            raise ValueError(f"{observation_id}: market provenance is incomplete")
    return manifest


def build_scenario_synthesis(manifest, hypotheses, *, generated_at=None):
    validate_observation_manifest(manifest)
    by_source = {
        observation["source_id"]: observation["observation_id"]
        for observation in manifest["observations"]
    }
    market_ids = {
        observation["observation_id"]
        for observation in manifest["observations"]
        if observation["market_derived"]
    }

    def resolve(draft, id_field, source_field):
        explicit = list(draft.get(id_field, []))
        for source_id in draft.get(source_field, []):
            if source_id not in by_source:
                raise ValueError(f"unknown evidence source: {source_id}")
            explicit.append(by_source[source_id])
        return explicit

    def resolve_market_directions(draft):
        resolved = []
        for entry in draft.get("market_directions", []):
            if not isinstance(entry, dict):
                resolved.append(entry)
                continue
            normalized = dict(entry)
            if "source_id" in normalized:
                source_id = normalized.pop("source_id")
                if source_id not in by_source:
                    raise ValueError(f"unknown direction source: {source_id}")
                normalized["observation_id"] = by_source[source_id]
            resolved.append(normalized)
        return resolved

    rendered = []
    for draft in hypotheses:
        rendered.append({
            "hypothesis_id": draft["hypothesis_id"],
            "claim": draft["claim"],
            "horizon": draft.get("horizon", {"start": None, "end": None}),
            "epistemic_status": draft["epistemic_status"],
            "confidence": draft["confidence"],
            "supporting_observation_ids": resolve(
                draft,
                "supporting_observation_ids",
                "supporting_source_ids",
            ),
            "contradicting_observation_ids": resolve(
                draft,
                "contradicting_observation_ids",
                "contradicting_source_ids",
            ),
            "unknowns": list(draft.get("unknowns", [])),
            "falsifiers": list(draft.get("falsifiers", [])),
            "watch_conditions": list(draft.get("watch_conditions", [])),
            "affected_entities": list(draft.get("affected_entities", [])),
            "market_observation_ids": resolve(
                draft,
                "market_observation_ids",
                "market_source_ids",
            ),
            "market_directions": resolve_market_directions(draft),
        })
    artifact = {
        "schema_version": "scenario-synthesis.v1",
        "report_id": manifest["report_id"],
        "generated_at": _iso_or_none(
            generated_at or manifest["generated_at"]
        ),
        "hypotheses": rendered,
    }
    validate_scenario_synthesis(artifact, manifest, market_ids=market_ids)
    return artifact


def validate_scenario_synthesis(artifact, manifest, market_ids=None):
    if set(artifact) != {
        "schema_version",
        "report_id",
        "generated_at",
        "hypotheses",
    }:
        raise ValueError("scenario fields do not match scenario-synthesis.v1")
    if artifact.get("schema_version") != "scenario-synthesis.v1":
        raise ValueError("unknown scenario schema")
    if artifact.get("report_id") != manifest.get("report_id"):
        raise ValueError("scenario and manifest report IDs differ")
    _parse_timestamp(artifact.get("generated_at"))
    if not isinstance(artifact.get("hypotheses"), list):
        raise ValueError("scenario hypotheses must be a list")
    observation_ids = {
        observation["observation_id"]
        for observation in manifest.get("observations", [])
    }
    market_ids = market_ids if market_ids is not None else {
        observation["observation_id"]
        for observation in manifest.get("observations", [])
        if observation.get("market_derived")
    }
    granular_polymarket_ids = {
        observation["observation_id"]
        for observation in manifest.get("observations", [])
        if observation.get("source_id", "").startswith("Crucix/Polymarket/")
    }
    aggregate_polymarket_ids = {
        observation["observation_id"]
        for observation in manifest.get("observations", [])
        if observation.get("source_id") == "Crucix/Polymarket"
    }
    hypothesis_ids = set()
    for hypothesis in artifact.get("hypotheses", []):
        field_difference = set(hypothesis) ^ HYPOTHESIS_FIELDS
        if field_difference:
            raise ValueError(
                f"{hypothesis.get('hypothesis_id')}: hypothesis fields differ "
                f"{sorted(field_difference)}"
            )
        hypothesis_id = hypothesis.get("hypothesis_id")
        if not hypothesis_id or hypothesis_id in hypothesis_ids:
            raise ValueError(f"duplicate or missing hypothesis ID: {hypothesis_id}")
        hypothesis_ids.add(hypothesis_id)
        if hypothesis.get("epistemic_status") not in EPISTEMIC_STATUSES:
            raise ValueError(f"{hypothesis_id}: invalid epistemic status")
        if hypothesis.get("confidence") not in CONFIDENCE_LEVELS:
            raise ValueError(f"{hypothesis_id}: invalid confidence")
        horizon = hypothesis.get("horizon", {})
        if set(horizon) != {"start", "end"}:
            raise ValueError(f"{hypothesis_id}: invalid horizon fields")
        for boundary in ("start", "end"):
            if horizon.get(boundary) is not None:
                _parse_timestamp(horizon[boundary])
        for field in (
            "supporting_observation_ids",
            "contradicting_observation_ids",
            "market_observation_ids",
        ):
            dangling = set(hypothesis.get(field, [])) - observation_ids
            if dangling:
                raise ValueError(f"{hypothesis_id}: dangling evidence IDs {dangling}")
        non_market_evidence = (
            set(hypothesis.get("supporting_observation_ids", []))
            | set(hypothesis.get("contradicting_observation_ids", []))
        )
        if non_market_evidence & market_ids:
            raise ValueError(
                f"{hypothesis_id}: market observations must use market evidence"
            )
        if not set(hypothesis.get("market_observation_ids", [])) <= market_ids:
            raise ValueError(f"{hypothesis_id}: non-market ID in market evidence")
        if (
            granular_polymarket_ids
            and set(hypothesis.get("market_observation_ids", []))
            & aggregate_polymarket_ids
        ):
            raise ValueError(
                f"{hypothesis_id}: aggregate Polymarket observation is "
                "provenance-only when exact contracts exist"
            )
        market_observation_ids = hypothesis.get("market_observation_ids", [])
        directional_ids = [
            observation_id
            for observation_id in market_observation_ids
            if observation_id in granular_polymarket_ids
        ]
        if len(directional_ids) != len(set(directional_ids)):
            raise ValueError(
                f"{hypothesis_id}: duplicate exact Polymarket market evidence"
            )
        market_directions = hypothesis.get("market_directions")
        if not isinstance(market_directions, list):
            raise ValueError(f"{hypothesis_id}: market_directions must be a list")
        direction_ids = []
        for entry in market_directions:
            if not isinstance(entry, dict) or set(entry) != {
                "observation_id",
                "direction",
            }:
                raise ValueError(
                    f"{hypothesis_id}: invalid market direction fields"
                )
            direction = entry["direction"]
            if type(direction) is not int or direction not in {-1, 0, 1}:
                raise ValueError(
                    f"{hypothesis_id}: market direction must be integer -1, 0, or 1"
                )
            direction_ids.append(entry["observation_id"])
        if direction_ids != directional_ids:
            raise ValueError(
                f"{hypothesis_id}: market directions must exactly match ordered "
                "direct Polymarket evidence"
            )
        if not isinstance(hypothesis.get("claim"), str) or not hypothesis["claim"]:
            raise ValueError(f"{hypothesis_id}: missing claim")
        for field in (
            "supporting_observation_ids",
            "contradicting_observation_ids",
            "unknowns",
            "falsifiers",
            "watch_conditions",
            "affected_entities",
            "market_observation_ids",
        ):
            if not isinstance(hypothesis.get(field), list):
                raise ValueError(f"{hypothesis_id}: {field} must be a list")
    return artifact


def render_observation_reference_index(manifest, source_labels=None) -> str:
    """Give the live report generator resolvable evidence IDs, not raw authority."""
    validate_observation_manifest(manifest)
    lines = [
        "## Observation Reference Index",
        "",
        "Use these IDs exactly in the machine-readable scenario block.",
        "Market observations belong only in `market_observation_ids`; they are "
        "not independent confirmation.",
        "",
    ]
    source_labels = source_labels or {}
    has_granular_polymarket = any(
        item.get("source_id", "").startswith("Crucix/Polymarket/")
        for item in manifest["observations"]
    )
    for observation in manifest["observations"]:
        source_id = observation["source_id"]
        suffix = ""
        if source_id.startswith("Crucix/Polymarket/"):
            label = source_labels.get(source_id)
            if not label:
                raise ValueError(f"missing prompt label for {source_id}")
            suffix = f" | {label}"
        elif source_id == "Crucix/Polymarket" and has_granular_polymarket:
            suffix = " | scope=aggregate-provenance-only; do not cite"
        lines.append(
            f"- `{observation['observation_id']}` | "
            f"{source_id} | status={observation['status']} | "
            f"type={observation['source_type']} | "
            f"market_derived={str(observation['market_derived']).lower()} | "
            f"independence_group={observation['independence_group']}"
            f"{suffix}"
        )
    lines.append("")
    return "\n".join(lines)


def extract_scenario_synthesis(
    report_text,
    manifest,
    *,
    report_id,
    generated_at,
):
    """Extract and validate the one machine-readable block from a live report."""
    marker = "## Scenario Synthesis (Machine-Readable)"
    marker_index = report_text.find(marker)
    if marker_index < 0:
        raise ValueError("live report omitted the scenario synthesis marker")
    block = report_text[marker_index + len(marker):]
    match = re.search(
        r"```json\s*(\{.*?\})\s*```",
        block,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise ValueError("live report omitted the scenario synthesis JSON block")
    try:
        artifact = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ValueError(f"live scenario JSON is invalid: {error}") from error
    artifact["schema_version"] = "scenario-synthesis.v1"
    artifact["report_id"] = report_id
    artifact["generated_at"] = _iso_or_none(generated_at)
    validate_scenario_synthesis(artifact, manifest)
    return artifact


def _parse_json_object(text):
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        raise ValueError("scenario repair returned no content")
    start = text.find("{")
    if start < 0:
        raise ValueError("scenario repair returned no JSON object")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as error:
        raise ValueError(f"scenario repair JSON is invalid: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("scenario repair did not return an object")
    return value


def _scenario_repair_prompt(report_text, brief_text, manifest, feedback=""):
    allowed = [
        {
            "observation_id": item["observation_id"],
            "source_id": item["source_id"],
            "source_type": item["source_type"],
            "market_derived": item["market_derived"],
            "status": item["status"],
            "independence_group": item["independence_group"],
        }
        for item in manifest["observations"]
    ]
    retry_note = (
        f"\nThe previous candidate failed local validation: {feedback}\n"
        if feedback
        else ""
    )
    reference_marker = "## Observation Reference Index"
    reference_index = ""
    marker_index = brief_text.find(reference_marker)
    if marker_index >= 0:
        reference_index = brief_text[marker_index:].strip()
        brief_text = brief_text[:marker_index].rstrip()
    if len(brief_text) > 8000:
        brief_text = (
            brief_text[:5000]
            + "\n\n[...middle of brief omitted...]\n\n"
            + brief_text[-3000:]
        )
    if len(report_text) > 6000:
        report_text = report_text[:6000] + "\n\n[...report truncated...]\n"
    return f"""\
Convert the supplied evidence brief and preserved social-simulation report into
one concise `scenario-synthesis.v1` JSON object. Output JSON only.

Use at most three hypotheses. Preserve useful conditional tradeable implications,
but do not issue trade recommendations or invent outcome probabilities.
Confidence is only low, medium, or high and describes evidence quality.

Every hypothesis must contain exactly:
`hypothesis_id`, `claim`, `horizon` (an object with `start` and `end`, each ISO
timestamp or null), `epistemic_status` (`observed`, `inferred`, or `unknown`),
`confidence`, `supporting_observation_ids`, `contradicting_observation_ids`,
`unknowns`, `falsifiers`, `watch_conditions`, `affected_entities`, and
`market_observation_ids`, and `market_directions`.

Use only the exact allowed observation IDs below. Market-derived IDs may appear
only in `market_observation_ids`; never use market prices as independent
confirmation. Non-market IDs may appear only in supporting or contradicting
lists. If the evidence cannot support a claim, narrow the claim or record the
gap as an unknown. Do not create descriptive replacement IDs.

For direct Polymarket claims, cite only exact
`Crucix/Polymarket/<venueContractId>` observations. When those exact contract
observations exist, `Crucix/Polymarket` is aggregate provenance only and must
not appear in `market_observation_ids`. Use the complete reference index below
to map each exact observation ID to its market question and YES outcome.
For every exact direct-Polymarket ID in `market_observation_ids`, in the same
order, include exactly one `market_directions` object shaped as
one of these valid JSON examples (replace `obs-...` with an allowed ID):
`{{"observation_id":"obs-...","direction":-1}}`,
`{{"observation_id":"obs-...","direction":0}}`, or
`{{"observation_id":"obs-...","direction":1}}`. Direction refers to the
contract's YES outcome: `1` means the claim makes YES more likely, `-1` means
less likely, and `0` is a genuine directional abstention. Use `0` freely when a
relevant cited contract has no supportable sign. Omit unrelated contracts from
`market_observation_ids`; do not use `0` merely to include them. Never add a
direction for aggregate Polymarket or any other market source.

The publisher overwrites `schema_version`, `report_id`, and `generated_at`, but
include those three top-level fields plus `hypotheses`.
{retry_note}
ALLOWED OBSERVATIONS:
{json.dumps(allowed, ensure_ascii=False)}

OBSERVATION REFERENCE INDEX:
{reference_index or "(not supplied)"}

ORIGINAL EVIDENCE BRIEF:
{brief_text}

PRESERVED SIMULATION REPORT:
{report_text}
"""


def _scenario_response_metadata(payload):
    """Describe response shape without retaining prompt or generated content."""
    if not isinstance(payload, dict):
        return {"response_type": type(payload).__name__}
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    reasoning_content = message.get("reasoning_content")
    reasoning_details = message.get("reasoning_details")
    base_resp = payload.get("base_resp")
    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    result_text = result.get("text")
    base_status = {}
    if isinstance(base_resp, dict):
        base_status = {
            "status_code": base_resp.get("status_code"),
            "status_msg": str(base_resp.get("status_msg", ""))[:160],
        }
    return {
        "top_keys": sorted(payload),
        "base_resp": base_status,
        "model": payload.get("model"),
        "choice_count": len(choices) if isinstance(choices, list) else None,
        "finish_reason": choice.get("finish_reason")
        if isinstance(choice, dict)
        else None,
        "message_keys": sorted(message),
        "content_type": type(content).__name__,
        "content_length": len(content) if isinstance(content, str) else None,
        "content_has_object": "{" in content if isinstance(content, str) else False,
        "reasoning_content_length": len(reasoning_content)
        if isinstance(reasoning_content, str)
        else None,
        "reasoning_details_type": type(reasoning_details).__name__,
        "reasoning_details_count": len(reasoning_details)
        if isinstance(reasoning_details, list)
        else None,
        "output_sensitive": payload.get("output_sensitive"),
        "output_sensitive_type": payload.get("output_sensitive_type"),
        "usage_keys": sorted(payload.get("usage", {}))
        if isinstance(payload.get("usage"), dict)
        else [],
        "provider": payload.get("provider"),
        "task": payload.get("task"),
        "result_keys": sorted(result),
        "result_text_type": type(result_text).__name__,
        "result_text_length": len(result_text)
        if isinstance(result_text, str)
        else None,
        "result_text_has_object": "{" in result_text
        if isinstance(result_text, str)
        else False,
    }


def _request_scenario_repair(prompt):
    backends = [
        (
            "ai_backend",
            AI_BACKEND_URL,
            os.getenv("AI_BACKEND_API_KEY"),
            "deepseek-v4-flash",
        ),
    ]
    errors = []
    for label, base_url, key, model in backends:
        try:
            headers = {"Content-Type": "application/json"}
            if key:
                headers["X-API-Key"] = key
            endpoint = f"{base_url.rstrip('/')}/callAI"
            response = _session.post(
                endpoint,
                headers=headers,
                json={
                    "provider": "deepseek",
                    "task": "chat",
                    "prompt": [
                        {
                            "role": "system",
                            "content": (
                                "Return one valid raw JSON object only. Do not "
                                "include markdown fences, introductory text, "
                                "explanations, or hidden chain-of-thought. Do "
                                "not output <think> tags. The entire response "
                                "must begin with { and end with }."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "metadata": {
                        "model": model,
                        "max_tokens": 8192,
                        "response_format": {"type": "json_object"},
                        "thinking": {"type": "disabled"},
                        "no_legacy": True,
                    },
                },
                timeout=300,
            )
            response.raise_for_status()
            payload = response.json()
            metadata = _scenario_response_metadata(payload)
            result = payload.get("result") if isinstance(payload, dict) else None
            content = result.get("text") if isinstance(result, dict) else None
            if not isinstance(content, str) or not content.strip() or "{" not in content:
                raise ValueError(
                    "scenario repair response has no JSON object; metadata="
                    + json.dumps(metadata, sort_keys=True)
                )
            print(f"  Scenario repair response received from {label} ({model})")
            return content
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else "?"
            detail = (
                error.response.text[:240]
                if error.response is not None
                else type(error).__name__
            )
            errors.append(f"{label}: HTTP {status} {detail}")
        except (
            KeyError,
            TypeError,
            ValueError,
            requests.RequestException,
        ) as error:
            errors.append(
                f"{label}: {type(error).__name__}: {str(error)[:1200]}"
            )
    if not errors:
        raise ValueError("scenario repair has no configured backend")
    raise ValueError("scenario repair backends failed: " + ", ".join(errors))


def repair_scenario_synthesis(
    report_text,
    brief_text,
    manifest,
    *,
    report_id,
    generated_at,
    completion_fn=None,
):
    """Constrain a failed report block to exact local evidence and schema."""
    validate_observation_manifest(manifest)
    completion_fn = completion_fn or _request_scenario_repair
    feedback = ""
    last_error = None
    for _ in range(2):
        prompt = _scenario_repair_prompt(
            report_text,
            brief_text,
            manifest,
            feedback=feedback,
        )
        try:
            artifact = _parse_json_object(completion_fn(prompt))
            artifact["schema_version"] = "scenario-synthesis.v1"
            artifact["report_id"] = report_id
            artifact["generated_at"] = _iso_or_none(generated_at)
            market_ids = {
                item["observation_id"]
                for item in manifest["observations"]
                if item["market_derived"]
            }
            for hypothesis in artifact.get("hypotheses", []):
                misplaced_market_ids = (
                    set(hypothesis.get("supporting_observation_ids", []))
                    | set(hypothesis.get("contradicting_observation_ids", []))
                ) & market_ids
                if misplaced_market_ids:
                    hypothesis["supporting_observation_ids"] = [
                        item
                        for item in hypothesis.get("supporting_observation_ids", [])
                        if item not in market_ids
                    ]
                    hypothesis["contradicting_observation_ids"] = [
                        item
                        for item in hypothesis.get("contradicting_observation_ids", [])
                        if item not in market_ids
                    ]
                    hypothesis["market_observation_ids"] = list(dict.fromkeys(
                        hypothesis.get("market_observation_ids", [])
                        + sorted(misplaced_market_ids)
                    ))
            validate_scenario_synthesis(artifact, manifest)
            return artifact
        except (KeyError, TypeError, ValueError) as error:
            last_error = error
            feedback = str(error)
    raise ValueError(
        f"scenario repair failed local validation: {last_error}"
    ) from last_error


def render_scenario_markdown(artifact, manifest) -> str:
    validate_scenario_synthesis(artifact, manifest)
    hypotheses = artifact["hypotheses"]
    sections = [
        ("What is observed", [
            item for item in hypotheses if item["epistemic_status"] == "observed"
        ]),
        ("What can be inferred", [
            item for item in hypotheses if item["epistemic_status"] == "inferred"
        ]),
    ]
    lines = [
        f"# Evidence-backed scenario brief — {artifact['report_id']}",
        "",
        "> Confidence labels describe evidence quality, not outcome probability.",
        "",
    ]
    for title, items in sections:
        lines.extend([f"## {title}", ""])
        if not items:
            lines.extend(["- None recorded.", ""])
            continue
        for item in items:
            lines.append(
                f"- **{item['hypothesis_id']} [{item['confidence']}]** "
                f"{item['claim']}"
            )
            if item["supporting_observation_ids"]:
                lines.append(
                    "  - Supporting evidence: "
                    + ", ".join(item["supporting_observation_ids"])
                )
            if item["contradicting_observation_ids"]:
                lines.append(
                    "  - Contradicting evidence: "
                    + ", ".join(item["contradicting_observation_ids"])
                )
        lines.append("")

    lines.extend(["## What remains unknown", ""])
    unknown_values = [
        (item["hypothesis_id"], value)
        for item in hypotheses
        for value in item["unknowns"]
    ]
    unknown_values.extend(
        (item["hypothesis_id"], item["claim"])
        for item in hypotheses
        if item["epistemic_status"] == "unknown"
        and item["claim"] not in item["unknowns"]
    )
    lines.extend(
        [f"- **{hypothesis_id}:** {value}" for hypothesis_id, value in unknown_values]
        or ["- None recorded."]
    )
    lines.append("")

    lines.extend(["## Competing scenarios", ""])
    competing = [
        item for item in hypotheses if item["epistemic_status"] == "inferred"
    ]
    for item in competing:
        lines.append(
            f"- **{item['hypothesis_id']} [{item['confidence']}]** "
            f"{item['claim']}"
        )
    if not competing:
        lines.append("- None recorded.")
    lines.append("")

    for title, field in [
        ("Falsifiers", "falsifiers"),
        ("Watch conditions", "watch_conditions"),
    ]:
        lines.extend([f"## {title}", ""])
        values = [
            (item["hypothesis_id"], value)
            for item in hypotheses
            for value in item[field]
        ]
        lines.extend(
            [f"- **{hypothesis_id}:** {value}" for hypothesis_id, value in values]
            or ["- None recorded."]
        )
        lines.append("")

    lines.extend(["## What the crowd currently prices", ""])
    observations_by_id = {
        item["observation_id"]: item
        for item in manifest["observations"]
    }
    market_values = [
        (
            item["hypothesis_id"],
            observations_by_id[observation_id],
            next(
                (
                    direction["direction"]
                    for direction in item["market_directions"]
                    if direction["observation_id"] == observation_id
                ),
                None,
            ),
        )
        for item in hypotheses
        for observation_id in item["market_observation_ids"]
    ]
    lines.extend(
        [
            f"- **{hypothesis_id}:** `{observation['observation_id']}` from "
            f"{observation['source_id']} "
            f"(independence group: `{observation['independence_group']}`)"
            + (
                " | direction="
                + {
                    1: "UP (+1)",
                    -1: "DOWN (-1)",
                    0: "ABSTAIN (0)",
                }[direction]
                if direction is not None
                else ""
            )
            for hypothesis_id, observation, direction in market_values
        ]
        or ["- No market observations recorded."]
    )
    lines.append("")
    return "\n".join(lines)


def append_preserved_simulation(canonical_markdown, simulation_markdown) -> str:
    """Retain the full simulation while distinguishing analysis from evidence."""
    if not simulation_markdown.strip():
        raise ValueError("simulation narrative is empty")
    return (
        canonical_markdown.rstrip()
        + "\n\n"
        + "## Model-derived simulation and tradeable implications\n\n"
        + "> This preserved simulation layer explores conditional market reactions. "
        + "Its narrative and trading signals are model-derived hypotheses—not "
        + "verified observations, outcome probabilities, or trade recommendations.\n\n"
        + simulation_markdown.strip()
        + "\n"
    )


def legacy_consumer_diff():
    return {
        "schema_version": "legacy-consumer-diff.v1",
        "legacy_surfaces": {
            "/output/today": {
                "preserved_fields": [
                    "available",
                    "date",
                    "path",
                    "title",
                    "chapters",
                    "total_chars",
                ],
                "lost_fields": [],
            },
            "/output/today/full": {
                "preserved_fields": ["raw_markdown"],
                "lost_fields": [],
            },
        },
        "new_surfaces": {
            "/output/today/manifest": [
                "schema_version",
                "report_id",
                "generated_at",
                "observations",
            ],
            "/output/today/structured": [
                "schema_version",
                "report_id",
                "generated_at",
                "hypotheses",
            ],
        },
        "deprecated_fields_after_phase_1": [
            "unstructured_prediction_only_interpretation",
        ],
    }


def _parallel_fetch(symbols, fetch_fn, max_workers=10):
    """Run fetch_fn(sym) for each sym in parallel via the shared Session.

    Returns dict of sym → result (or None on failure). Order-independent.
    Used by the N+1 fetch loops — saves 5-8x runtime vs sequential.
    """
    if not symbols:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_fn, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = fut.result()
            except Exception:
                out[sym] = None
    return out


def refresh_adanos():
    """Force a fresh Adanos fetch on Crucix before reading latest.json.

    The 2hr Adanos cache could otherwise feed up to 2hr-stale social sentiment
    into MiroFish's simulation. POST /api/adanos/refresh bypasses the cache,
    merges fresh data into latest.json, and returns the new payload.

    Falls back gracefully if Crucix is unreachable — the on-disk latest.json
    still has whatever the last 15-min sweep cached.
    """
    try:
        r = _session.post(f"{CRUCIX_URL}/api/adanos/refresh", timeout=30)
        if r.ok:
            data = r.json()
            age = data.get("cache_age_seconds", "?")
            key = data.get("key_used", "?")
            poly = len(data.get("polymarket") or [])
            print(f"  Adanos refreshed (key={key}, age={age}s, {poly} polymarket items)")
        else:
            print(f"  Adanos refresh HTTP {r.status_code} — using on-disk cache")
    except requests.exceptions.ConnectionError:
        print(f"  Adanos refresh skipped — Crucix unreachable at {CRUCIX_URL}")
    except Exception as e:
        print(f"  Adanos refresh failed: {e} — using on-disk cache")


def fetch_insider_capitol_signal() -> dict | None:
    """Fetch congressional trading signal from Insider Capitol.

    Returns the daily_signal dict or None if unavailable.
    """
    try:
        r = _session.get(f"{INSIDER_CAPITOL_URL}/signal/daily?days=3", timeout=15)
        if r.ok:
            data = r.json()
            n = data.get("n_new_disclosures", 0)
            trifecta = data.get("n_trifecta_flagged", 0)
            sectors = len(data.get("hot_sectors", []))
            print(f"  Insider Capitol signal: {n} disclosures ({trifecta} trifecta), {sectors} hot sectors")
            return data
        print(f"  Insider Capitol signal HTTP {r.status_code}")
        return None
    except requests.exceptions.ConnectionError:
        print(f"  Insider Capitol signal skipped — unreachable at {INSIDER_CAPITOL_URL}")
        return None
    except Exception as e:
        print(f"  Insider Capitol signal failed: {e}")
        return None


def insider_capitol_to_markdown(signal: dict) -> str:
    """Format Insider Capitol daily signal as a markdown section."""
    if not signal:
        return ""
    lines = []
    n = signal.get("n_new_disclosures", 0)
    trifecta = signal.get("n_trifecta_flagged", 0)
    lines.append(f"**{n} new disclosures** ({trifecta} trifecta-flagged) in the last {signal.get('window_days', 3)} days")
    hot = signal.get("hot_sectors", [])
    if hot:
        lines.append(f"\n**Hot sectors** (by dollar volume):")
        for s in hot:
            vol = s.get("dollar_volume", 0)
            lines.append(f"- {s['sector']}: ${vol:,.0f} ({s['n_purchases']} purchases)")
    alpha = signal.get("proven_alpha_members", [])
    if alpha:
        lines.append(f"\n**Proven alpha members** (beat market significantly):")
        for m in alpha:
            lines.append(f"- {m['member_name']}: mean AR(90)={m['mean_ar_90']:+.2f}% ({m['n_trades']} trades)")
    trifecta = signal.get("n_trifecta_flagged", 0)
    if trifecta:
        lines.append(f"\n**Trifecta-flagged disclosures** ({trifecta}):")
        for t in signal.get("new_disclosures", []):
            if t.get("trifecta_flag"):
                amt = t.get("amount_mid", "")
                amt_str = f" ${amt:,.0f}" if amt else ""
                lines.append(f"- {t['member_name']} ({t['chamber']}): **{t['ticker']}** {t['type']}{amt_str} — score {t.get('insider_score', '?')}")
    return "\n".join(lines) if lines else ""


def _section(title, body):
    if not body or body.strip() == "":
        return ""
    return f"## {title}\n\n{body}\n\n"


_TIMEOUT = 10

SIMULATION_REQUIREMENT = """\
Use the social simulation to examine plausible reactions to the supplied \
observations over the next 24-48 hours. This is evidence and scenario synthesis, \
not an oracle, trading recommendation, or search for an exploitable edge.

Separate:
1. What the supplied observations directly establish.
2. What can be inferred, with supporting and contradicting observation IDs.
3. What remains unknown.
4. At least two genuinely competing conditional scenarios when evidence permits.
5. Concrete falsifiers and watch conditions.
6. What named market observations currently price.
7. Conditional tradeable implications: affected assets or sectors, direction
   under each scenario, the evidence IDs behind it, and what would invalidate it.

Confidence must be low, medium, or high and describes evidence quality only. \
Never invent an outcome probability. Never treat a market observation as \
independent confirmation of a non-market claim. Polymarket observations sharing \
the `polymarket` independence group are one crowd-information family.

The final report MUST end with the exact heading:
## Scenario Synthesis (Machine-Readable)

Under that heading, emit one fenced `json` object with:
`schema_version`, `report_id`, `generated_at`, and `hypotheses`.
Each hypothesis must contain exactly:
`hypothesis_id`, `claim`, `horizon` (`start`, `end`), `epistemic_status`
(`observed`, `inferred`, `unknown`), `confidence` (`low`, `medium`, `high`),
`supporting_observation_ids`, `contradicting_observation_ids`, `unknowns`,
`falsifiers`, `watch_conditions`, `affected_entities`, and
`market_observation_ids`, and `market_directions`.

Use only IDs from the Observation Reference Index. Put market IDs only in
`market_observation_ids`. Use null horizon boundaries when the evidence does not
support a time boundary. The metadata values may be placeholders; the publisher
sets them after validation. For direct Polymarket claims, cite only exact
`Crucix/Polymarket/<venueContractId>` observations from the index. When those
exact observations exist, `Crucix/Polymarket` is aggregate provenance only and
must not appear in `market_observation_ids`.

For every exact direct-Polymarket ID in `market_observation_ids`, emit exactly
one `market_directions` object in the same order, using one of these valid JSON
forms (replace `obs-...` with an ID from the index):
`{"observation_id":"obs-...","direction":-1}`,
`{"observation_id":"obs-...","direction":0}`, or
`{"observation_id":"obs-...","direction":1}`. Direction targets the
contract's YES outcome: `1` means the hypothesis makes YES more likely, `-1`
means less likely, and `0` is a genuine directional abstention. Use `0` freely
when a relevant cited contract has no supportable sign. Omit unrelated
contracts instead of assigning them `0`. Never emit a direction for aggregate
Polymarket, Adanos, equities, options, screeners, or other market sources.\
"""

# ---------------------------------------------------------------------------
# Crucix JSON -> Markdown brief
# ---------------------------------------------------------------------------


def _fmt_change(val, pct=False):
    """Format a numeric change with arrow."""
    if val is None:
        return ""
    sign = "+" if val > 0 else ""
    suffix = "%" if pct else ""
    return f" ({sign}{val:.2f}{suffix})"


def _section(title, body):
    if not body or body.strip() == "":
        return ""
    return f"## {title}\n\n{body}\n\n"


def crucix_to_markdown(data: dict) -> str:
    """Convert a Crucix latest.json into a concise markdown intelligence brief."""
    meta = data.get("crucix", {})
    sources = data.get("sources", {})
    ts = meta.get("timestamp", "unknown")
    ok = meta.get("sourcesOk", 0)
    failed = meta.get("sourcesFailed", 0)
    source_health = data.get("sourceHealth", {})
    total = (
        len(source_health)
        or meta.get("sourcesQueried")
        or (ok + failed)
        or len(sources)
    )

    parts = []
    parts.append(f"# OSINT Intelligence Brief — {ts[:19]}Z\n")
    parts.append(f"*{ok}/{total} sources reporting, {failed} failed*\n\n")

    # --- Market snapshot (YFinance) ---
    yf = sources.get("YFinance", {})
    quotes = yf.get("quotes", {})
    if quotes:
        lines = []
        for sym, q in quotes.items():
            price = q.get("price", "?")
            chg = q.get("change", 0)
            pct = q.get("changePct", 0)
            sign = "+" if chg >= 0 else ""
            lines.append(f"- **{q.get('name', sym)}** ({sym}): ${price:.2f} {sign}{chg:.2f} ({sign}{pct:.2f}%)")
        parts.append(_section("Live Market Snapshot", "\n".join(lines)))

    # --- Macro indicators (FRED) ---
    fred = sources.get("FRED", {})
    indicators = fred.get("indicators", [])
    if indicators:
        lines = []
        for ind in indicators:
            val = ind.get("value")
            if val is None:
                continue
            mom = ind.get("momChange")
            mom_str = _fmt_change(mom) if mom else ""
            lines.append(f"- **{ind['label']}**: {val}{mom_str} (as of {ind.get('date', '?')})")
        signals = fred.get("signals", [])
        if signals:
            lines.append("")
            lines.append("**Signals:**")
            for s in signals:
                lines.append(f"- {s}")
        parts.append(_section("Macro Indicators (FRED)", "\n".join(lines)))

    # --- Energy (EIA) ---
    eia = sources.get("EIA", {})
    oil = eia.get("oilPrices", {})
    if oil:
        lines = []
        for fuel in ["wti", "brent"]:
            info = oil.get(fuel) or {}
            if info.get("value"):
                recent = info.get("recent", [])
                trend = ""
                if len(recent) >= 3:
                    vals = [r["value"] for r in recent[:3]]
                    if vals[0] > vals[1] > vals[2]:
                        trend = " (trending UP)"
                    elif vals[0] < vals[1] < vals[2]:
                        trend = " (trending DOWN)"
                lines.append(f"- **{info.get('label', fuel)}**: ${info['value']:.2f}{trend}")
        gas = eia.get("gasPrice") or {}
        if gas.get("value"):
            lines.append(f"- **Natural Gas**: ${gas['value']:.2f}")
        inv = eia.get("inventories", {})
        crude = inv.get("crudeStocks") or {}
        if crude.get("value"):
            lines.append(f"- **Crude Stocks**: {crude['value']:.1f}M bbl")
        signals = eia.get("signals", [])
        if signals:
            lines.append("")
            for s in signals:
                lines.append(f"- {s}")
        parts.append(_section("Energy Markets (EIA)", "\n".join(lines)))

    # --- Treasury ---
    treasury = sources.get("Treasury", {})
    debt_list = treasury.get("debt", [])
    rates_list = treasury.get("interestRates", [])
    if debt_list or rates_list:
        lines = []
        if debt_list:
            latest = debt_list[0]
            total = float(latest.get("totalDebt", 0))
            lines.append(f"- **Total US Debt**: ${total/1e12:.2f}T (as of {latest.get('date', '?')})")
        if rates_list:
            for r in rates_list[:5]:
                label = r.get("security") or r.get("type", "?")
                lines.append(f"- **{label}**: {r.get('rate', '?')}%")
        signals = treasury.get("signals", [])
        if signals:
            for s in signals:
                lines.append(f"- {s}")
        parts.append(_section("US Treasury", "\n".join(lines)))

    # --- Supply chain (GSCPI) ---
    gscpi = sources.get("GSCPI", {})
    latest_gscpi = gscpi.get("latest", {})
    if latest_gscpi:
        interp = latest_gscpi.get("interpretation", "")
        val = latest_gscpi.get("value", "?")
        trend = gscpi.get("trend", "?")
        parts.append(_section(
            "Global Supply Chain Pressure (GSCPI)",
            f"- **Index**: {val} ({interp}), trend: {trend}"
        ))

    # --- Geopolitical conflicts (ACLED) ---
    acled = sources.get("ACLED", {})
    if not acled.get("error"):
        total_ev = acled.get("totalEvents", 0)
        total_fat = acled.get("totalFatalities", 0)
        by_region = acled.get("byRegion", {})
        lines = [f"- **Total events**: {total_ev}, **Fatalities**: {total_fat}"]
        for region, info in by_region.items():
            lines.append(f"- {region}: {info.get('events', 0)} events, {info.get('fatalities', 0)} fatalities")
        parts.append(_section("Conflict Events (ACLED)", "\n".join(lines)))

    # --- Telegram OSINT ---
    tg = sources.get("Telegram", {})
    urgent = tg.get("urgentPosts", [])
    if urgent:
        lines = []
        for p in urgent[:12]:
            text = p.get("text", "")[:250].replace("\n", " ")
            topic = p.get("topic", "")
            channel = p.get("channel", "")
            tag = f"[{topic}]" if topic else ""
            lines.append(f"- {tag} {text} *(via {channel})*")
        by_topic = tg.get("byTopic", {})
        if by_topic:
            lines.append("")
            topic_counts = []
            for k, v in by_topic.items():
                if isinstance(v, dict):
                    topic_counts.append(f"{k}: {v.get('totalPosts', 0)}")
                else:
                    topic_counts.append(f"{k}: {v}")
            lines.append(f"**Topic distribution** ({tg.get('totalPosts', 0)} total posts): " +
                         ", ".join(topic_counts))
        parts.append(_section("Telegram OSINT Signals", "\n".join(lines)))

    # --- Prediction markets & social sentiment (Adanos) ---
    adanos = sources.get("Adanos", {})
    if adanos.get("status") == "no_key" or adanos.get("error"):
        pass
    elif adanos.get("status") in ("ok", "partial"):
        lines = []
        poly = adanos.get("polymarket", [])
        if poly:
            lines.append(f"**Polymarket trending tickers** ({len(poly)}):")
            for item in poly[:8]:
                ticker = item.get("ticker", "?")
                name = item.get("company_name", "")
                bull = item.get("bullish_pct")
                bear = item.get("bearish_pct")
                sent = item.get("sentiment_score")
                liq = item.get("total_liquidity")
                trades = item.get("trade_count")
                extras = []
                if bull is not None and bear is not None:
                    extras.append(f"bull={bull:.0f}%/bear={bear:.0f}%")
                if sent is not None:
                    extras.append(f"sent={sent:+.2f}")
                if liq is not None:
                    extras.append(f"liq=${liq:,.0f}")
                if trades is not None:
                    extras.append(f"trades={trades}")
                extras_str = f" ({', '.join(extras)})" if extras else ""
                name_str = f" {name}" if name else ""
                lines.append(f"- **{ticker}**{name_str}{extras_str}")
        rd = adanos.get("reddit", [])
        if rd:
            lines.append(f"\n**Reddit trending tickers** ({len(rd)}):")
            for item in rd[:8]:
                ticker = item.get("ticker", "?")
                name = item.get("company_name", "")
                sent = item.get("sentiment_score")
                mentions = item.get("mentions")
                extras = []
                if sent is not None:
                    extras.append(f"sent={sent:+.2f}")
                if mentions is not None:
                    extras.append(f"mentions={mentions}")
                extras_str = f" ({', '.join(extras)})" if extras else ""
                name_str = f" {name}" if name else ""
                lines.append(f"- **{ticker}**{name_str}{extras_str}")
        x = adanos.get("x", [])
        if x:
            lines.append(f"\n**X trending tickers** ({len(x)}):")
            for item in x[:8]:
                ticker = item.get("ticker", "?")
                name = item.get("company_name", "")
                sent = item.get("sentiment_score")
                mentions = item.get("mentions")
                extras = []
                if sent is not None:
                    extras.append(f"sent={sent:+.2f}")
                if mentions is not None:
                    extras.append(f"mentions={mentions}")
                extras_str = f" ({', '.join(extras)})" if extras else ""
                name_str = f" {name}" if name else ""
                lines.append(f"- **{ticker}**{name_str}{extras_str}")
        if lines:
            parts.append(_section("Prediction Markets & Social Sentiment (Adanos)", "\n".join(lines)))

    # --- Earthquakes (USGS) ---
    usgs = sources.get("USGS", {})
    if not usgs.get("error"):
        major = usgs.get("majorEvents", []) or []
        if major:
            lines = []
            for q in major[:8]:
                mag = q.get("mag", 0)
                place = (q.get("place") or "?").split(",")[-1].strip()
                depth = q.get("depthKm", "?")
                tsunami = " [TSUNAMI FLAG]" if q.get("tsunami") else ""
                alert = q.get("alert") or ""
                alert_str = f" [{alert.upper()}]" if alert in ("red", "orange") else ""
                lines.append(f"- **M{mag:.1f}** {place} — depth {depth}km{tsunami}{alert_str}")
            parts.append(_section("Major Earthquakes (USGS, M5.5+)", "\n".join(lines)))

    # --- Natural disasters (GDACS) ---
    gdacs = sources.get("GDACS", {})
    gdacs_events = gdacs.get("events", []) or []
    gdacs_red = [e for e in gdacs_events if e.get("severity") == "Red"]
    gdacs_orange = [e for e in gdacs_events if e.get("severity") == "Orange"]
    if gdacs.get("totalEvents", 0) > 0 and (gdacs_red or gdacs_orange):
        lines = [f"**{gdacs.get('totalEvents', 0)} total disasters tracked** ({len(gdacs_red)} RED, {len(gdacs_orange)} ORANGE)"]
        by_type = gdacs.get("byType") or {}
        if by_type:
            lines.append(f"By type: " + ", ".join(f"{k}={v}" for k, v in by_type.items()))
        for e in gdacs_red[:3] + gdacs_orange[:5]:
            sev = (e.get("severity") or "").upper()
            t = e.get("type", "?")
            title = (e.get("title") or "?")[:120]
            lines.append(f"- [{sev}] {t}: {title}")
        parts.append(_section("Global Disasters (GDACS)", "\n".join(lines)))

    # --- NASA EONET (natural events) ---
    eonet = sources.get("EONET", {})
    eonet_events = eonet.get("events", []) or []
    notable_eonet = [e for e in eonet_events if (e.get("category") or "").lower() in ("volcanoes", "severe storms", "tropical cyclones")]
    if eonet.get("totalOpen", 0) > 0 and notable_eonet:
        lines = [f"**{eonet.get('totalOpen', 0)} open natural events** tracked by NASA EONET"]
        for e in notable_eonet[:6]:
            cat = e.get("category", "?")
            title = (e.get("title") or "?")[:100]
            lines.append(f"- {cat}: {title}")
        parts.append(_section("NASA EONET — Notable Natural Events", "\n".join(lines)))

    # --- Climate anomalies (Open-Meteo) ---
    openmeteo = sources.get("OpenMeteo", {})
    anomalies = openmeteo.get("anomalies", []) or []
    if anomalies:
        lines = []
        for h in anomalies[:5]:
            region = h.get("label") or h.get("key") or "?"
            delta = h.get("tempAnomalyC", 0)
            sev = h.get("tempSeverity", "?")
            base = h.get("baselineTempC", 0)
            recent = h.get("recentTempC", 0)
            sign = "+" if delta >= 0 else ""
            lines.append(f"- **{region}**: {sign}{delta:.1f}°C vs baseline ({sev}) — {base:.1f}°C → {recent:.1f}°C")
        parts.append(_section("Climate Anomalies at Conflict Hotspots (Open-Meteo ERA5)", "\n".join(lines)))

    # --- ECB rates + FX ---
    ecb = sources.get("ECB", {})
    ecb_rates = ecb.get("rates", []) or []
    ecb_ok = [r for r in ecb_rates if r.get("value") is not None and not r.get("error")]
    if ecb_ok:
        lines = []
        for r in ecb_ok:
            chg = r.get("change") or 0
            pct = r.get("changePct") or 0
            sign = "+" if chg >= 0 else ""
            lines.append(f"- **{r.get('label', r.get('key'))}**: {r.get('value'):.4f} ({sign}{chg:.4f}, {sign}{pct:.2f}%) as of {r.get('date', '?')}")
        parts.append(_section("ECB Policy Rates & FX Reference Rates", "\n".join(lines)))

    # --- Polymarket (prediction markets) ---
    polymarket = sources.get("Polymarket", {})
    if polymarket.get("status") == "ok" and polymarket.get("totalRelevant", 0) > 0:
        top = polymarket.get("top", []) or []
        by_cat = polymarket.get("byCategory") or {}
        if top:
            cat_str = ", ".join(f"{k}={v}" for k, v in by_cat.items()) if by_cat else ""
            lines = []
            if cat_str:
                lines.append(f"By category: {cat_str}")
            lines.append("")
            for m in top[:10]:
                q = m.get("question") or "?"
                yes = m.get("yesPrice")
                vol = m.get("volume24hr") or 0
                end = (m.get("endDate") or "")[:10]
                cat = m.get("category") or "?"
                yes_str = f"{yes*100:.0f}%" if yes is not None else "?"
                vol_str = f"${vol/1000:.0f}k 24h vol" if vol else ""
                lines.append(f"- [{cat}] **{yes_str} YES** — {q[:90]} ({vol_str}, ends {end})")
            parts.append(_section("Prediction Markets (Polymarket)", "\n".join(lines)))
        extremes = polymarket.get("highProbShifts") or []
        if extremes:
            lines = ["**Markets at extremes (high signal value for cross-domain confirmation):**"]
            for m in extremes[:5]:
                q = m.get("question") or "?"
                yes = m.get("yesPrice")
                vol = m.get("volume24hr") or 0
                yes_str = f"{yes*100:.0f}%" if yes is not None else "?"
                lines.append(f"- {yes_str} YES — {q[:80]} (vol ${vol/1000:.0f}k)")
            parts.append(_section("Polymarket — Extreme Probabilities", "\n".join(lines)))

    # --- Travel advisories (government risk assessments) ---
    advisories = sources.get("Advisories", {})
    dnt = advisories.get("doNotTravel") or []
    reconsider = advisories.get("reconsider") or []
    if advisories.get("total", 0) > 0 and (dnt or reconsider):
        lines = []
        if dnt:
            lines.append(f"**Do Not Travel ({len(dnt)} countries):** " + ", ".join(a.get("country") for a in dnt[:10]))
        if reconsider:
            lines.append(f"**Reconsider Travel ({len(reconsider)} countries):** " + ", ".join(a.get("country") for a in reconsider[:10]))
        # Most-recent updates with description
        recent = sorted(dnt + reconsider, key=lambda a: a.get("pubDate") or "", reverse=True)[:6]
        for a in recent:
            level = a.get("level", "?")
            country = a.get("country") or "?"
            desc = (a.get("description") or "")[:140]
            lines.append(f"- [{level}] {country}: {desc}")
        parts.append(_section("Government Travel Advisories", "\n".join(lines)))

    # --- Cyber threat IOCs (abuse.ch) ---
    abusech = sources.get("abuse.ch", {})
    if not abusech.get("error"):
        c2_count = abusech.get("c2ServerCount", 0)
        mal_count = abusech.get("malwareHostCount", 0)
        if c2_count or mal_count:
            lines = [f"**{c2_count} active C2 servers**, {mal_count} malware distribution hosts"]
            by_country = abusech.get("c2ByCountry") or {}
            if by_country:
                top = sorted(by_country.items(), key=lambda x: x[1], reverse=True)[:5]
                lines.append(f"Top C2 source countries: " + ", ".join(f"{k}={v}" for k, v in top))
            top_c2 = (abusech.get("c2Servers") or [])[:5]
            for c in top_c2:
                mal = c.get("malware") or "?"
                asn = c.get("asName") or "?"
                lines.append(f"- C2: {c.get('ip')} ({mal}, {asn}, {c.get('country') or '?'})")
            parts.append(_section("Cyber Threat Infrastructure (abuse.ch)", "\n".join(lines)))

    # --- Thermal / fire detections (FIRMS) ---
    firms = sources.get("FIRMS", {})
    hotspots = firms.get("hotspots", [])
    if hotspots:
        lines = []
        for h in hotspots:
            region = h.get("region", "?")
            total = h.get("totalDetections", 0)
            high = h.get("highConfidence", 0)
            night = h.get("nightDetections", 0)
            if total > 0:
                lines.append(f"- **{region}**: {total} detections ({high} high-confidence, {night} at night)")
        signals = firms.get("signals", [])
        if signals:
            for s in signals:
                lines.append(f"- {s}")
        if lines:
            parts.append(_section("Thermal Detections (NASA FIRMS)", "\n".join(lines)))

    # --- Air traffic ---
    opensky = sources.get("OpenSky", {})
    hotspots = opensky.get("hotspots", [])
    active = [h for h in hotspots if h.get("totalAircraft", 0) > 0]
    if active:
        lines = [f"- **{h['region']}**: {h['totalAircraft']} aircraft tracked" for h in active]
        parts.append(_section("Air Traffic Hotspots (OpenSky)", "\n".join(lines)))

    # --- Military aircraft (ADS-B) ---
    adsb = sources.get("ADS-B", {})
    mil = adsb.get("militaryAircraft", [])
    if mil:
        lines = [f"- {a.get('callsign', '?')} ({a.get('type', '?')}) — {a.get('region', '?')}" for a in mil[:10]]
        parts.append(_section("Military Aircraft Activity", "\n".join(lines)))

    # --- Maritime chokepoints ---
    maritime = sources.get("Maritime", {})
    chokepoints = maritime.get("chokepoints", {})
    if isinstance(chokepoints, dict) and chokepoints:
        lines = []
        for key, c in chokepoints.items():
            if isinstance(c, dict):
                label = c.get("label", key)
                note = c.get("note", "")
                vessels = c.get("vesselCount", 0)
                if vessels:
                    lines.append(f"- **{label}**: {vessels} vessels ({note})")
                elif note:
                    lines.append(f"- **{label}**: {note}")
        if lines:
            parts.append(_section("Maritime Chokepoints", "\n".join(lines)))
    elif isinstance(chokepoints, list):
        active_choke = [c for c in chokepoints if isinstance(c, dict) and c.get("vesselCount", 0) > 0]
        if active_choke:
            lines = [f"- **{c.get('name', '?')}**: {c['vesselCount']} vessels" for c in active_choke]
            parts.append(_section("Maritime Chokepoints", "\n".join(lines)))

    # --- Cyber threats ---
    otx = sources.get("OTX", {})
    if not otx.get("error") and otx.get("totalPulses", 0) > 0:
        lines = [f"- **{otx['totalPulses']} threat pulses** detected"]
        tags = otx.get("topTags", [])
        if tags:
            lines.append(f"- Top tags: {', '.join(tags[:8])}")
        targeted = otx.get("targetedCountries", {})
        if targeted:
            lines.append(f"- Targeted countries: {', '.join(f'{k} ({v})' for k, v in list(targeted.items())[:5])}")
        signals = otx.get("signals", [])
        for s in signals:
            lines.append(f"- {s}")
        parts.append(_section("Cyber Threat Intelligence (OTX)", "\n".join(lines)))

    # --- Defense spending ---
    spending = sources.get("USAspending", {})
    contracts = spending.get("recentDefenseContracts", [])
    if contracts:
        lines = []
        for c in contracts[:5]:
            amt = c.get("amount", 0)
            lines.append(f"- **${amt/1e6:.1f}M** — {c.get('description', '?')[:120]} ({c.get('recipient', '?')})")
        parts.append(_section("Recent Defense Contracts", "\n".join(lines)))

    # --- NOAA weather ---
    noaa = sources.get("NOAA", {})
    total_alerts = noaa.get("totalSevereAlerts", 0)
    if total_alerts > 0:
        top = noaa.get("topAlerts", [])[:5]
        lines = [f"- **{total_alerts} severe weather alerts active**"]
        for a in top:
            lines.append(f"- {a.get('event', '?')} — {a.get('areaDesc', '?')[:80]}")
        parts.append(_section("Severe Weather (NOAA)", "\n".join(lines)))

    # --- WHO health ---
    who = sources.get("WHO", {})
    outbreaks = who.get("diseaseOutbreakNews", [])
    if outbreaks:
        lines = [f"- {o.get('title', '?')[:120]}" for o in outbreaks[:5]]
        parts.append(_section("WHO Disease Alerts", "\n".join(lines)))

    # --- Sanctions ---
    ofac = sources.get("OFAC", {})
    osanc = sources.get("OpenSanctions", {})
    sanc_lines = []
    if ofac.get("lastUpdated"):
        sanc_lines.append(f"- OFAC last updated: {ofac['lastUpdated']}")
    new_entries = ofac.get("newEntries", [])
    if new_entries:
        for e in new_entries[:5]:
            sanc_lines.append(f"- NEW: {e.get('name', '?')} — {e.get('program', '?')}")
    if sanc_lines:
        parts.append(_section("Sanctions Updates", "\n".join(sanc_lines)))

    # --- Space ---
    space = sources.get("Space", {})
    launches = space.get("recentLaunches", [])
    if launches:
        lines = [f"- {l.get('name', '?')} — {l.get('date', '?')}" for l in launches[:5]]
        signals = space.get("signals", [])
        for s in signals:
            lines.append(f"- {s}")
        parts.append(_section("Space / Satellite Activity", "\n".join(lines)))

    # --- Social (Bluesky, Reddit) ---
    bluesky = sources.get("Bluesky", {})
    topics = bluesky.get("topics", {})
    reddit = sources.get("Reddit", {})
    social_lines = []
    if isinstance(topics, dict):
        for category, items in topics.items():
            if items:
                social_lines.append(f"**Bluesky {category}:** " + ", ".join(str(t) for t in items[:10]))
    elif isinstance(topics, list) and topics:
        social_lines.append("**Bluesky trending:** " + ", ".join(str(t) for t in topics[:10]))
    reddit_topics = reddit.get("topics") or reddit.get("trending")
    if isinstance(reddit_topics, list) and reddit_topics:
        social_lines.append("**Reddit trending:** " + ", ".join(str(t) for t in reddit_topics[:10]))
    if social_lines:
        parts.append(_section("Social Media Signals", "\n".join(social_lines)))

    # --- Delta (changes since last sweep) ---
    delta = data.get("_delta", {})
    if delta:
        lines = []
        for src, changes in delta.items():
            if isinstance(changes, dict) and changes:
                summary = json.dumps(changes, default=str)[:200]
                lines.append(f"- **{src}**: {summary}")
            elif isinstance(changes, str):
                lines.append(f"- **{src}**: {changes}")
        if lines:
            parts.append(_section("Changes Since Last Sweep (Delta)", "\n".join(lines)))

    # --- GDELT ---
    gdelt = sources.get("GDELT", {})
    total_articles = gdelt.get("totalArticles", 0)
    if total_articles > 0:
        lines = [f"- **{total_articles} articles** indexed"]
        for cat in ["conflicts", "economy", "health", "crisis"]:
            items = gdelt.get(cat, [])
            if items:
                lines.append(f"- **{cat.title()}**: {len(items)} articles")
                for a in items[:3]:
                    lines.append(f"  - {a.get('title', '?')[:100]}")
        parts.append(_section("Global News (GDELT)", "\n".join(lines)))

    # --- Patent signals ---
    patents = sources.get("Patents", {})
    signals = patents.get("signals", [])
    if signals:
        lines = [f"- {s}" for s in signals[:5]]
        parts.append(_section("Patent / Innovation Signals", "\n".join(lines)))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# News Aggregator (8 global sources)
# ---------------------------------------------------------------------------

def fetch_news_aggregator(limit=12):
    """Fetch headlines from the news-aggregator container. Returns list of items."""
    try:
        r = _session.get(
            f"{NEWS_AGGREGATOR_URL}/news",
            params={"limit": limit},
            timeout=60,
        )
        if not r.ok:
            print(f"  Warning: news-aggregator HTTP {r.status_code}: {r.text[:200]}")
            return []
        return r.json().get("items", [])
    except Exception as e:
        print(f"  Warning: news-aggregator error: {e}")
        return []


def news_to_markdown(items: list) -> str:
    """Convert news-aggregator JSON items into markdown sections by source."""
    if not items:
        return ""
    by_source = {}
    for item in items:
        src = item.get("source", "Unknown")
        by_source.setdefault(src, []).append(item)

    lines = []
    for src, src_items in by_source.items():
        lines.append(f"**{src}:**")
        for item in src_items:
            title = item.get("title", "?")
            heat = item.get("heat", "")
            time_str = item.get("time", "")
            extra = f" ({heat})" if heat else ""
            extra += f" — {time_str}" if time_str else ""
            lines.append(f"- {title}{extra}")
        lines.append("")

    return _section("Global News & Tech Headlines", "\n".join(lines))


# ---------------------------------------------------------------------------
# Social Sentiment for held positions
# ---------------------------------------------------------------------------


def get_held_symbols():
    """Read open positions from the schwalpaca trading journal."""
    if not SCHWALPACA_JOURNAL.exists():
        return []
    try:
        with SCHWALPACA_JOURNAL.open(encoding="utf-8") as f:
            journal = json.load(f)
        return list({t["symbol"] for t in journal.get("trades", []) if t.get("status") == "open" and t.get("symbol")})
    except Exception:
        return []


def get_held_kalshi_positions():
    """Read open positions from the kalshimarket trading journal.

    Kalshi markets are event contracts (e.g., KXBRENTW-26JUN2617-T94.99) — NOT
    stock tickers. They cannot be enriched via schwalpaca's news/sentiment APIs
    (those expect stock symbols like SPY/INTC). Returned for context only — the
    brief surfaces them so the simulation knows what's held cross-portfolio.
    Crucix OSINT already covers the underlying assets (EIA for crude, Treasury
    for rates, etc.).
    """
    if not KALSHI_JOURNAL.exists():
        return []
    try:
        with KALSHI_JOURNAL.open(encoding="utf-8") as f:
            journal = json.load(f)
        out = []
        for t in journal.get("trades", []):
            if t.get("status") != "open":
                continue
            ticker = t.get("market_ticker") or t.get("series_ticker") or t.get("event_ticker")
            if not ticker:
                continue
            out.append({
                "ticker": ticker,
                "title": t.get("title", ""),
                "side": t.get("side", ""),
                "contracts": t.get("count", t.get("contracts", 0)),
                "entry_price_cents": t.get("entry_price_cents"),
                "category": t.get("category", ""),
            })
        return out
    except Exception as e:
        print(f"  Warning: kalshi journal read failed: {e}")
        return []


def kalshi_positions_to_markdown(positions: list) -> str:
    """Format open Kalshi event-contract positions as markdown."""
    if not positions:
        return ""
    lines = []
    for p in positions:
        ticker = p.get("ticker", "?")
        title = (p.get("title") or "")[:120]
        side = (p.get("side") or "?").upper()
        contracts = p.get("contracts", 0)
        entry = p.get("entry_price_cents")
        entry_str = f" @ {entry/100:.2f}" if entry is not None else ""
        cat = p.get("category") or ""
        cat_str = f" *[{cat}]*" if cat else ""
        lines.append(f"- **{ticker}**{cat_str} — {side} × {contracts}{entry_str}")
        if title:
            lines.append(f"  > {title}")
    return _section("Cross-Portfolio: Kalshi Open Positions", "\n".join(lines))


def fetch_social_sentiment(symbol):
    """Fetch social sentiment for a symbol from schwalpaca API."""
    try:
        headers = {"X-API-Key": SCHWALPACA_API_KEY}
        r = _session.get(
            f"{SCHWALPACA_URL}/intel/social-sentiment/{symbol}",
            headers=headers,
            timeout=60,
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"  Warning: sentiment fetch failed for {symbol}: {e}")
    return None


def sentiment_to_markdown(sentiments: dict) -> str:
    """Convert social sentiment data for held symbols into markdown."""
    if not sentiments:
        return ""

    lines = []
    for symbol, data in sentiments.items():
        if not data or data.get("error"):
            continue
        overall = data.get("overall", "?").upper()
        confidence = data.get("confidence", 0)
        summary = data.get("summary", "")
        lines.append(f"**{symbol}** — {overall} ({confidence*100:.0f}% confidence)")
        if summary:
            lines.append(f"> {summary}")
        bull = data.get("bull_points", [])
        bear = data.get("bear_points", [])
        if bull:
            lines.append(f"- Bullish: {'; '.join(bull[:3])}")
        if bear:
            lines.append(f"- Bearish: {'; '.join(bear[:3])}")
        themes = data.get("key_themes", [])
        if themes:
            lines.append(f"- Themes: {', '.join(themes[:3])}")
        lines.append("")

    if not lines:
        return ""
    return _section("Social Sentiment — Held Positions (Perplexity + X/Twitter)", "\n".join(lines))


# ---------------------------------------------------------------------------
# Screeners
# ---------------------------------------------------------------------------


def fetch_screeners():
    """Fetch all screeners from schwalpaca API."""
    try:
        headers = {"X-API-Key": SCHWALPACA_API_KEY}
        r = _session.get(
            f"{SCHWALPACA_URL}/market-data/screen/all",
            headers=headers,
            params={"sentiment": True},
            timeout=120,
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"  Warning: screeners fetch failed: {e}")
    return None


def screeners_to_markdown(data: dict) -> str:
    """Convert screener results into markdown for MiroFish brief."""
    if not data:
        return ""

    parts = []

    # 52-week highs
    high = data.get("near_52w_high", [])
    if high:
        lines = []
        for s in high[:8]:
            sym = s["symbol"]
            pct = s.get("pct_from_high", 0)
            vol = s.get("volume_ratio", 1)
            sent = s.get("sentiment")
            sent_str = ""
            if sent and not sent.get("error"):
                sent_str = f" — {sent.get('sentiment', '?').upper()} ({sent.get('confidence', 0)*100:.0f}%)"
            lines.append(f"- **{sym}**: {pct:.1f}% from 52w high, vol {vol:.1f}x{sent_str}")
        parts.append(_section("Stocks Near 52-Week Highs", "\n".join(lines)))

    # Unusual volume
    volume = data.get("unusual_volume", [])
    if volume:
        lines = []
        for s in volume[:8]:
            sym = s["symbol"]
            ratio = s.get("volume_ratio", 1)
            change = s.get("change_pct", 0)
            sent = s.get("sentiment")
            sent_str = ""
            if sent and not sent.get("error"):
                sent_str = f" — {sent.get('sentiment', '?').upper()}"
            sign = "+" if change >= 0 else ""
            lines.append(f"- **{sym}**: {ratio:.1f}x avg volume, {sign}{change:.1f}%{sent_str}")
        parts.append(_section("Unusual Volume Activity", "\n".join(lines)))

    # Strong momentum
    momentum = data.get("strong_momentum", [])
    if momentum:
        lines = []
        for s in momentum[:8]:
            sym = s["symbol"]
            mom5 = s.get("momentum_5d", 0)
            mom20 = s.get("momentum_20d", 0)
            sent = s.get("sentiment")
            sent_str = ""
            if sent and not sent.get("error"):
                sent_str = f" — {sent.get('sentiment', '?').upper()}"
            lines.append(f"- **{sym}**: +{mom5:.1f}% (5d), +{mom20:.1f}% (20d){sent_str}")
        parts.append(_section("Strong Momentum Stocks", "\n".join(lines)))

    # Sector rotation
    sectors = data.get("sector_rotation", {})
    if sectors and sectors.get("sectors"):
        lines = [f"**Rotation signal**: {sectors.get('rotation_signal', '?')}"]
        leaders = sectors.get("leaders", [])
        laggers = sectors.get("laggers", [])
        if leaders:
            lines.append("")
            lines.append("**Leaders (5d):**")
            for s in leaders:
                lines.append(f"- **{s['etf']}** ({s['sector']}): +{s['perf_5d']:.1f}%")
        if laggers:
            lines.append("")
            lines.append("**Laggers (5d):**")
            for s in laggers:
                lines.append(f"- **{s['etf']}** ({s['sector']}): {s['perf_5d']:.1f}%")
        parts.append(_section("Sector Rotation", "\n".join(lines)))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Movers
# ---------------------------------------------------------------------------


def fetch_movers(index='$SPX.X'):
    """Fetch top gainers/losers from schwalpaca API (Schwab)."""
    try:
        headers = {"X-API-Key": SCHWALPACA_API_KEY}
        r = _session.get(
            f"{SCHWALPACA_URL}/market-data/schwab/movers/{index}",
            headers=headers,
            timeout=_TIMEOUT,
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"  Warning: movers fetch failed: {e}")
    return None


def movers_to_markdown(movers: list, held_symbols: set) -> str:
    """Format movers as markdown. Exclude held positions."""
    if not movers:
        return ""

    lines = []
    for m in movers[:10]:
        sym = m.get('symbol', '?')
        if sym in held_symbols:
            continue
        change_pct = m.get('change_percent', 0) or 0
        change = m.get('change', 0) or 0
        volume = m.get('volume')
        direction = "GAINER" if change_pct >= 0 else "LOSER"
        sign = "+" if change_pct >= 0 else ""
        vol_str = f", vol {volume:,}" if volume else ""
        lines.append(f"- **{sym}**: {sign}{change_pct:.2f}% ({sign}${change:.2f}) [{direction}]{vol_str}")

    if not lines:
        return ""
    return _section("Market Movers (Schwab)", "\n".join(lines))


# ---------------------------------------------------------------------------
# Mover Ticker News
# ---------------------------------------------------------------------------


def fetch_mover_news(movers: list, held_symbols: set, days: int = 3) -> dict:
    """Fetch news for top movers (excluding held). Returns {symbol: [articles]}.

    Parallelized — pre-filters up to 12 candidate symbols, then fetches with 8 workers.
    """
    if not movers:
        return {}
    held_set = set(held_symbols) if held_symbols else set()
    candidates = []
    for m in movers:
        s = m.get('symbol')
        if s and s not in held_set:
            candidates.append(s)
        if len(candidates) >= 12:  # buffer — cap results at 8 after dedup
            break

    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    headers = {"X-API-Key": SCHWALPACA_API_KEY}

    def fetch_one(sym):
        try:
            r = _session.get(
                f"{SCHWALPACA_URL}/intel/news/{sym}",
                headers=headers,
                params={"from": from_date, "limit": 3},
                timeout=_TIMEOUT,
            )
            if r.ok:
                arts = r.json()
                return arts if arts else None
        except Exception:
            pass
        return None

    out = _parallel_fetch(candidates, fetch_one, max_workers=8)
    return {s: a for s, a in out.items() if a}


def mover_news_to_markdown(news_data: dict) -> str:
    """Format mover news as markdown."""
    if not news_data:
        return ""

    lines = []
    for sym, articles in sorted(news_data.items()):
        lines.append(f"**{sym}:**")
        for a in articles[:3]:
            headline = a.get("headline") or a.get("title", "?")
            source = a.get("source", "")
            lines.append(f"- {headline} *(via {source})*")
        lines.append("")

    return _section("Mover Catalyst News", "\n".join(lines))


# ---------------------------------------------------------------------------
# Mover Sentiment
# ---------------------------------------------------------------------------


def fetch_mover_sentiment(movers: list, held_symbols: set) -> dict:
    """Fetch social sentiment for top movers (excluding held). Cap at 5 (60s timeout each).

    Parallelized — 5 workers. Lower than news because sentiment uses LLM-backed calls
    that are slower and may stress upstream rate limits.
    """
    if not movers:
        return {}
    held_set = set(held_symbols) if held_symbols else set()
    candidates = []
    for m in movers:
        s = m.get('symbol')
        if s and s not in held_set:
            candidates.append(s)
        if len(candidates) >= 8:  # buffer — cap results at 5 after success
            break

    headers = {"X-API-Key": SCHWALPACA_API_KEY}

    def fetch_one(sym):
        try:
            r = _session.get(
                f"{SCHWALPACA_URL}/intel/social-sentiment/{sym}",
                headers=headers,
                timeout=60,
            )
            if r.ok:
                data = r.json()
                if data and not data.get("error"):
                    print(f"  ✓ {sym}: {data.get('overall', '?')}")
                    return data
                print(f"  ✗ {sym}: {data.get('error', 'no data') if data else 'failed'}")
        except Exception as e:
            print(f"  Warning: sentiment fetch failed for {sym}: {e}")
        return None

    out = _parallel_fetch(candidates, fetch_one, max_workers=5)
    return {s: d for s, d in out.items() if d}


def mover_sentiment_to_markdown(sentiments: dict) -> str:
    """Format mover sentiment as markdown. Same structure as held position sentiment."""
    if not sentiments:
        return ""

    lines = []
    for symbol, data in sentiments.items():
        if not data or data.get("error"):
            continue
        overall = data.get("overall", "?").upper()
        confidence = data.get("confidence", 0)
        summary = data.get("summary", "")
        lines.append(f"**{symbol}** — {overall} ({confidence*100:.0f}% confidence)")
        if summary:
            lines.append(f"> {summary}")
        bull = data.get("bull_points", [])
        bear = data.get("bear_points", [])
        if bull:
            lines.append(f"- Bullish: {'; '.join(bull[:3])}")
        if bear:
            lines.append(f"- Bearish: {'; '.join(bear[:3])}")
        themes = data.get("key_themes", [])
        if themes:
            lines.append(f"- Themes: {', '.join(themes[:3])}")
        lines.append("")

    if not lines:
        return ""
    return _section("Social Sentiment — Market Movers", "\n".join(lines))


# ---------------------------------------------------------------------------
# Technical Indicators
# ---------------------------------------------------------------------------


def fetch_indicators(symbol):
    """Fetch technical indicators for a symbol (MA20/50, RSI14, momentum, vol, S/R)."""
    try:
        headers = {"X-API-Key": SCHWALPACA_API_KEY}
        r = _session.get(
            f"{SCHWALPACA_URL}/market-data/indicators/{symbol}",
            headers=headers,
            params={"period": "3M", "frequency": "daily"},
            timeout=_TIMEOUT,
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"  Warning: indicators fetch failed for {symbol}: {e}")
    return None


def indicators_to_markdown(indicators: dict) -> str:
    """Format technical indicators for multiple symbols as markdown."""
    if not indicators:
        return ""

    lines = []
    for sym in sorted(indicators.keys()):
        ind = indicators[sym]
        parts = []
        ma20 = ind.get("MA20")
        ma50 = ind.get("MA50")
        if ma20 is not None or ma50 is not None:
            ma_str = f"MA20={ma20:.2f}" if ma20 is not None else ""
            if ma50 is not None:
                ma_str = f"{ma_str} MA50={ma50:.2f}" if ma_str else f"MA50={ma50:.2f}"
            parts.append(ma_str)
        rsi = ind.get("RSI14")
        if rsi is not None:
            parts.append(f"RSI14={rsi:.1f}")
        vol_ratio = ind.get("volume_ratio")
        if vol_ratio is not None:
            parts.append(f"Vol {vol_ratio:.1f}x")
        mom = ind.get("momentum_5d")
        if mom is not None:
            sign = "+" if mom >= 0 else ""
            parts.append(f"Mom5d={sign}{mom:.1f}%")
        vol20 = ind.get("volatility_20d")
        if vol20 is not None:
            parts.append(f"Vol20d={vol20:.2f}%")
        support = ind.get("support")
        resistance = ind.get("resistance")
        if support is not None or resistance is not None:
            sr = []
            if support is not None:
                sr.append(f"S={support:.2f}")
            if resistance is not None:
                sr.append(f"R={resistance:.2f}")
            parts.append(" | ".join(sr))

        if parts:
            lines.append(f"- **{sym}**: {' | '.join(parts)}")

    if not lines:
        return ""
    return _section("Technical Indicators", "\n".join(lines))


# ---------------------------------------------------------------------------
# Options Flow (put/call ratio + unusual activity)
# ---------------------------------------------------------------------------


def fetch_option_flow(symbol):
    """Fetch option chain and compute put/call ratio + unusual activity for a symbol."""
    try:
        headers = {"X-API-Key": SCHWALPACA_API_KEY}
        r = _session.get(
            f"{SCHWALPACA_URL}/market-data/schwab/option-chain/{symbol}",
            headers=headers,
            params={"strike_count": 20},
            timeout=30,
        )
        if not r.ok:
            return None
        chain = r.json()
    except Exception as e:
        print(f"  Warning: option chain fetch failed for {symbol}: {e}")
        return None

    if not chain or chain.get("error"):
        return None

    calls = chain.get("calls", [])
    puts = chain.get("puts", [])
    total_call_vol = sum(c.get("totalVolume", 0) for c in calls)
    total_put_vol = sum(p.get("totalVolume", 0) for p in puts)
    total_call_oi = sum(c.get("openInterest", 0) for c in calls)
    total_put_oi = sum(p.get("openInterest", 0) for p in puts)
    pc_ratio_vol = round(total_put_vol / total_call_vol, 2) if total_call_vol > 0 else None
    pc_ratio_oi = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else None

    # Flag unusual: volume >> open interest suggests new positioning
    unusual = []
    for opt in calls + puts:
        vol = opt.get("totalVolume", 0)
        oi = opt.get("openInterest", 0)
        if oi > 0 and vol > oi * 2:
            unusual.append({
                "type": "call" if opt in calls else "put",
                "strike": opt.get("strikePrice"),
                "volume": vol,
                "oi": oi,
                "ratio": round(vol / oi, 1),
            })

    signal = ("bearish" if pc_ratio_vol and pc_ratio_vol > 1.5 else
              "bullish" if pc_ratio_vol and pc_ratio_vol < 0.5 else "neutral")

    return {
        "total_call_volume": total_call_vol,
        "total_put_volume": total_put_vol,
        "put_call_ratio_volume": pc_ratio_vol,
        "put_call_ratio_oi": pc_ratio_oi,
        "unusual_activity": unusual[:5],
        "signal": signal,
    }


def options_flow_to_markdown(flows: dict) -> str:
    """Format options flow data for multiple symbols as markdown."""
    if not flows:
        return ""

    lines = []
    for sym in sorted(flows.keys()):
        f = flows[sym]
        pc_vol = f.get("put_call_ratio_volume")
        pc_oi = f.get("put_call_ratio_oi")
        signal = f.get("signal", "?").upper()
        parts = [f"P/C vol={pc_vol}" if pc_vol else "", f"P/C OI={pc_oi}" if pc_oi else "", signal]
        lines.append(f"- **{sym}**: {' | '.join(p for p in parts if p)}")
        for u in f.get("unusual_activity", []):
            lines.append(f"  - UNUSUAL: {u['type']} ${u['strike']} — vol {u['volume']} vs OI {u['oi']} ({u['ratio']}x)")

    return _section("Options Flow (Put/Call + Unusual Activity)", "\n".join(lines))


# ---------------------------------------------------------------------------
# Earnings Calendar
# ---------------------------------------------------------------------------


def fetch_earnings(days_ahead=7):
    """Fetch earnings calendar for the next N days from schwalpaca."""
    try:
        headers = {"X-API-Key": SCHWALPACA_API_KEY}
        today = datetime.now().strftime("%Y-%m-%d")
        end = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        r = _session.get(
            f"{SCHWALPACA_URL}/intel/earnings",
            headers=headers,
            params={"from_date": today, "to_date": end},
            timeout=_TIMEOUT,
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"  Warning: earnings fetch failed: {e}")
    return None


def earnings_to_markdown(earnings_data, held_symbols: set = None) -> str:
    """Format earnings calendar as markdown, flagging held positions."""
    if not earnings_data:
        return ""

    items = earnings_data if isinstance(earnings_data, list) else earnings_data.get("data", [])
    if not items:
        return ""

    lines = []
    for e in items[:20]:
        if not isinstance(e, dict):
            continue
        sym = e.get("symbol", "?")
        date = e.get("date", e.get("reportDate", "?"))
        timing = e.get("timing", e.get("time", ""))
        held_flag = " **[HELD]**" if held_symbols and sym in held_symbols else ""
        lines.append(f"- **{sym}** — {date} {timing}{held_flag}")

    if not lines:
        return ""
    return _section("Upcoming Earnings (next 7 days)", "\n".join(lines))


# ---------------------------------------------------------------------------
# Screener Ticker News (past 7 days)
# ---------------------------------------------------------------------------


def fetch_screener_news(screener_data: dict, held_symbols: set, days: int = 7) -> dict:
    """Fetch news for screener tickers (past N days). Returns {symbol: [articles]}."""
    if not screener_data:
        return {}

    screener_symbols = set()
    for key in ["near_52w_high", "unusual_volume", "strong_momentum"]:
        for item in screener_data.get(key, []):
            sym = item.get("symbol")
            if sym and sym not in held_symbols:
                screener_symbols.add(sym)

    if not screener_symbols:
        return {}

    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    headers = {"X-API-Key": SCHWALPACA_API_KEY}

    def fetch_one(sym):
        try:
            r = _session.get(
                f"{SCHWALPACA_URL}/intel/news/{sym}",
                headers=headers,
                params={"from": from_date, "limit": 3},
                timeout=10,
            )
            if r.ok:
                arts = r.json()
                return arts if arts else None
        except Exception:
            pass
        return None

    out = _parallel_fetch(list(screener_symbols)[:12], fetch_one, max_workers=10)
    return {s: a for s, a in out.items() if a}


def screener_news_to_markdown(news_data: dict) -> str:
    """Convert screener ticker news into markdown."""
    if not news_data:
        return ""

    lines = []
    for sym, articles in sorted(news_data.items()):
        lines.append(f"**{sym}:**")
        for a in articles[:3]:
            headline = a.get("headline") or a.get("title", "?")
            source = a.get("source", "")
            lines.append(f"- {headline} *(via {source})*")
        lines.append("")

    return _section("Screener Ticker News (past 7 days)", "\n".join(lines))


# ---------------------------------------------------------------------------
# MiroFish API pipeline
# ---------------------------------------------------------------------------


def poll_task(task_id: str, label: str, endpoint: str = "/api/graph/task"):
    """Poll a MiroFish async task until completion."""
    url = f"{MIROFISH_URL}{endpoint}/{task_id}"
    spinner = ["|", "/", "-", "\\"]
    i = 0
    while True:
        r = _session.get(url, timeout=30)
        r.raise_for_status()
        task = r.json().get("data", {})
        status = task.get("status", "unknown")
        progress = task.get("progress", 0)
        message = task.get("message", "")

        sys.stdout.write(f"\r  {spinner[i % 4]} [{label}] {status} {progress}% — {message[:60]:<60}")
        sys.stdout.flush()
        i += 1

        if status in ("completed", "COMPLETED"):
            print(f"\n  OK: {label} completed.")
            return task
        elif status in ("failed", "FAILED"):
            print(f"\n  FAILED: {label} — {task.get('error', message)}")
            sys.exit(1)

        time.sleep(3)


def _state_path():
    """Path to the pipeline checkpoint file."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / ".pipeline_state.json"


def _save_state(state: dict):
    """Save pipeline checkpoint."""
    state["updated_at"] = _now().isoformat()
    _atomic_write_text(_state_path(), json.dumps(state, indent=2))


def _load_state() -> dict | None:
    """Load pipeline checkpoint, or None if no valid state."""
    p = _state_path()
    if not p.exists():
        return None
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        # Only resume today's runs
        today = _now().strftime("%Y-%m-%d")
        if state.get("date") != today:
            return None
        return state
    except (json.JSONDecodeError, KeyError):
        return None


def _clear_state():
    """Remove checkpoint file after successful completion."""
    p = _state_path()
    if p.exists():
        p.unlink()


def run_pipeline(
    md_path: str,
    max_rounds: int,
    project_name: str,
    resume: bool = False,
    manifest_path: str | None = None,
):
    """Drive the full MiroFish pipeline with checkpoint/resume support.

    After each step, state is saved to .pipeline_state.json. If the pipeline
    crashes, re-run with --resume to pick up from the last completed step.
    """
    base = MIROFISH_URL

    # Load checkpoint if resuming
    state = _load_state() if resume else None
    if state:
        completed = state.get("completed_step", 0)
        print(f"\n  Resuming from checkpoint — last completed step: {completed}/6")
        print(f"  project_id:    {state.get('project_id')}")
        print(f"  graph_id:      {state.get('graph_id')}")
        print(f"  simulation_id: {state.get('simulation_id')}")
    else:
        state = {
            "date": _now().strftime("%Y-%m-%d"),
            "completed_step": 0,
            "md_path": md_path,
            "max_rounds": max_rounds,
            "project_name": project_name,
            "manifest_path": manifest_path,
        }
        completed = 0

    print(f"\n{'='*60}")
    print(f"  MiroFish Pipeline — {project_name}")
    print(f"  Max rounds: {max_rounds}")
    print(f"  Backend: {base}")
    if completed > 0:
        print(f"  Resuming from step {completed + 1}")
    print(f"{'='*60}\n")

    # --- Step 1: Upload & generate ontology ---
    if completed < 1:
        print("[1/6] Uploading brief & generating ontology...")
        with open(md_path, "rb") as f:
            r = _session.post(
                f"{base}/api/graph/ontology/generate",
                files={"files": ("crucix_brief.md", f, "text/markdown")},
                data={
                    "simulation_requirement": SIMULATION_REQUIREMENT,
                    "project_name": project_name,
                },
                timeout=300,
            )
        if not r.ok:
            print(f"  HTTP {r.status_code}")
            try:
                print(f"  Response: {r.json()}")
            except Exception:
                print(f"  Response: {r.text[:500]}")
            sys.exit(1)
        resp = r.json()
        if not resp.get("success"):
            print(f"  FAILED: {resp.get('error')}")
            if resp.get("traceback"):
                print(f"  Traceback:\n{resp['traceback']}")
            sys.exit(1)
        state["project_id"] = resp["data"]["project_id"]
        entity_types = len(resp["data"]["ontology"].get("entity_types", []))
        edge_types = len(resp["data"]["ontology"].get("edge_types", []))
        print(f"  OK: project={state['project_id']}, {entity_types} entity types, {edge_types} edge types")
        state["completed_step"] = 1
        _save_state(state)
    else:
        print("[1/6] Uploading brief & generating ontology... (cached)")

    project_id = state["project_id"]

    # --- Step 2: Build graph ---
    if completed < 2:
        print("\n[2/6] Building knowledge graph...")
        # Smart 400 retry (Discovered 2026-07-05): backend can return 400
        # transiently (e.g., during startup when Flask is still loading
        # blueprints). Retry once with 5s delay before treating as fatal.
        r = None
        for attempt in range(2):
            r = _session.post(
                f"{base}/api/graph/build",
                json={"project_id": project_id},
                timeout=30,
            )
            if r.status_code == 400 and attempt == 0:
                print(f"  graph build 400 on attempt 1 — retrying in 5s (transient?)")
                time.sleep(5)
                continue
            break
        # 400 after retry = genuine stale state (project not in backend)
        if r.status_code == 400:
            error_body = r.text[:300] if hasattr(r, 'text') else '(no body)'
            print(f"  graph build 400: {error_body}")
            print(f"  Project {project_id} likely not fully created — restarting from step 1")
            state["completed_step"] = 0  # clear partial
            state["project_id"] = None
            state["graph_id"] = None
            state["simulation_id"] = None
            _save_state(state)
            print(f"  checkpoint cleared — re-run with --resume to retry from step 1")
            sys.exit(1)
        r.raise_for_status()
        resp = r.json()
        if not resp.get("success"):
            print(f"  FAILED: {resp.get('error')}")
            sys.exit(1)
        task_id = resp["data"]["task_id"]
        task_result = poll_task(task_id, "graph build")
        state["graph_id"] = task_result.get("result", {}).get("graph_id")
        print(f"  graph_id={state['graph_id']}")
        state["completed_step"] = 2
        _save_state(state)
    else:
        print("[2/6] Building knowledge graph... (cached)")

    graph_id = state["graph_id"]

    # --- Step 3: Create simulation ---
    if completed < 3:
        print("\n[3/6] Creating simulation...")
        r = _session.post(
            f"{base}/api/simulation/create",
            json={
                "project_id": project_id,
                "graph_id": graph_id,
                "enable_twitter": True,
                "enable_reddit": True,
            },
            timeout=30,
        )
        r.raise_for_status()
        resp = r.json()
        if not resp.get("success"):
            print(f"  FAILED: {resp.get('error')}")
            sys.exit(1)
        state["simulation_id"] = resp["data"]["simulation_id"]
        print(f"  OK: simulation_id={state['simulation_id']}")
        state["completed_step"] = 3
        _save_state(state)
    else:
        print("[3/6] Creating simulation... (cached)")

    simulation_id = state["simulation_id"]

    # --- Step 4: Prepare simulation ---
    if completed < 4:
        print("\n[4/6] Preparing simulation (generating agent profiles & config)...")
        r = _session.post(
            f"{base}/api/simulation/prepare",
            json={"simulation_id": simulation_id},
            timeout=30,
        )
        r.raise_for_status()
        resp = r.json()
        if not resp.get("success"):
            print(f"  FAILED: {resp.get('error')}")
            sys.exit(1)

        if resp["data"].get("already_prepared"):
            print("  OK: already prepared (reusing)")
        else:
            task_id = resp["data"]["task_id"]
            poll_task(task_id, "prepare sim", endpoint="/api/graph/task")
        state["completed_step"] = 4
        _save_state(state)
    else:
        print("[4/6] Preparing simulation... (cached)")

    # --- Step 5: Start simulation ---
    if completed < 5:
        print(f"\n[5/6] Running simulation (max {max_rounds} rounds)...")
        # Smart 400 retry: same pattern as step 2 (Discovered 2026-07-05)
        r = None
        for attempt in range(2):
            r = _session.post(
                f"{base}/api/simulation/start",
                json={
                    "simulation_id": simulation_id,
                    "platform": "parallel",
                    "max_rounds": max_rounds,
                },
                timeout=30,
            )
            if r.status_code == 400 and attempt == 0:
                print(f"  simulation/start 400 on attempt 1 — retrying in 5s (transient?)")
                time.sleep(5)
                continue
            break
        # 400 after retry = genuine stale state (simulation_id not in backend)
        if r.status_code == 400:
            error_body = r.text[:300] if hasattr(r, 'text') else '(no body)'
            print(f"  simulation/start 400: {error_body}")
            print(f"  simulation_id {simulation_id} likely doesn't exist in current backend — re-creating")
            state["completed_step"] = 2  # back to before simulation create
            state["simulation_id"] = None
            _save_state(state)
            print(f"  checkpoint adjusted — re-run with --resume to recreate simulation")
            sys.exit(1)
        r.raise_for_status()
        resp = r.json()
        if not resp.get("success"):
            print(f"  FAILED: {resp.get('error')}")
            sys.exit(1)
        print(f"  OK: simulation running (pid={resp['data'].get('process_pid')})")

        # Poll simulation status (90-minute wall-clock timeout)
        spinner = ["|", "/", "-", "\\"]
        i = 0
        poll_start = time.time()
        current = 0
        total = max_rounds
        MAX_SIM_SECONDS = 90 * 60
        # Watchdog: if status doesn't change for STALE_POLL_SECONDS, treat as hung.
        # Catches the architectural gap where the simulation child dies but the
        # API's in-memory status stays "running" forever (orchestrator never
        # sees the failure). Discovered 2026-07-04.
        STALE_POLL_SECONDS = 10 * 60
        last_state_change = time.time()
        last_signature = None
        while True:
            elapsed_min = (time.time() - poll_start) / 60
            if time.time() - poll_start > MAX_SIM_SECONDS:
                print(f"\n  TIMEOUT: simulation exceeded 90 min ({current}/{total} rounds) — proceeding with partial results")
                break

            try:
                r = _session.get(f"{base}/api/simulation/{simulation_id}/run-status", timeout=180)
                r.raise_for_status()
                status_data = r.json().get("data", {})
                runner_status = status_data.get("runner_status", "unknown")
                current = status_data.get("current_round", 0)
                total = status_data.get("total_rounds", "?")
                pct = status_data.get("progress_percent", 0)
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
                elapsed_min = (time.time() - poll_start) / 60
                sys.stdout.write(f"\r  {spinner[i % 4]} [simulation] poll hiccup ({type(e).__name__}, retrying) {elapsed_min:.0f}m   ")
                sys.stdout.flush()
                i += 1
                time.sleep(5)
                continue

            # Watchdog: detect stale status (no progress for too long).
            # This catches the case where the simulation child process dies but
            # the API's in-memory state still says "running" because the
            # monitor thread's state updates don't propagate to the orchestrator.
            signature = (runner_status, current, total, pct)
            if signature != last_signature:
                last_signature = signature
                last_state_change = time.time()
            elif (time.time() - last_state_change) > STALE_POLL_SECONDS:
                print(f"\n  WATCHDOG: simulation status unchanged for {STALE_POLL_SECONDS//60} min "
                      f"(state={signature}). Child process likely died without status update.")
                print(f"  This catches the architectural gap where mirofish-api simulation state")
                print(f"  is stale because the actual simulation process died but the API doesn't")
                print(f"  know. Failing fast rather than waiting 90 min for the hard timeout.")
                sys.exit(2)  # distinct exit code so cron can detect "hung vs failed"

            sys.stdout.write(f"\r  {spinner[i % 4]} [simulation] {runner_status} round {current}/{total} ({pct}%) {elapsed_min:.0f}m   ")
            sys.stdout.flush()
            i += 1

            if runner_status in ("completed", "stopped"):
                print(f"\n  OK: simulation {runner_status}.")
                break
            elif runner_status in ("failed", "process_died"):
                # process_died: API's PID liveness check detected worker died
                # without state update (PID file in <sim_id>/worker.pid was dead).
                # Treat as a hard failure — no point continuing to step 6.
                if runner_status == "process_died":
                    print(f"\n  FAILED: simulation worker process died unexpectedly (state still says 'running' but PID is dead)")
                    print(f"  This is the silent-hang mode the PID liveness check catches.")
                else:
                    print(f"\n  FAILED: simulation failed")
                sys.exit(1)

            time.sleep(5)

        state["completed_step"] = 5
        _save_state(state)
    else:
        print("[5/6] Running simulation... (cached)")

    # --- Step 6: Generate report (with retry for rate limits) ---
    if completed < 6:
        max_report_retries = 3
        report_id = None
        for report_attempt in range(max_report_retries):
            print(f"\n[6/6] Generating prediction report{f' (retry {report_attempt})' if report_attempt else ''}...")
            r = _session.post(
                f"{base}/api/report/generate",
                json={"simulation_id": simulation_id, "force_regenerate": report_attempt > 0},
                timeout=30,
            )
            r.raise_for_status()
            resp = r.json()
            if not resp.get("success"):
                print(f"  FAILED: {resp.get('error')}")
                sys.exit(1)

            report_id = resp["data"].get("report_id")
            if resp["data"].get("already_generated"):
                print(f"  OK: report already exists: {report_id}")
                break

            task_id = resp["data"]["task_id"]
            task_result = poll_task(task_id, "report gen", endpoint="/api/graph/task")
            if task_result.get("status") in ("completed", "COMPLETED"):
                break

            # Task failed — likely rate limit, wait and retry
            if report_attempt < max_report_retries - 1:
                wait = 60 * (report_attempt + 1)
                print(f"  Waiting {wait}s before retry (rate limit cooldown)...")
                time.sleep(wait)
            else:
                print(f"  Report generation failed after {max_report_retries} attempts.")
                sys.exit(1)

        state["report_id"] = report_id
        state["completed_step"] = 6
        _save_state(state)
    else:
        report_id = state.get("report_id")
        print("[6/6] Generating prediction report... (cached)")

    # Fetch the report
    r = _session.get(f"{base}/api/report/{report_id}", timeout=30)
    r.raise_for_status()
    report = r.json().get("data", {})

    print(f"\n{'='*60}")
    print(f"  DONE")
    print(f"  Project:    {state['project_id']}")
    print(f"  Simulation: {simulation_id}")
    print(f"  Report:     {report_id}")
    print(f"{'='*60}")

    # Preserve the complete backend simulation as a non-discoverable source
    # artifact. Publish the evidence/scenario rendering first, followed by the
    # original model-derived narrative and trading implications.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = _now().strftime("%Y%m%d_%H%M%S")
    artifact_report_id = f"prediction_{ts}"
    report_path = OUTPUT_DIR / f"prediction_{ts}.md"
    simulation_path = OUTPUT_DIR / f"simulation_{ts}.md"
    md_content = report.get("markdown_content", "")
    if not md_content:
        # Hard fail — the Apr 15 incident was exactly this: pipeline reported
        # success but the markdown came back empty. Silent warning = silent loss.
        print("  FAILED: report markdown was empty — pipeline returned no content")
        sys.exit(1)

    saved_manifest_path = state.get("manifest_path") or manifest_path
    if not saved_manifest_path or not Path(saved_manifest_path).is_file():
        print("  FAILED: observation manifest input is missing")
        sys.exit(1)
    try:
        _atomic_write_text(simulation_path, md_content, _validate_report_text)
        artifact_time = _now().isoformat()
        manifest = json.loads(
            Path(saved_manifest_path).read_text(encoding="utf-8")
        )
        manifest["report_id"] = artifact_report_id
        manifest["generated_at"] = artifact_time
        validate_observation_manifest(manifest)
        try:
            structured = extract_scenario_synthesis(
                md_content,
                manifest,
                report_id=artifact_report_id,
                generated_at=artifact_time,
            )
        except ValueError as extraction_error:
            print(
                "  Embedded scenario block rejected; attempting constrained "
                f"repair ({extraction_error})"
            )
            structured = repair_scenario_synthesis(
                md_content,
                Path(md_path).read_text(encoding="utf-8"),
                manifest,
                report_id=artifact_report_id,
                generated_at=artifact_time,
            )
        canonical_markdown = append_preserved_simulation(
            render_scenario_markdown(structured, manifest),
            md_content,
        )
        _validate_report_text(canonical_markdown)

        final_manifest_path = OUTPUT_DIR / f"prediction_{ts}.manifest.json"
        structured_path = OUTPUT_DIR / f"prediction_{ts}.json"
        _atomic_write_json(
            final_manifest_path,
            manifest,
            validate_observation_manifest,
        )
        _atomic_write_json(
            structured_path,
            structured,
            lambda value: validate_scenario_synthesis(value, manifest),
        )
        # Publish Markdown last so discovery never sees a report without both
        # validated schema siblings.
        _atomic_write_text(
            report_path,
            canonical_markdown,
            _validate_report_text,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        print(f"  FAILED: scenario publication rejected: {error}")
        sys.exit(1)

    print(f"  Scenario report saved: {report_path}")
    print(f"  Original simulation saved: {simulation_path}")
    print(f"  Structured artifact saved: {structured_path}")
    print(f"  Manifest saved: {final_manifest_path}")

    # VERIFY: a prediction file for today exists with non-trivial size.
    # Cron health check used to just look at process exit status — silent
    # step-6 failures slipped through. This catches the file-missing case.
    today = _now().strftime("%Y%m%d")
    today_files = sorted(
        path for path in OUTPUT_DIR.glob(f"prediction_{today}*.md")
        if _is_valid_report(path)
    )
    if not today_files:
        print(f"  FAILED: no prediction_{today}*.md found in {OUTPUT_DIR} after pipeline completion")
        sys.exit(1)
    newest = max(today_files, key=lambda path: path.stat().st_mtime)
    print(f"  VERIFIED: today's report = {newest.name} ({newest.stat().st_size:,} bytes)")

    # Also save the brief for reference
    brief_out = OUTPUT_DIR / f"brief_{ts}.md"
    _atomic_write_text(brief_out, Path(md_path).read_text(encoding="utf-8"))
    print(f"  Brief saved: {brief_out}")

    # Preserve the established English sibling for direct readers and downstream
    # consumers. The canonical report remains untouched if translation fails.
    try:
        translate_report_to_english(report_path)
    except Exception as e:
        print(f"  Translation to English failed (non-fatal): {e}")
    _clear_state()

    return {
        "project_id": state["project_id"],
        "simulation_id": simulation_id,
        "report_id": report_id,
        "report_path": str(report_path),
        "simulation_path": str(simulation_path),
        "structured_path": str(structured_path),
        "manifest_path": str(final_manifest_path),
    }


def _call_llama_swap(prompt: str, model: str = "granite4.1-8b",
                      host: str = LLAMA_SWAP_URL,
                      timeout: int = 120) -> str | None:
    """Try local llama-swap translation. Returns None if unavailable."""
    import urllib.request, json as _json, time
    start = time.time()
    try:
        req = urllib.request.Request(
            f"{host}/v1/chat/completions",
            data=_json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 4096,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read())
        text = data["choices"][0]["message"]["content"]
        print(f"    local {model}: {len(text)} chars in {time.time()-start:.1f}s")
        return text
    except Exception as e:
        print(f"    local {model} failed ({e}), falling back to API...")
        return None


def translate_report_to_english(report_path: Path) -> None:
    """Translate a Chinese OASIS report to English and save as *._en.md.

    Tries local llama-swap (granite4.1-8b) first for speed & privacy,
    then falls back to kimi-k2.6 via API.
    """
    import urllib.request, json as _json
    print(f"\n  Translating report to English...")

    src_text = report_path.read_text(encoding="utf-8")
    if not src_text.strip():
        print(f"  Empty source, skipping translation")
        return

    # Truncate to fit safely in 32k tokens.
    if len(src_text) > 80000:
        src_text = src_text[:80000] + "\n\n[truncated]"

    prompt = (
        "Translate the following Chinese financial intelligence report to English. "
        "Preserve all markdown structure, headers, numerical data, and entity names "
        "(e.g., 'COIN 75%', 'TLT $85.51'). Use professional financial analyst English. "
        "Keep bullet points and section hierarchy intact. Translate the full report — "
        "do not summarize or skip sections.\n\n"
        f"{src_text}"
    )

    # Try local llama-swap first
    en_text = _call_llama_swap(prompt)
    if en_text is not None:
        en_path = report_path.with_name(report_path.stem + "_en.md")
        _atomic_write_text(en_path, en_text, _validate_report_text)
        print(f"  English translation saved: {en_path.name} ({len(en_text):,} chars)")
        return

    # Fallback: API (kimi-k2.6 or primary)
    llm_key = os.environ.get("LLM_BOOST_API_KEY") or os.environ.get("LLM_API_KEY")
    boost_model = os.environ.get("LLM_BOOST_MODEL_NAME", "kimi-k2.6")
    primary_key = os.environ.get("LLM_API_KEY")
    primary_model = os.environ.get("LLM_MODEL_NAME", "MiniMax-M3")
    if not llm_key:
        print(f"  No LLM_API_KEY and local failed, skipping translation")
        return

    for label, url, key, model in [
        ("boost", LLM_BOOST_BASE_URL, llm_key, boost_model),
        ("primary", LLM_BASE_URL, primary_key, primary_model),
    ]:
        if not key:
            continue
        print(f"    trying {label}: {model}...")
        try:
            req = urllib.request.Request(
                f"{url}/chat/completions",
                data=_json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 1,  # kimi requires 1
                    "max_tokens": 4000,
                }).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = _json.loads(resp.read())
            en_text = data["choices"][0]["message"]["content"]
            print(f"    {label} ({model}): {len(en_text)} chars")
            break
        except Exception as e:
            print(f"    {label} failed: {e}")
            continue
    else:
        raise RuntimeError("translation: all backends failed")

    en_path = report_path.with_name(report_path.stem + "_en.md")
    _atomic_write_text(en_path, en_text, _validate_report_text)
    print(f"  English translation saved: {en_path.name} ({len(en_text):,} chars)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Feed Crucix OSINT into MiroFish for trading predictions")
    parser.add_argument("--max-rounds", type=int, default=30, help="Max simulation rounds (default: 30; reduced from 40 to cut the AFTER-the-rounds hang rate)")
    parser.add_argument("--crucix-json", default=CRUCIX_LATEST, help="Path to latest.json")
    parser.add_argument("--dry-run", action="store_true", help="Just generate markdown, don't run simulation")
    parser.add_argument("--project-name", default=None, help="Override project name")
    parser.add_argument("--no-news", action="store_true", help="Skip news-aggregator fetch")
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint (skips data gathering)")
    args = parser.parse_args()

    # Resume mode — skip data gathering, jump straight to pipeline
    if args.resume:
        state = _load_state()
        if not state:
            print("No valid checkpoint found for today. Run without --resume first.")
            sys.exit(1)
        md_path = state.get("md_path")
        if not md_path or not os.path.exists(md_path):
            print(f"Checkpoint brief not found at {md_path}. Run without --resume.")
            sys.exit(1)
        print(f"Resuming pipeline from step {state['completed_step'] + 1}/6...")
        run_pipeline(
            md_path,
            state.get("max_rounds", args.max_rounds),
            state.get("project_name", "Crucix Trading Intel"),
            resume=True,
        )
        return

    # Load Crucix data
    json_path = args.crucix_json
    if not os.path.exists(json_path):
        print(f"Error: Crucix latest.json not found at {json_path}")
        sys.exit(1)

    print(f"Reading Crucix data from {json_path}...")

    # Force a fresh Adanos fetch so MiroFish simulation runs on up-to-the-minute
    # social sentiment instead of up-to-2hr-old cached data.
    refresh_adanos()

    with open(json_path, "r", encoding="utf-8") as f:
        crucix_data = json.load(f)

    supplements = {}

    def record_supplement(
        source_id,
        payload,
        *,
        source_type,
        independence_group,
        market_derived=False,
        status=None,
        observed_at=None,
        as_of=None,
    ):
        if status is None:
            if payload is None:
                status = "unavailable"
            elif hasattr(payload, "__len__") and len(payload) == 0:
                status = "degraded"
            else:
                status = "ok"
        supplements[source_id] = {
            "payload": payload,
            "source_type": source_type,
            "independence_group": independence_group,
            "market_derived": market_derived,
            "status": status,
            "fetched_at": _now().isoformat(),
            "observed_at": observed_at,
            "as_of": as_of,
        }

    sweep_ts = crucix_data.get("crucix", {}).get("timestamp", "unknown")
    sources_ok = crucix_data.get("crucix", {}).get("sourcesOk", 0)
    source_health = crucix_data.get("sourceHealth", {})
    sources_total = (
        len(source_health)
        or crucix_data.get("crucix", {}).get("sourcesQueried")
        or (
            sources_ok
            + crucix_data.get("crucix", {}).get("sourcesFailed", 0)
        )
        or len(crucix_data.get("sources", {}))
    )
    print(f"  Sweep time: {sweep_ts}")
    print(f"  Sources OK: {sources_ok}/{sources_total}")

    # Convert to markdown
    md = crucix_to_markdown(crucix_data)
    print(f"  Crucix brief: {len(md)} characters")

    # Fetch and append news-aggregator headlines
    if not args.no_news:
        print("Fetching news-aggregator (8 global sources)...")
        news_items = fetch_news_aggregator(limit=12)
        record_supplement(
            "Supplement/NewsAggregator",
            news_items,
            source_type="news",
            independence_group="news-aggregator",
        )
        if news_items:
            news_md = news_to_markdown(news_items)
            md += "\n" + news_md
            print(f"  Added {len(news_items)} headlines from {len(set(i.get('source') for i in news_items))} sources")
        else:
            print("  No news items retrieved")
    # Fetch and append Insider Capitol congressional trading signal
    print("Fetching Insider Capitol congressional trading signal...")
    ic_signal = fetch_insider_capitol_signal()
    record_supplement(
        "Supplement/InsiderCapitol",
        ic_signal,
        source_type="osint",
        independence_group="insider-capitol",
    )
    if ic_signal:
        ic_md = insider_capitol_to_markdown(ic_signal)
        if ic_md:
            md += "\n" + _section("Congressional Trading (Insider Capitol)", ic_md)
            print(f"  Added Insider Capitol signal to brief")
    print(f"  Total brief: {len(md)} characters")

    # Fetch social sentiment for held positions
    print("Fetching social sentiment for held positions...")
    held_symbols = get_held_symbols()
    record_supplement(
        "Supplement/SchwalpacaJournal",
        {"open_symbols": sorted(held_symbols)},
        source_type="portfolio",
        independence_group="schwalpaca-journal",
        status="ok" if SCHWALPACA_JOURNAL.is_file() else "unavailable",
    )
    if held_symbols:
        print(f"  Held symbols: {', '.join(held_symbols)}")
        sentiments = {}
        sym_results = _parallel_fetch(held_symbols[:5], fetch_social_sentiment, max_workers=5)
        for sym, sentiment_data in sym_results.items():
            if sentiment_data and not sentiment_data.get("error"):
                sentiments[sym] = sentiment_data
                print(f"  ✓ {sym}: {sentiment_data.get('overall', '?')}")
            else:
                err = (
                    sentiment_data.get('error', 'no data')
                    if sentiment_data
                    else 'failed'
                )
                print(f"  ✗ {sym}: {err}")
        sentiment_md = sentiment_to_markdown(sentiments)
        record_supplement(
            "Supplement/SchwalpacaHeldSentiment",
            sentiments,
            source_type="news",
            independence_group="schwalpaca-social-sentiment",
        )
        if sentiment_md:
            md += "\n" + sentiment_md
            print(f"  Added sentiment for {len(sentiments)} symbols")
    else:
        print("  No held positions found in journal")

    # Cross-portfolio: Kalshi event-contract positions (context only, not enriched via schwalpaca APIs)
    kalshi_positions = get_held_kalshi_positions()
    record_supplement(
        "Supplement/KalshiJournal",
        kalshi_positions,
        source_type="portfolio",
        independence_group="kalshi-journal",
        status="ok" if KALSHI_JOURNAL.is_file() else "unavailable",
    )
    if kalshi_positions:
        print(f"Cross-portfolio: {len(kalshi_positions)} open Kalshi position(s) (context only)...")
        kalshi_md = kalshi_positions_to_markdown(kalshi_positions)
        if kalshi_md:
            md += "\n" + kalshi_md
            print(f"  Added {len(kalshi_positions)} Kalshi positions to brief")
    else:
        print("  No open Kalshi positions")

    # Fetch news for held positions
    if held_symbols:
        print("Fetching news for held positions...")
        headers = {"X-API-Key": SCHWALPACA_API_KEY}

        def _held_news(sym):
            try:
                r = _session.get(
                    f"{SCHWALPACA_URL}/intel/news/{sym}",
                    headers=headers,
                    params={"limit": 5},
                    timeout=_TIMEOUT,
                )
                if r.ok:
                    arts = r.json()
                    return arts if arts else None
            except Exception:
                pass
            return None

        out = _parallel_fetch(held_symbols[:5], _held_news, max_workers=5)
        held_news = {s: a for s, a in out.items() if a}
        record_supplement(
            "Supplement/SchwalpacaHeldNews",
            held_news,
            source_type="news",
            independence_group="schwalpaca-news",
        )
        if held_news:
            held_news_md = screener_news_to_markdown(held_news)
            if held_news_md:
                md += "\n" + held_news_md
                print(f"  Added news for {len(held_news)} held positions")

    # Fetch market movers (before screeners — screener candidate news uses movers)
    print("Fetching market movers...")
    movers = fetch_movers()
    record_supplement(
        "Supplement/SchwalpacaMovers",
        movers,
        source_type="market",
        independence_group="schwalpaca-market-data",
        market_derived=True,
    )
    held_set = set(held_symbols) if held_symbols else set()
    if movers:
        non_held = [m for m in movers if m.get('symbol') not in held_set]
        print(f"  {len(movers)} movers, {len(non_held)} non-held")
        movers_md = movers_to_markdown(movers, held_set)
        if movers_md:
            md += "\n" + movers_md
    else:
        print("  Movers unavailable")

    # Fetch market screeners
    print("Fetching market screeners...")
    screeners = fetch_screeners()
    record_supplement(
        "Supplement/SchwalpacaScreeners",
        screeners,
        source_type="market",
        independence_group="schwalpaca-market-data",
        market_derived=True,
    )
    if screeners:
        near_high = len(screeners.get("near_52w_high", []))
        unusual_vol = len(screeners.get("unusual_volume", []))
        momentum = len(screeners.get("strong_momentum", []))
        print(f"  Near 52w high: {near_high}, Unusual volume: {unusual_vol}, Momentum: {momentum}")
        screeners_md = screeners_to_markdown(screeners)
        if screeners_md:
            md += "\n" + screeners_md

        # Fetch news for screener + mover candidates (unified)
        held_set = set(held_symbols) if held_symbols else set()
        print("Fetching news for screener + mover candidates...")
        candidate_symbols = set()
        for key in ["near_52w_high", "unusual_volume", "strong_momentum"]:
            for item in screeners.get(key, []):
                sym = item.get("symbol")
                if sym and sym not in held_set:
                    candidate_symbols.add(sym)
        if movers:
            for m in movers[:10]:
                sym = m.get("symbol")
                if sym and sym not in held_set:
                    candidate_symbols.add(sym)
        if candidate_symbols:
            from_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            headers = {"X-API-Key": SCHWALPACA_API_KEY}

            def _cand_news(sym):
                try:
                    r = _session.get(
                        f"{SCHWALPACA_URL}/intel/news/{sym}",
                        headers=headers,
                        params={"from": from_date, "limit": 3},
                        timeout=_TIMEOUT,
                    )
                    if r.ok:
                        arts = r.json()
                        return arts if arts else None
                except Exception:
                    pass
                return None

            out = _parallel_fetch(list(candidate_symbols)[:12], _cand_news, max_workers=10)
            candidate_news = {s: a for s, a in out.items() if a}
            record_supplement(
                "Supplement/SchwalpacaCandidateNews",
                candidate_news,
                source_type="news",
                independence_group="schwalpaca-news",
            )
            if candidate_news:
                total_articles = sum(len(v) for v in candidate_news.values())
                print(f"  Found {total_articles} articles across {len(candidate_news)} tickers")
                candidate_news_md = screener_news_to_markdown(candidate_news)
                if candidate_news_md:
                    md += "\n" + candidate_news_md
            else:
                print("  No candidate news found")
        else:
            print("  No non-held screener/mover candidates")
    else:
        print("  Screeners unavailable")

    # Mover news & sentiment (movers already fetched above)
    mover_sentiments = {}
    if movers:
        # Mover news (catalyst layer)
        print("Fetching mover news (past 3 days)...")
        mover_news = fetch_mover_news(movers, held_set, days=3)
        record_supplement(
            "Supplement/SchwalpacaMoverNews",
            mover_news,
            source_type="news",
            independence_group="schwalpaca-news",
        )
        if mover_news:
            total_articles = sum(len(v) for v in mover_news.values())
            print(f"  Found {total_articles} articles across {len(mover_news)} tickers")
            mover_news_md = mover_news_to_markdown(mover_news)
            if mover_news_md:
                md += "\n" + mover_news_md
        else:
            print("  No mover news found")

        # Mover sentiment (narrative layer)
        print("Fetching mover sentiment...")
        mover_sentiments = fetch_mover_sentiment(movers, held_set)
        record_supplement(
            "Supplement/SchwalpacaMoverSentiment",
            mover_sentiments,
            source_type="news",
            independence_group="schwalpaca-social-sentiment",
        )
        if mover_sentiments:
            mover_sent_md = mover_sentiment_to_markdown(mover_sentiments)
            if mover_sent_md:
                md += "\n" + mover_sent_md

    # Screener sentiment (non-held screener tickers)
    if screeners:
        held_set = set(held_symbols) if held_symbols else set()
        already_fetched = set(mover_sentiments.keys())
        screener_syms = set()
        for key in ["near_52w_high", "unusual_volume", "strong_momentum"]:
            for item in screeners.get(key, []):
                sym = item.get("symbol")
                if sym and sym not in held_set and sym not in already_fetched:
                    screener_syms.add(sym)
        if screener_syms:
            print("Fetching screener sentiment...")
            headers = {"X-API-Key": SCHWALPACA_API_KEY}

            def _screener_sent(sym):
                try:
                    r = _session.get(
                        f"{SCHWALPACA_URL}/intel/social-sentiment/{sym}",
                        headers=headers,
                        timeout=60,
                    )
                    if r.ok:
                        data = r.json()
                        if data and not data.get("error"):
                            print(f"  ✓ {sym}: {data.get('overall', '?')}")
                            return data
                        print(f"  ✗ {sym}: {data.get('error', 'no data') if data else 'failed'}")
                except Exception as e:
                    print(f"  Warning: sentiment fetch failed for {sym}: {e}")
                return None

            out = _parallel_fetch(list(screener_syms)[:5], _screener_sent, max_workers=5)
            screener_sentiments = {s: d for s, d in out.items() if d}
            record_supplement(
                "Supplement/SchwalpacaScreenerSentiment",
                screener_sentiments,
                source_type="news",
                independence_group="schwalpaca-social-sentiment",
            )
            if screener_sentiments:
                screener_sent_md = mover_sentiment_to_markdown(screener_sentiments)
                if screener_sent_md:
                    md += "\n" + screener_sent_md

    # Technical indicators (held positions + screeners + top movers)
    print("Fetching technical indicators...")
    indicator_symbols = set()
    if held_symbols:
        for sym in held_symbols[:5]:
            indicator_symbols.add(sym)
    if screeners:
        for key in ["near_52w_high", "unusual_volume", "strong_momentum"]:
            for item in screeners.get(key, []):
                sym = item.get("symbol")
                if sym:
                    indicator_symbols.add(sym)
    if movers:
        for m in movers[:10]:
            sym = m.get('symbol')
            if sym:
                indicator_symbols.add(sym)
    if indicator_symbols:
        all_indicators = {}
        for sym in list(indicator_symbols)[:12]:
            ind = fetch_indicators(sym)
            if ind and not ind.get("error"):
                all_indicators[sym] = ind
                print(f"  ✓ {sym}")
            else:
                print(f"  ✗ {sym}")
        indicators_md = indicators_to_markdown(all_indicators)
        record_supplement(
            "Supplement/SchwalpacaIndicators",
            all_indicators,
            source_type="market",
            independence_group="schwalpaca-market-data",
            market_derived=True,
        )
        if indicators_md:
            md += "\n" + indicators_md
            print(f"  Added indicators for {len(all_indicators)} symbols")

    # Options flow for held positions + top screener/mover candidates
    print("Fetching options flow (put/call ratio + unusual activity)...")
    flow_symbols = set()
    if held_symbols:
        flow_symbols.update(held_symbols[:5] if isinstance(held_symbols, list) else list(held_symbols)[:5])
    if screeners:
        for key in ["near_52w_high", "unusual_volume", "strong_momentum"]:
            for item in screeners.get(key, []):
                sym = item.get("symbol")
                if sym:
                    flow_symbols.add(sym)
    if movers:
        for m in movers[:5]:
            sym = m.get("symbol")
            if sym:
                flow_symbols.add(sym)
    if flow_symbols:
        all_flows = {}
        for sym in list(flow_symbols)[:10]:
            flow = fetch_option_flow(sym)
            if flow:
                all_flows[sym] = flow
                print(f"  ✓ {sym}: P/C={flow.get('put_call_ratio_volume', '?')} [{flow.get('signal', '?')}]")
            else:
                print(f"  ✗ {sym}")
        flow_md = options_flow_to_markdown(all_flows)
        record_supplement(
            "Supplement/SchwalpacaOptionsFlow",
            all_flows,
            source_type="market",
            independence_group="schwalpaca-market-data",
            market_derived=True,
        )
        if flow_md:
            md += "\n" + flow_md
            print(f"  Added options flow for {len(all_flows)} symbols")
    else:
        print("  No symbols for options flow")

    # Earnings calendar (next 7 days)
    print("Fetching earnings calendar (next 7 days)...")
    earnings = fetch_earnings(days_ahead=7)
    record_supplement(
        "Supplement/SchwalpacaEarnings",
        earnings,
        source_type="market",
        independence_group="schwalpaca-market-data",
        market_derived=True,
    )
    if earnings:
        held_set_for_earnings = set(held_symbols) if held_symbols else set()
        earnings_md = earnings_to_markdown(earnings, held_set_for_earnings)
        if earnings_md:
            md += "\n" + earnings_md
            items = earnings if isinstance(earnings, list) else earnings.get("data", [])
            print(f"  {len(items)} earnings events in next 7 days")
        else:
            print("  No upcoming earnings")
    else:
        print("  Earnings data unavailable")

    try:
        manifest_fetched_at = _iso_or_none(sweep_ts)
    except ValueError:
        manifest_fetched_at = _now().isoformat()
    manifest = build_observation_manifest(
        crucix_data,
        supplements,
        report_id=f"pending-{_now():%Y%m%d}",
        fetched_at=manifest_fetched_at,
    )
    print(f"  Observation manifest: {len(manifest['observations'])} observations")
    md += "\n" + render_observation_reference_index(
        manifest,
        _polymarket_observation_labels(crucix_data),
    )

    # Write to temp file (or output dir for dry-run)
    if args.dry_run:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = _now().strftime("%Y%m%d_%H%M%S")
        md_path = OUTPUT_DIR / f"brief_{ts}.md"
        _atomic_write_text(md_path, md)
        manifest["report_id"] = f"brief_{ts}"
        manifest_path = OUTPUT_DIR / f"brief_{ts}.manifest.json"
        _atomic_write_json(
            manifest_path,
            manifest,
            validate_observation_manifest,
        )
        print(f"\n  Dry run — brief saved to: {md_path}")
        print(f"  Dry run — manifest saved to: {manifest_path}")
        print(f"\n--- Preview (first 2000 chars) ---\n")
        print(md[:2000])
        return

    # Write brief to stable path (survives crashes for --resume)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = _now().strftime("%Y%m%d")
    md_path = str(OUTPUT_DIR / f".brief_{today}.md")
    _atomic_write_text(Path(md_path), md)
    manifest_path = OUTPUT_DIR / f".observation_manifest_{today}.json"
    _atomic_write_json(
        manifest_path,
        manifest,
        validate_observation_manifest,
    )

    project_name = args.project_name or f"Crucix Trading Intel {sweep_ts[:10]}"
    run_pipeline(
        md_path,
        args.max_rounds,
        project_name,
        manifest_path=str(manifest_path),
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        # Module-level _session lives until process exit otherwise. For a single-shot
        # pipeline run this releases the keep-alive TCP connection immediately.
        _session.close()
