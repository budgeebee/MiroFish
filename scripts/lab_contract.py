"""Schwalpaca Phase 7 lab wire contracts (Generator side).

Pure stdlib mirror of the frozen Core conventions in
schwalpaca `backend/app/lab/contracts.py` (M6 handoff). No import from the
other repository: the shared canonical-JSON convention, the `lab-feedback.v1`
grammar, the `lab-proposals.v1` sidecar and the `hypothesis-spec.v1` fields are
frozen by the byte-identical cross-repo fixture `fixtures/lab-feedback.json`
(SHA-256 `8a7a936a016f3dbb38553692da28268e2786bb9d6ea726b69b93b41f10cc2a24`).

This module validates; it never executes a proposal, calls a model, or opens
a network/socket. All times are tz-aware ISO strings. Hashes are SHA-256 of
canonical JSON UTF-8 bytes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone

FIXTURE_SHA256 = '8a7a936a016f3dbb38553692da28268e2786bb9d6ea726b69b93b41f10cc2a24'

SELECTORS = ('liquidity', 'ranked')
CAPS = (10, 20, 30)
THRESHOLDS = ('0.0000', '0.0005', '0.0010')
FEATURE_IDS = [
    'crucix.acled.total_events.v1',
    'crucix.acled.total_fatalities.v1',
    'crucix.who.outbreak_items.v1',
    'crucix.ecb.deposit_rate.v1',
]
SPEC_FIELDS = frozenset(('schema', 'base_rule_sha256', 'selector', 'name_cap', 'cost_gate_threshold'))
UNCONDITIONAL = {'schema': 'context-predicate.v1', 'kind': 'unconditional'}
LAB_MARKER = '## Lab Proposals (Machine-Readable)'
MAX_FEEDBACK_BYTES = 128 * 1024
MAX_LAB_BLOCK_BYTES = 128 * 1024
MAX_PROPOSALS = 8


class LabError(ValueError):
    """Invalid lab wire content; never a model or trading signal."""


# ---------------------------------------------------------------------------
# Canonical JSON (frozen Core convention)
# ---------------------------------------------------------------------------

def finite_json(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            finite_json(item)
        return
    if isinstance(value, dict) and all(type(key) is str for key in value):
        for item in value.values():
            finite_json(item)
        return
    raise LabError('finite_json_required')


def canonical_json(value):
    """Sorted keys, compact separators, default ASCII escaping, finite only."""
    finite_json(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def canonical_sha256(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LabError('duplicate_json_key')
        result[key] = value
    return result


def parse_json(raw):
    """Strict JSON: duplicate keys rejected, finite values only."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode('utf-8')
        value = json.loads(raw, object_pairs_hook=_unique_keys)
        finite_json(value)
        return value
    except (ValueError, TypeError, RecursionError, UnicodeDecodeError) as exc:
        if isinstance(exc, LabError):
            raise
        raise LabError('invalid_json') from exc


# ---------------------------------------------------------------------------
# Field helpers (mirrors of the Core validators)
# ---------------------------------------------------------------------------

def exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise LabError(label + ':exact_fields_required')


def sha256_text(value, label):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise LabError(label + ':sha256_required')
    return value


def identifier(value, label):
    if type(value) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value):
        raise LabError(label + ':identifier_required')
    return value


def nonempty(value, label):
    if type(value) is not str or not value.strip():
        raise LabError(label + ':nonempty_text_required')


def timestamp(raw, label):
    if type(raw) is not str:
        raise LabError(label + ':timestamp_required')
    try:
        result = datetime.fromisoformat(raw)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except ValueError as exc:
        raise LabError(label + ':aware_timestamp_required') from exc


def _decimal_text(value, label):
    if type(value) is not str or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value):
        raise LabError(label + ':decimal_string_required')
    return value


def validate_spec(raw, base_rule_sha256):
    exact(raw, SPEC_FIELDS, 'strategy')
    finite_json(raw)
    if raw['schema'] != 'strategy-spec.v1':
        raise LabError('strategy_schema')
    if (type(base_rule_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', base_rule_sha256)
            or raw['base_rule_sha256'] != base_rule_sha256):
        raise LabError('base_rule_mismatch')
    if raw['selector'] not in SELECTORS or type(raw['selector']) is not str:
        raise LabError('unsupported_selector')
    if type(raw['name_cap']) is not int or raw['name_cap'] not in CAPS:
        raise LabError('unsupported_name_cap')
    if type(raw['cost_gate_threshold']) is not str or raw['cost_gate_threshold'] not in THRESHOLDS:
        raise LabError('unsupported_cost_gate_threshold')
    return dict(raw)


def validate_context(value):
    """Unconditional or one frozen above-median predicate (validation shape only)."""
    finite_json(value)
    if value == UNCONDITIONAL:
        return copy.deepcopy(value)
    exact(value, ('schema', 'kind', 'feature_id', 'status', 'median', 'usable_discovery_count',
                  'requested_discovery_count', 'discovery_sha256', 'predicate_sha256'), 'context')
    body = {key: item for key, item in value.items() if key != 'predicate_sha256'}
    if (type(value['usable_discovery_count']) is not int
            or type(value['requested_discovery_count']) is not int
            or not 0 <= value['usable_discovery_count'] <= value['requested_discovery_count']):
        raise LabError('invalid_context_counts')
    if (canonical_sha256(body) != value['predicate_sha256']
            or value['schema'] != 'context-predicate.v1'
            or value['kind'] != 'above_discovery_median'
            or value['feature_id'] not in FEATURE_IDS):
        raise LabError('context_identity_mismatch')
    if value['status'] == 'not_testable' and value['median'] is None and value['usable_discovery_count'] < 20:
        return copy.deepcopy(value)
    if value['status'] != 'ready' or value['usable_discovery_count'] < 20:
        raise LabError('invalid_context_status')
    _decimal_text(value['median'], 'context.median')
    return copy.deepcopy(value)


def validate_proposal(raw, context):
    """`hypothesis-spec.v1` against the epoch identities carried by feedback."""
    fields = ('schema', 'hypothesis_id', 'parent_experiment_id', 'source_report_id',
              'source_hypothesis_id', 'claim', 'falsifiers', 'strategy_spec',
              'context_predicate', 'expected_sign', 'epoch_id', 'input_manifest_sha256',
              'evaluation_policy_sha256', 'code_commit')
    exact(raw, fields, 'hypothesis')
    finite_json(raw)
    if raw['schema'] != 'hypothesis-spec.v1':
        raise LabError('invalid_hypothesis_schema')
    for key in ('hypothesis_id', 'source_report_id', 'source_hypothesis_id'):
        identifier(raw[key], key)
    if raw['parent_experiment_id'] is not None:
        sha256_text(raw['parent_experiment_id'], 'parent_experiment_id')
    nonempty(raw['claim'], 'claim')
    if not isinstance(raw['falsifiers'], list) or not raw['falsifiers']:
        raise LabError('falsifiers_required')
    for value in raw['falsifiers']:
        nonempty(value, 'falsifier')
    for key in ('epoch_id', 'input_manifest_sha256', 'evaluation_policy_sha256', 'code_commit'):
        if raw[key] != context[key]:
            raise LabError(key + ':epoch_mismatch')
    validate_spec(raw['strategy_spec'], context['base_rule_sha256'])
    predicate = validate_context(raw['context_predicate'])
    if predicate not in context['discovery']['contexts']:
        raise LabError('context_not_frozen_in_discovery')
    if raw['expected_sign'] not in ('positive', 'negative'):
        raise LabError('invalid_expected_sign')
    return copy.deepcopy(raw)


# ---------------------------------------------------------------------------
# `lab-feedback.v1`
# ---------------------------------------------------------------------------

def validate_feedback(value):
    """D8 shared wire schema; protected metrics or paths cannot appear here."""
    exact(value, ('schema', 'generated_at', 'epoch_id', 'ledger_head_sha256', 'lifetime_trial_count',
                  'proposal_context', 'experiments'), 'feedback')
    finite_json(value)
    if value['schema'] != 'lab-feedback.v1':
        raise LabError('invalid_feedback_schema')
    timestamp(value['generated_at'], 'generated_at')
    sha256_text(value['ledger_head_sha256'], 'ledger_head_sha256')
    if type(value['lifetime_trial_count']) is not int or value['lifetime_trial_count'] < 0:
        raise LabError('invalid_lifetime_count')
    context = value['proposal_context']
    exact(context, ('base_rule_sha256', 'input_manifest_sha256', 'evaluation_policy_sha256', 'code_commit',
                    'admission_closes_at', 'admission_state', 'allowed_strategy_values', 'feature_ids',
                    'frozen_contexts'), 'proposal_context')
    if context['admission_state'] not in ('open', 'closed', 'collect_only'):
        raise LabError('invalid_admission_state')
    if context['admission_state'] == 'collect_only':
        if value['epoch_id'] is not None or any(context[key] is not None for key in
                ('base_rule_sha256', 'input_manifest_sha256', 'evaluation_policy_sha256', 'code_commit',
                 'admission_closes_at')):
            raise LabError('collect_only_has_epoch')
    else:
        identifier(value['epoch_id'], 'epoch_id')
        for key in ('base_rule_sha256', 'input_manifest_sha256', 'evaluation_policy_sha256'):
            sha256_text(context[key], key)
        if type(context['code_commit']) is not str or not re.fullmatch('[0-9a-f]{40}', context['code_commit']):
            raise LabError('full_code_commit_required')
        timestamp(context['admission_closes_at'], 'admission_closes_at')
    exact(context['allowed_strategy_values'], ('selector', 'name_cap', 'cost_gate_threshold'),
          'allowed_strategy_values')
    if context['allowed_strategy_values'] != {'selector': list(SELECTORS), 'name_cap': list(CAPS),
                                              'cost_gate_threshold': list(THRESHOLDS)}:
        raise LabError('feedback_grammar_mismatch')
    if context['feature_ids'] != FEATURE_IDS:
        raise LabError('feedback_features_mismatch')
    if not isinstance(context['frozen_contexts'], list):
        raise LabError('frozen_contexts_required')
    for predicate in context['frozen_contexts']:
        validate_context(predicate)
    if not isinstance(value['experiments'], list) or len(value['experiments']) > 50:
        raise LabError('feedback_limit')
    for row in value['experiments']:
        exact(row, ('experiment_id', 'hypothesis_id', 'source_report_id', 'source_hypothesis_id',
                    'spec_sha256', 'status', 'reason_codes', 'paired_session_count',
                    'mean_paired_net_difference', 'next_allowed_action'), 'feedback_item')
        for key in ('experiment_id', 'spec_sha256'):
            if row[key] is not None:
                sha256_text(row[key], key)
        for key in ('hypothesis_id', 'source_report_id', 'source_hypothesis_id'):
            if row[key] is not None:
                identifier(row[key], key)
        if row['status'] not in ('rejected', 'duplicate', 'failed', 'insufficient_data', 'testable_discovery'):
            raise LabError('protected_or_unknown_feedback_status')
        if type(row['paired_session_count']) is not int or row['paired_session_count'] < 0:
            raise LabError('invalid_paired_count')
        if row['mean_paired_net_difference'] is not None:
            _decimal_text(row['mean_paired_net_difference'], 'mean')
        if not isinstance(row['reason_codes'], list) or any(type(reason) is not str for reason in row['reason_codes']):
            raise LabError('invalid_feedback_reasons')
        if row['next_allowed_action'] not in ('revise_in_open_epoch', 'wait_for_next_epoch', 'none'):
            raise LabError('invalid_feedback_action')
    return copy.deepcopy(value)


def feedback_proposal_context(feedback):
    """Epoch identity view of validated feedback for proposal validation."""
    context = feedback['proposal_context']
    manifest = {key: context[key] for key in
                ('base_rule_sha256', 'input_manifest_sha256', 'evaluation_policy_sha256', 'code_commit')}
    manifest['epoch_id'] = feedback['epoch_id']
    manifest['discovery'] = {'contexts': context['frozen_contexts']}
    return manifest


def validate_proposal_sidecar(value, scenario, feedback):
    """D8 pair binding after the existing scenario validator accepted `scenario`.

    `feedback` is the validated pinned feedback object or None (unavailable).
    """
    exact(value, ('schema', 'report_id', 'generated_at', 'scenario_sha256', 'feedback_sha256',
                  'status', 'reason_codes', 'proposals'), 'proposal_sidecar')
    finite_json(value)
    if (value['schema'] != 'lab-proposals.v1' or value['report_id'] != scenario['report_id']
            or value['scenario_sha256'] != canonical_sha256(scenario)):
        raise LabError('sidecar_scenario_identity')
    timestamp(value['generated_at'], 'sidecar.generated_at')
    if not isinstance(value['reason_codes'], list) or any(type(item) is not str for item in value['reason_codes']):
        raise LabError('invalid_sidecar_reasons')
    if not isinstance(value['proposals'], list) or len(value['proposals']) > MAX_PROPOSALS:
        raise LabError('sidecar_proposal_limit')
    if value['status'] not in ('ready', 'no_proposals') or (value['status'] == 'ready') != bool(value['proposals']):
        raise LabError('sidecar_status_mismatch')
    if feedback is None:
        if value['feedback_sha256'] is not None or value['proposals']:
            raise LabError('sidecar_feedback_unavailable')
        return copy.deepcopy(value)
    validate_feedback(feedback)
    if value['feedback_sha256'] != canonical_sha256(feedback):
        raise LabError('sidecar_feedback_identity')
    context = feedback['proposal_context']
    if context['admission_state'] != 'open' and value['proposals']:
        raise LabError('sidecar_admission_closed')
    manifest = feedback_proposal_context(feedback)
    ids = {row['hypothesis_id'] for row in scenario['hypotheses']}
    seen = set()
    for proposal in value['proposals']:
        validate_proposal(proposal, manifest)
        if (proposal['source_report_id'] != value['report_id']
                or proposal['source_hypothesis_id'] not in ids
                or proposal['hypothesis_id'] in seen):
            raise LabError('sidecar_hypothesis_identity')
        seen.add(proposal['hypothesis_id'])
    return copy.deepcopy(value)


# ---------------------------------------------------------------------------
# Normal-path extraction: `## Lab Proposals (Machine-Readable)` + one fence
# ---------------------------------------------------------------------------

def extract_lab_proposals(markdown):
    """Locate exactly one lab proposal block in the complete report markdown.

    Returns the parsed envelope. Raises LabError with a reason code on
    absent/ambiguous/oversized/malformed content; the caller maps those to
    `no_proposals` diagnostics, never to a scenario retry.
    """
    if not isinstance(markdown, str):
        raise LabError('lab_block_not_text')
    count = markdown.count(LAB_MARKER)
    if count == 0:
        raise LabError('lab_block_absent')
    if count > 1:
        raise LabError('lab_block_ambiguous')
    block = markdown[markdown.index(LAB_MARKER) + len(LAB_MARKER):]
    if len(block.encode('utf-8')) > MAX_LAB_BLOCK_BYTES:
        raise LabError('lab_block_oversize')
    match = re.search(r'```(?:json)?\s*(.+?)\s*```', block, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        raise LabError('lab_block_fence_missing')
    if re.search(r'```', block[match.end():]):
        raise LabError('lab_block_trailing_fence')
    # One complete value, then only whitespace: trailing objects are refused.
    stripped = match.group(1).strip()
    try:
        value, end = json.JSONDecoder(object_pairs_hook=_unique_keys).raw_decode(stripped)
    except LabError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LabError('lab_block_json_invalid') from exc
    if stripped[end:].strip():
        raise LabError('lab_block_trailing_object')
    if not isinstance(value, dict):
        raise LabError('lab_block_not_object')
    finite_json(value)
    return value


def parse_repair_bundle(text):
    """Strict lab-enabled repair response: one wrapper object, nothing else.

    Returns (kind, value): kind is 'bundle' for exactly {scenario, lab_proposals},
    or 'legacy_flat' for a single object without the wrapper keys (the caller
    applies the existing scenario path and records no_proposals).
    """
    if not isinstance(text, str) or not text.strip():
        raise LabError('lab_repair_empty')
    stripped = text.strip()
    fence = re.fullmatch(r'```(?:json)?\s*(\{.*?\})\s*```', stripped, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1).strip()
    try:
        value, end = json.JSONDecoder(object_pairs_hook=_unique_keys).raw_decode(stripped)
    except LabError:
        raise
    except json.JSONDecodeError as exc:
        raise LabError('lab_repair_json_invalid') from exc
    if stripped[end:].strip():
        raise LabError('lab_repair_trailing_object')
    if not isinstance(value, dict):
        raise LabError('lab_repair_not_object')
    finite_json(value)
    if set(value) == {'scenario', 'lab_proposals'}:
        return 'bundle', value
    if 'lab_proposals' not in value and isinstance(value.get('hypotheses'), list):
        return 'legacy_flat', value
    raise LabError('lab_repair_wrapper_keys')


def build_sidecar(report_id, generated_at, scenario, feedback, status, reason_codes, proposals):
    """Assemble a `lab-proposals.v1` envelope with canonical identities."""
    if status not in ('ready', 'no_proposals'):
        raise LabError('invalid_sidecar_status')
    if (status == 'ready') != bool(proposals):
        raise LabError('sidecar_status_mismatch')
    sidecar = {'schema': 'lab-proposals.v1', 'report_id': report_id, 'generated_at': generated_at,
               'scenario_sha256': canonical_sha256(scenario),
               'feedback_sha256': None if feedback is None else canonical_sha256(feedback),
               'status': status, 'reason_codes': list(reason_codes), 'proposals': proposals}
    return validate_proposal_sidecar(sidecar, scenario, feedback)
