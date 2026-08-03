# MiroFish Phase 2 Plan — Exact Market Attribution and Signed Direction

**Status:** READY FOR EXECUTION

**Planned:** 2026-08-02

**Planner snapshot:** `f24a6c04f4e42084acadbc000a28cdc836a572df` (`main`, one commit ahead of `origin/main`)

**Workflow:** `~/Projects/PLANNING.md`

**Incoming request:** `REQUEST-trading-intelligence-phase6.md`

**Next milestone:** P2-M3 only

This is the MiroFish Phase 2 plan. The incoming document is a request from
trading-intelligence Phase 6; it is not itself an implementation contract.
Executors follow this file, one milestone per session.

## Mission

Make MiroFish's structured hypotheses precisely joinable and objectively
scorable against individual Polymarket contracts without changing its role into
a probability forecaster or trade recommendation service.

Phase 2 has two producer-side outputs:

1. A hypothesis can cite an observation whose `source_id` contains the exact
   Polymarket `venueContractId`, rather than citing the Polymarket feed as a
   whole.
2. For every cited exact Polymarket contract, MiroFish emits its own signed
   implication for that contract's YES outcome: `-1`, `0`, or `+1`.

The first output fixes identity. The second removes a downstream human/agent
annotation confound from trading-intelligence's lead/lag experiment.

## Done means

- [ ] Direct Crucix Polymarket markets shown to MiroFish are represented by
      individual `observation.v1` entries whose `source_id` is exactly
      `Crucix/Polymarket/<venueContractId>` and whose `payload_ref` identifies
      the exact source array element.
- [ ] The selected population is the stable, first-seen union of `top[:10]`
      followed by `highProbShifts[:5]`, deduplicated by `venueContractId`.
- [ ] The existing aggregate `Crucix/Polymarket` observation remains in the
      manifest for whole-source provenance, but a hypothesis may not cite that
      aggregate in `market_observation_ids` when exact contract observations
      exist.
- [ ] The prompt-visible Observation Reference Index maps each exact contract
      observation ID to the contract question and YES semantics, so the model
      can select an ID from meaning rather than guess from a hash.
- [ ] Every `scenario-synthesis.v1` hypothesis contains
      `market_directions`, a list of exact objects shaped as
      `{"observation_id": "obs-...", "direction": -1|0|1}`.
- [ ] `market_directions` is complete and ordered for the hypothesis's exact
      direct-Polymarket IDs, contains no other observation IDs, and accepts an
      integer `0` as a first-class abstention.
- [ ] Prompt, extraction, constrained repair, deterministic builder, local
      validation, Markdown rendering, fixture bundle, and daily freshness
      verification all enforce the same contract.
- [ ] No probability, direction confidence, trade recommendation, new API
      endpoint, permissions change, timer change, or consumer-side annotation
      is introduced.
- [ ] The first naturally scheduled post-change live report passes the strict
      verifier, contains at least one exact Polymarket hypothesis/market pair,
      and is visible verbatim through the existing loopback endpoints.
- [ ] Phase 2 closeout is recorded in this plan, `TODO.md`, and `CLAUDE.md` with
      real command output and the live report ID.

## Explicitly not in this phase

- No implementation in `/home/irvins/Projects/trading-intelligence`,
  Schwalpaca, Kalshi, Crucix, or any other repository.
- No scoring, lead/lag window, null model, independence correction, sample-size
  rule, experiment conclusion, or promotion decision. Those belong to
  trading-intelligence Phase 6.
- No backfill or mutation of previously captured reports or append-only
  snapshots. Old source-level snapshots remain historical facts.
- No probability or confidence number for a market outcome. Existing
  `confidence` continues to describe evidence quality only.
- No per-claim direction. Direction is only a property of a
  `(hypothesis, exact Polymarket observation)` pair.
- No direction for YFinance, equities, options, screeners, Adanos sentiment,
  or other market-derived observations that do not have a binary YES contract.
- No expansion from the detailed Polymarket arrays MiroFish actually renders to
  all `totalRelevant` markets. The current payload reports more relevant markets
  than it provides as detailed `top`/`highProbShifts` entries.
- No new endpoint, sidecar artifact, schema-version string, permissions, Docker
  user, service, timer, dependency, or model selection.
- No refactor of the broader 3,500-line pipeline, no general schema framework,
  and no abstraction for hypothetical future venues.
- No deletion or cleanup of `output/` or `output/recovery/`.

## Lane definition

This phase uses one lane because manifest construction, prompt semantics,
scenario validation, and their fixtures form one exact contract and touch the
same core files. Splitting them would manufacture a merge boundary inside a
single invariant.

### Lane S — Scenario Contract

Owns:

- `scripts/crucix_to_mirofish.py`
- `scripts/verify_pipeline_contract.py`
- `scripts/verify_daily_freshness.py`
- `fixtures/scenario-input.json`
- `fixtures/scenario-expected-shape.json`
- `PHASE2_PLAN.md` (status, Verify logs, Amendments, and handoffs only)
- `TODO.md`
- `CLAUDE.md` (phase handoff only)

Read-only context:

- `REQUEST-trading-intelligence-phase6.md`
- `api_server.py`
- `docker-compose.yml`
- `ops/systemd/mirofish-daily.service`
- `ops/systemd/mirofish-daily.timer`
- `/home/irvins/Projects/Crucix/runs/latest.json`
- `/home/irvins/Projects/trading-intelligence/src/tradingintel/mirofish_snapshot.py`

No parallel milestone execution is allowed. P2-M1, P2-M2, and P2-M3 serialize.

## Decisions locked

### D1 — Granular population is exactly what reaches the evidence brief

For direct Crucix `sources.Polymarket`, enumerate:

1. `top[:10]`, in payload order;
2. then `highProbShifts[:5]`, in payload order;
3. keep the first occurrence of each non-empty string `venueContractId`.

The current live payload has 30 `top` entries and 10 `highProbShifts`; all first
five high-probability entries also occur in the first ten `top` entries. The
algorithm therefore emits ten exact observations today, not fifteen, while
remaining correct when those lists stop overlapping.

Do not sort contract IDs. List order is evidence/presentation order and is the
deterministic tie-breaker for duplicate entries. If a selected entry lacks a
non-empty string `venueContractId`, fail manifest construction with a precise
validation error. Do not silently emit an unjoinable pseudo-contract.

If neither selected array contains a market, retain only the legacy aggregate
observation; this preserves unavailable/degraded source evidence without
claiming exact market coverage that does not exist.

### D2 — Granular observation encoding uses the existing schema

Each selected market produces an existing `observation.v1` object; do not add
fields to `OBSERVATION_FIELDS` and do not change
`observation-manifest.v1`/`observation.v1`.

Exact values:

```text
source_id          Crucix/Polymarket/<venueContractId>
payload_ref        /sources/Polymarket/top/<zero-based-index>
                 or /sources/Polymarket/highProbShifts/<zero-based-index>
source_type        market
independence_group inherited from item, payload, or health; fallback polymarket
market_derived     true
status             direct Polymarket source-health status
observed_at        item.observedAt, falling back to item.observed_at, else null
fetched_at         item.fetchedAt, then payload.fetchedAt/payload.timestamp,
                   then the Crucix sweep timestamp
content_sha256     canonical hash of the individual market object
observation_id     existing content-addressed _observation() result
```

The full source-level `Crucix/Polymarket` observation remains exactly one
additional observation, hashing the complete source payload. This preserves
whole-source provenance and degradation status. It is not a contract identity.

### D3 — Aggregate Polymarket is provenance-only once granular IDs exist

When a manifest contains any `source_id` beginning
`Crucix/Polymarket/`, `validate_scenario_synthesis` must reject the observation
whose `source_id` is exactly `Crucix/Polymarket` if it appears in any
hypothesis's `market_observation_ids`.

The prompt must give the same instruction. The aggregate remains visible in the
manifest and reference index but is labeled aggregate/provenance-only. Other
market-derived sources retain their current behavior.

This is what turns Ask 1 into an exact join rather than merely adding exact IDs
while allowing the model to keep citing the old ambiguous one.

### D4 — The model receives an explicit ID-to-question catalog

The current reference index exposes observation ID and source ID but not the
market question. Exact hashes and contract IDs alone are not enough for the
model to choose the semantically correct market.

Add a small deterministic label mapping for selected Polymarket entries and
pass it into `render_observation_reference_index`. Each granular index line must
contain:

- the exact observation ID;
- the exact `Crucix/Polymarket/<venueContractId>` source ID;
- the full market question with embedded whitespace normalized to spaces;
- the fact that direction refers to the contract's YES outcome;
- the end date when present.

Do not place the question in the persisted observation schema. It remains in
the source payload and prompt-visible reference catalog; consumers join via the
stable `venueContractId` encoded in `source_id`.

The repair prompt must split the Observation Reference Index from the rest of
the brief before applying the existing brief truncation. Preserve the complete
index, then truncate only the non-index brief body. The existing 8,000-character
body cap and 6,000-character report cap remain unchanged.

Helper names and punctuation are DISCRETION; the data and preservation behavior
above are LOCKED.

### D5 — Signed direction is additive inside `scenario-synthesis.v1`

Add `market_directions` to `HYPOTHESIS_FIELDS`, but keep the top-level schema
string `scenario-synthesis.v1` and the existing endpoint
`/output/today/structured`.

This is an intentional additive compatibility decision:

- no existing field is removed or reinterpreted;
- the requesting snapshotter stores structured JSON verbatim and explicitly
  tolerates upstream additive/schema changes;
- trading-intelligence's current fleet health check pins the endpoint to
  `scenario-synthesis.v1`;
- Schwalpaca consumes the legacy Markdown endpoints, not this exact hypothesis
  field set;
- repo audit found no external consumer that exact-validates the hypothesis key
  set.

Do not modify `api_server.py`, endpoint paths, `legacy_consumer_diff()`, or
schema-version checks in this phase. If implementation discovers an exact-field
external consumer not found by the audit, stop and file an Amendment; do not
silently choose v2.

### D6 — Exact `market_directions` shape and semantics

Every hypothesis contains the field, including hypotheses with no exact
Polymarket contract:

```json
"market_directions": [
  {"observation_id": "obs-...", "direction": -1},
  {"observation_id": "obs-...", "direction": 0},
  {"observation_id": "obs-...", "direction": 1}
]
```

Semantics:

```text
 1  the hypothesis claim implies the contract's YES outcome becomes more likely
-1  the hypothesis claim implies the contract's YES outcome becomes less likely
 0  the claim/contract relationship is genuinely directionally indeterminate
```

`0` is an abstention, not an error and not neutral market movement. A model must
not be forced to select a non-zero sign.

For each hypothesis, derive `directional_ids` by filtering
`market_observation_ids`, in existing list order, to observations whose
`source_id` begins `Crucix/Polymarket/`. Validation requires:

1. `market_observation_ids` contains no duplicate exact Polymarket ID.
2. `market_directions` is a list.
3. Every entry is a dict with exactly `observation_id` and `direction`.
4. Direction uses `type(value) is int` and belongs to `{-1, 0, 1}`; JSON
   booleans are rejected even though Python treats `bool` as an `int` subclass.
5. Direction entries' observation IDs, in order, equal `directional_ids`.
6. No aggregate Polymarket, Adanos, equity, option, screener, or other
   non-binary market observation receives a direction entry.

An unrelated contract is omitted from `market_observation_ids`. Use direction
`0` when the hypothesis deliberately cites a relevant contract but cannot imply
a sign for its YES outcome.

### D7 — Generation and repair may not invent a sign locally

Update both `SIMULATION_REQUIREMENT` and `_scenario_repair_prompt` with the exact
field, pairing, and sign semantics from D6. Tell the model to cite only exact
direct-Polymarket IDs for direct Polymarket claims and to use `0` freely.

`build_scenario_synthesis` may normalize fixture/draft direction entries from a
known `source_id` to an observation ID, matching its existing source-ID
convenience for evidence lists. It may not infer direction from claim text.

`extract_scenario_synthesis` continues to overwrite only top-level metadata. A
missing/invalid direction makes extraction fail. Constrained repair receives
the validation message and may retry twice as today. Repair code must never
default, keyword-map, copy, flip, or otherwise synthesize a direction. If two
model attempts cannot produce a valid pairing, publication fails closed.

### D8 — Human-readable output exposes the producer's sign

In `## What the crowd currently prices`, render each exact Polymarket contract
pair with a direct label:

```text
direction=UP (+1)
direction=DOWN (-1)
direction=ABSTAIN (0)
```

Non-directional market observations keep their existing presentation. Do not
rewrite the hypothesis claim or preserved simulation narrative.

### D9 — Contract verification is deterministic and adversarial

Extend existing fixtures; do not introduce a new test framework. Tests must
cover at least:

- per-market source IDs and exact payload refs;
- selection caps and first-seen deduplication across `top` and
  `highProbShifts`;
- individual-market hashing determinism;
- retained aggregate provenance;
- aggregate direct Polymarket rejection from `market_observation_ids`;
- question/YES labeling in the reference index;
- full reference-index preservation through repair-prompt truncation;
- valid `-1`, `0`, and `+1` pairs;
- empty directions when there is no exact Polymarket ID;
- missing, extra, reordered, duplicated, dangling, aggregate, and non-market
  direction IDs;
- invalid `-2`, `2`, string, float, null, and boolean directions;
- repair retry after a pairing failure;
- Markdown UP/DOWN/ABSTAIN rendering;
- existing legacy endpoint and provenance invariants.

`scripts/verify_daily_freshness.py` must apply the same complete pairing checks
to fixture and live artifacts and return these read-only summary counts:

```text
granular_polymarket_observations
directional_pairs
directional_abstentions
```

The live verifier must require at least one granular Polymarket observation and
at least one hypothesis/contract pair. P2-M2 exercises that rule with a fixture;
P2-M3 applies it to the first new live report. Zero live pairs is a
prompt/integration divergence, not a successful canary.

### D10 — Deployment uses the natural schedule

Do not trigger an extra full simulation merely to accelerate verification. The
existing timer already authorizes a daily run at 21:05 America/Tijuana. P2-M3
waits for the first naturally scheduled report generated after P2-M2 lands,
then verifies it.

The existing API on loopback port 5010 remains the supported consumer path. The
scripts directory is bind-mounted into `mirofish-api`; no image rebuild or API
restart is planned because neither dependency nor API server code changes.

If runtime reality contradicts that deployment assumption, stop and record the
exact service/container state in Amendments before changing infrastructure.

## Ambiguity tolerance

### LOCKED

- All decisions D1-D10.
- One lane and serial milestone order.
- Exact contract-ID source prefix and payload-ref shapes.
- Retention but non-citability of aggregate direct Polymarket provenance.
- Additive `market_directions` inside `scenario-synthesis.v1`.
- Direction is signed YES implication for a hypothesis/contract pair.
- `0` is a valid abstention.
- No local inference/default of direction.
- No new endpoint, permission, timer, dependency, consumer mutation, or backfill.
- Natural scheduled live canary rather than an extra paid run.

### DISCRETION

- Private helper names inside `scripts/crucix_to_mirofish.py`.
- Exact validation error wording, provided tests assert the relevant concept.
- Test helper organization inside the existing verifier.
- Whitespace and punctuation of reference-index and Markdown direction labels,
  provided IDs, question, YES meaning, and numeric sign remain explicit.
- Whether fixture direction drafts name a source ID directly or are normalized
  to observation IDs in the verifier before calling the builder. Record the
  chosen fixture technique in the P2-M2 Verify log.

### CHECKPOINT

None at plan publication. The user has already asked to plan both incoming asks,
and this plan does not widen permissions, APIs, operational cost, or trading
authority.

The following discoveries create a new checkpoint and require a planner/user
decision rather than executor improvisation:

- satisfying Ask 2 requires a schema-version bump or a new endpoint;
- an external exact-field consumer would break on `market_directions`;
- the live producer cannot map questions to exact IDs without persisting a new
  observation field;
- a full unscheduled simulation is required;
- implementation must modify another repository.

## User checkpoints

No current checkpoints.

## Repository inventory snapshot

Audit time: 2026-08-02. Code snapshot is
`f24a6c04f4e42084acadbc000a28cdc836a572df`.

At audit:

- `git status --short` was empty.
- `main` was one commit ahead of `origin/main` solely because `f24a6c0`
  records the incoming request.
- No `PHASE1_PLAN.md`, `TODO.md`, or lane handoff document existed in this repo.
- Existing deterministic contract and freshness fixtures passed.
- No Phase 2 implementation existed.

Planning-session documentation changes (`PHASE2_PLAN.md`, `TODO.md`, and the
phase handoff in `CLAUDE.md`) are the only allowed difference from the code
snapshot before P2-M1 begins. P2-M1 must stop if any implementation file differs
from `f24a6c0` before it starts.

| Path | Audit state | Phase use |
|---|---|---|
| `PHASE2_PLAN.md` | to be created by planner | contracts, logs, amendments |
| `TODO.md` | to be created by planner | running Done/Next list |
| `CLAUDE.md` | exists | refresh handoff at plan start and closeout |
| `REQUEST-trading-intelligence-phase6.md` | exists | read-only scope source |
| `scripts/crucix_to_mirofish.py` | exists | M1/M2 implementation |
| `scripts/verify_pipeline_contract.py` | exists | M1/M2 deterministic contract tests |
| `scripts/verify_daily_freshness.py` | exists | M2 fixture + M3 live contract |
| `fixtures/scenario-input.json` | exists | M1/M2 fixture input |
| `fixtures/scenario-expected-shape.json` | exists | M1/M2 expected contract |
| `api_server.py` | exists | read-only; endpoint already serves artifact verbatim |
| `docker-compose.yml` | exists | read-only deployment evidence |
| `ops/systemd/mirofish-daily.service` | exists | read-only schedule evidence |
| `ops/systemd/mirofish-daily.timer` | exists | read-only schedule evidence |

Relevant current code facts:

- `HYPOTHESIS_FIELDS` is an exact set and currently ends with
  `market_observation_ids`.
- direct Polymarket currently falls through to one source-level observation;
  Adanos already has per-section handling.
- `build_scenario_synthesis` resolves source IDs to content-addressed
  observation IDs.
- `validate_scenario_synthesis` rejects any hypothesis field difference and
  segregates market-derived evidence.
- the repair prompt truncates the brief to 8,000 characters and report to 6,000
  characters.
- publication writes manifest and structured JSON before Markdown discovery.
- `api_server.py` validates only top-level schema/report identity before serving
  JSON; no API change is necessary.

## Capability playbook

1. **The live payload has overlap.** On 2026-08-02, Polymarket carried 30
   `top` and 10 `highProbShifts`; the first five shifts duplicated contracts in
   `top[:10]`. Deduplicate by `venueContractId` before calling `_observation`, or
   duplicate observation IDs will fail manifest validation.
2. **Observation IDs are content-addressed.** They change when the individual
   market payload changes. Downstream identity comes from the stable
   `venueContractId` in `source_id`, not from treating `observation_id` as a
   stable cross-day key.
3. **Question mapping must survive repair truncation.** The reference index is
   appended at the end of the brief. Preserve it explicitly; simply adding
   market observations to `ALLOWED OBSERVATIONS` does not teach the model which
   question each hash means.
4. **Python booleans pass naive integer membership.** Use
   `type(direction) is int`, not `isinstance(direction, int)` or only
   `direction in {-1, 0, 1}`.
5. **Do not infer signs during repair.** Existing repair moves misplaced market
   evidence between lists. A moved exact Polymarket ID without a matching
   direction must cause retry/failure; relocation cannot manufacture a sign.
6. **Market-derived is broader than prediction market.** Supplements for
   equities, screeners, indicators, options, and earnings are market-derived.
   Only the exact direct-Polymarket source prefix has binary YES semantics.
7. **Keep the aggregate.** Removing `Crucix/Polymarket` would discard full-feed
   provenance and unnecessarily change manifest continuity. Make it
   non-citable for hypotheses when granular IDs exist instead.
8. **Source health is authoritative.** Preserve the direct source-health status
   and independence group on every child observation; all Polymarket children
   remain one `polymarket` crowd-information family.
9. **The live API serves only today.** Do not use older report files as a live
   canary. Match Markdown, manifest, and structured `report_id` from port 5010.
10. **Old output is sensitive and recoverable.** Do not chmod, delete, rewrite,
    or backfill historical artifacts. Consumers use the loopback API.
11. **Current baseline commands pass:**

    ```text
    python3 scripts/verify_pipeline_contract.py
    PASS: zero-byte, undersized, invalid UTF-8, partial, valid, atomic-write,
    stale/current checkpoint, idempotent skip, observation provenance,
    scenario synthesis, schema rejection, and artifact endpoint fixtures

    python3 scripts/verify_daily_freshness.py --fixture
    {"fixture": true, "report_id": "prediction_20260727_000500", "status": "ok"}
    ```

## Dependency graph

```text
P2-M1  exact Polymarket observation identity
   |
   v
P2-M2  signed hypothesis/contract direction contract
   |
   v
P2-M3  naturally scheduled live canary and phase closeout
```

No milestones may run in parallel.

## Milestone contract — P2-M1: Exact Polymarket observation identity

**Lane:** S — Scenario Contract

**Owner:** one executor session

**Prerequisites:** plan documentation is the only diff from `f24a6c0`; baseline
contract tests pass; no active pipeline/checkpoint is being modified.

### Exact files

Modify:

- `scripts/crucix_to_mirofish.py`
- `scripts/verify_pipeline_contract.py`
- `fixtures/scenario-input.json`
- `fixtures/scenario-expected-shape.json`
- `PHASE2_PLAN.md` (M1 status and Verify log only)
- `TODO.md`

Do not modify any other file in P2-M1.

### Implementation contract

1. Add one small iterator/helper implementing D1's exact selection, cap,
   ordering, ID validation, payload-ref generation, and deduplication.
2. In `build_observation_manifest`, keep the existing aggregate direct
   Polymarket observation and additionally emit one child observation per
   selected market using D2's exact values.
3. Derive a prompt-only label mapping for those child source IDs. Extend
   `render_observation_reference_index` to render D4's question, YES semantics,
   and optional end date while leaving non-child index lines compatible.
4. Preserve the complete reference index separately when
   `_scenario_repair_prompt` truncates the rest of the brief.
5. Update `SIMULATION_REQUIREMENT` and the repair prompt to require exact child
   IDs for direct Polymarket claims and forbid the aggregate direct source when
   children exist. Do not add `market_directions` yet.
6. Update scenario validation so the aggregate direct Polymarket observation is
   rejected from `market_observation_ids` only when granular direct Polymarket
   observations exist. Do not change behavior for other market-derived IDs.
7. Expand deterministic fixtures per D9. The primary fixture must contain at
   least one child contract cited by a hypothesis; dynamic test variants must
   prove the 10/5 caps and first-seen deduplication without bloating the static
   fixture unnecessarily.
8. Preserve every existing test and compatibility assertion. Do not weaken
   exact field validation or market/non-market evidence separation.
9. Run Verify, paste real output below, check P2-M1 complete, update `TODO.md`,
   and commit only this milestone as:

   ```text
   P2-M1 add contract-level Polymarket observations
   ```

10. Stop. Do not begin signed directions in the same session.

### Verify

Run:

```bash
python3 scripts/verify_pipeline_contract.py
python3 scripts/verify_daily_freshness.py --fixture
python3 -m py_compile scripts/crucix_to_mirofish.py scripts/verify_pipeline_contract.py scripts/verify_daily_freshness.py api_server.py
git diff --check
git status --short
```

Expected:

- contract verifier exits 0 and its PASS line explicitly includes granular
  Polymarket identity/deduplication and aggregate-citation rejection;
- freshness fixture remains status `ok` under the pre-direction contract;
- compilation and `git diff --check` are silent with exit 0;
- `git status --short` lists only the six P2-M1 files above before commit;
- after the focused commit, worktree is clean.

Named artifact:

- `/tmp/mirofish-p1-m3-cp0/prediction_cp0_fixture.manifest.json` contains at
  least one `Crucix/Polymarket/<venueContractId>` observation and exactly one
  aggregate `Crucix/Polymarket` observation.

### Stop conditions

- Live/static Crucix entries do not provide `venueContractId`, `question`, or
  the documented array shapes.
- Exact identity requires an observation schema/version change.
- The aggregate cannot be made non-citable without changing an endpoint.
- An implementation file already differs from `f24a6c0` before M1 begins.
- Existing deterministic tests fail for a reason unrelated to M1.

Record the divergence in Amendments and halt.

## Milestone contract — P2-M2: Signed hypothesis/contract direction

**Lane:** S — Scenario Contract

**Owner:** one fresh executor session

**Prerequisites:** P2-M1 is checked, committed, and clean; its Verify log and
latest handoff have been read.

### Exact files

Modify:

- `scripts/crucix_to_mirofish.py`
- `scripts/verify_pipeline_contract.py`
- `scripts/verify_daily_freshness.py`
- `fixtures/scenario-input.json`
- `fixtures/scenario-expected-shape.json`
- `PHASE2_PLAN.md` (M2 status and Verify log only)
- `TODO.md`

Do not modify any other file in P2-M2.

### Implementation contract

1. Add `market_directions` to `HYPOTHESIS_FIELDS` without changing the
   `scenario-synthesis.v1` string or top-level artifact fields.
2. Extend deterministic builder output so every hypothesis contains the field.
   Normalize only explicit fixture/draft direction records; do not derive a
   sign from a claim.
3. Implement D6's exact ordered pairing validation, including strict integer
   type, complete set/order equality, and non-Polymarket exclusion.
4. Update `SIMULATION_REQUIREMENT` and `_scenario_repair_prompt` with the
   exact JSON shape, YES semantics, the meaning of all three values, the
   unrelated-vs-abstain distinction, and the instruction to use `0` freely.
5. Keep extraction metadata behavior unchanged. Ensure constrained repair
   retries a missing/invalid pairing and never fills a direction locally.
6. Update `render_scenario_markdown` with D8's UP/DOWN/ABSTAIN labels for exact
   direct-Polymarket pairs only.
7. Extend the static fixture to exercise `-1`, `0`, and `+1` across hypotheses;
   hypotheses without exact contracts must carry an empty list.
8. Add every adversarial D9 direction case to the existing verifier. Preserve
   all P2-M1 and Phase 1 cases.
9. Extend `verify_daily_freshness.py` fixture/live validation and result counts
   exactly as D9 specifies. Its fixture must contain enough complete artifacts
   to exercise the new invariant rather than only top-level schemas.
10. Run Verify, paste real output below, check P2-M2 complete, update `TODO.md`,
    and commit only this milestone as:

    ```text
    P2-M2 add signed Polymarket direction pairs
    ```

11. Stop. Do not trigger an unscheduled live pipeline run.

### Verify

Run:

```bash
python3 scripts/verify_pipeline_contract.py
python3 scripts/verify_daily_freshness.py --fixture
python3 -m py_compile scripts/crucix_to_mirofish.py scripts/verify_pipeline_contract.py scripts/verify_daily_freshness.py api_server.py
git diff --check
git status --short
```

Expected:

- contract verifier exits 0 and its PASS line names signed market directions,
  abstention, invalid-type rejection, ordered pairing, and repair retry;
- freshness fixture prints status `ok` with positive
  `granular_polymarket_observations`, positive `directional_pairs`, and at least
  one `directional_abstention`;
- CP0 structured JSON retains `scenario-synthesis.v1`; every hypothesis has
  `market_directions`; fixture values cover `-1`, `0`, and `+1`;
- compilation and `git diff --check` are silent with exit 0;
- `git status --short` lists only the seven P2-M2 files above before commit;
- after the focused commit, worktree is clean.

Named artifacts:

- `/tmp/mirofish-p1-m3-cp0/prediction_cp0_fixture.json`
- `/tmp/mirofish-p1-m3-cp0/prediction_cp0_fixture.manifest.json`
- `/tmp/mirofish-p1-m3-cp0/prediction_cp0_fixture.md`

### Stop conditions

- A discovered consumer requires the hypothesis field set to remain exactly as
  pre-Phase-2 v1.
- Correct direction pairing requires a schema bump, new endpoint, new persisted
  observation field, or another repository change.
- Repair can pass only by inventing/defaulting a sign.
- P2-M1 is not committed and clean.
- Existing tests regress outside the explicitly changed contract.

Record the divergence in Amendments and halt.

## Milestone contract — P2-M3: Scheduled live canary and closeout

**Lane:** S — Scenario Contract

**Owner:** one fresh executor/monitor session

**Prerequisites:** P2-M2 is checked, committed, and clean; code is present on the
host before the 21:05 America/Tijuana timer; no manual paid run is authorized.

### Exact files

Modify only after the live canary passes:

- `PHASE2_PLAN.md` (M3 status, real Verify log, handoff)
- `TODO.md`
- `CLAUDE.md`

No implementation file changes are allowed in P2-M3. If code must change, stop
and amend/replan rather than hiding a hotfix inside closeout.

### Implementation contract

1. Confirm the timer is enabled/active and wait for the first naturally
   scheduled report whose generation is after P2-M2 landed. Do not call `/run`,
   `/resume`, or the pipeline directly to force an extra report.
2. Run the strict live verifier. Require matching report IDs, at least one exact
   Polymarket observation, at least one direction-bearing
   hypothesis/contract pair, and complete valid pairing for every exact cited
   contract.
3. Read `/output/today/manifest` and `/output/today/structured` only through the
   supported loopback API as needed to record the report ID and counts. Do not
   read or chmod root-owned artifacts directly.
4. Re-run deterministic contract and freshness fixtures after the live report.
5. Read-only, from `/home/irvins/Projects/trading-intelligence`, run its MiroFish
   snapshot status command after the 22:07 capture window. Record whether
   `last_report_id` matches the live report. Do not write that repository. A
   missing automatic capture is an external handoff/blocker, not permission to
   run or alter its collector.
6. If all MiroFish checks pass, mark Phase 2 closed in `CLAUDE.md`, move all
   tasks to Done in `TODO.md`, append the live evidence below, and commit only
   closeout documentation as:

   ```text
   P2-M3 verify live market direction contract
   ```

7. Stop.

### Verify

Run:

```bash
python3 scripts/verify_daily_freshness.py --live
python3 scripts/verify_pipeline_contract.py
python3 scripts/verify_daily_freshness.py --fixture
systemctl --user status mirofish-daily.timer --no-pager
systemctl --user list-timers --all --no-pager
PYTHONPATH=src python3 -m tradingintel.mirofish_snapshot status --format json
git diff --check
git status --short
```

The snapshot-status command runs with working directory
`/home/irvins/Projects/trading-intelligence`; all other commands run from the
MiroFish repository.

Expected:

- live verifier exits 0 and returns one report ID with
  `granular_polymarket_observations >= 1`, `directional_pairs >= 1`, and
  `directional_abstentions >= 0`;
- deterministic verifier and freshness fixture exit 0;
- timer is enabled/active and lists its next trigger;
- consumer status is read successfully and its `last_report_id` matches the
  live report after its scheduled capture window;
- before closeout commit, `git status --short` lists only the three M3 docs;
- after commit, worktree is clean.

Named evidence:

- live report ID from the verifier;
- live exact-observation, pair, and abstention counts;
- matching trading-intelligence snapshot `last_report_id`;
- pasted command output in the P2-M3 Verify log.

### Stop conditions

- The scheduled report predates P2-M2 or the timer did not run.
- Zero exact contract observations or zero direction-bearing pairs are emitted.
- Any cited exact Polymarket ID lacks exactly one valid direction, or any
  direction points elsewhere.
- Live publication falls back twice and fails, report IDs diverge, an active
  process/checkpoint remains, or source freshness fails.
- A code change, service restart, new run, permissions change, or external repo
  write appears necessary.

Record the divergence in Amendments and halt. Do not mark the phase complete.

## Executor contract

1. Read `CLAUDE.md`, this entire plan, the newest Verify log, Amendments,
   handoffs, and `TODO.md`. Run `git status` first.
2. Confirm implementation files match the plan snapshot plus completed focused
   milestone commits. If not, stop and record divergence.
3. Claim Lane S and execute exactly one milestone literally. Do not build ahead,
   combine milestones, skip Verify, or improvise product/schema behavior.
4. On divergence:
   - **LOCKED** — stop, append a dated note to Amendments, and halt.
   - **DISCRETION** — choose minimally and record choice/rationale in Verify.
   - **CHECKPOINT** — stop for the user/planner.
5. Verify with the named commands/artifacts and paste real output, not a
   paraphrase.
6. Close out the one milestone: check its status, append a dated Verify log,
   update `TODO.md`, append any handoff, make the specified focused commit, and
   stop.
7. Never mutate other repositories, old output, permissions, services, timers,
   or external consumers unless a later approved amendment explicitly expands
   scope.

## Milestone status

- [x] P2-M1 — Exact Polymarket observation identity
- [x] P2-M2 — Signed hypothesis/contract direction
- [ ] P2-M3 — Scheduled live canary and closeout

## Verify log

### Planner baseline — 2026-08-02

```text
Snapshot:
  f24a6c04f4e42084acadbc000a28cdc836a572df
Branch:
  main...origin/main [ahead 1]
Worktree before planning docs:
  clean

python3 scripts/verify_pipeline_contract.py
  PASS: zero-byte, undersized, invalid UTF-8, partial, valid, atomic-write,
  stale/current checkpoint, idempotent skip, observation provenance,
  scenario synthesis, schema rejection, and artifact endpoint fixtures
  CP0 bundle: /tmp/mirofish-p1-m3-cp0

python3 scripts/verify_daily_freshness.py --fixture
  {"fixture": true, "report_id": "prediction_20260727_000500", "status": "ok"}

python3 -m py_compile scripts/crucix_to_mirofish.py
  scripts/verify_pipeline_contract.py scripts/verify_daily_freshness.py api_server.py
  exit 0, no output

Live Crucix shape (read-only audit):
  sweep timestamp: 2026-08-02T16:21:45.749Z
  Polymarket health: ok, sourceType=market,
    independenceGroup=polymarket, marketDerived=true
  totalRelevant=41, top=30, highProbShifts=10
  top[:10] union highProbShifts[:5] unique contracts=10
  all five inspected highProbShifts duplicate top[:10]

Consumer audit:
  tradingintel.mirofish_snapshot stores both API bodies verbatim and its tests
  explicitly accept future/additive upstream schemas. trading-intelligence's
  fleet check currently requires scenario-synthesis.v1. No Schwalpaca/Kalshi
  exact hypothesis-field consumer was found.
```

### P2-M1 — 2026-08-02 — PASS

```text
Commit target:
  P2-M1 add contract-level Polymarket observations

Implementation:
  - Added stable top[:10] + highProbShifts[:5] selection with first-seen
    venueContractId deduplication and fail-closed malformed-row validation.
  - Emitted child observation.v1 entries with exact source IDs, payload refs,
    per-market hashes/timestamps, and shared Polymarket provenance while
    retaining the aggregate exactly once.
  - Made aggregate direct Polymarket provenance non-citable whenever child
    contracts exist; aggregate-only fallback remains valid for empty arrays.
  - Added prompt-only question/YES/end-date labels and preserved the complete
    Observation Reference Index outside repair-prompt body truncation.

DISCRETION:
  Private helpers are `_selected_polymarket_markets` and
  `_polymarket_observation_labels`. The static fixture carries one top market,
  one overlapping shift, and one non-overlapping shift; a generated fixture
  proves the full 10/5 caps without adding eleven repetitive rows to JSON.

python3 scripts/verify_pipeline_contract.py
  PASS: zero-byte, undersized, invalid UTF-8, partial, valid, atomic-write,
  stale/current checkpoint, idempotent skip, observation provenance, granular
  Polymarket identity/deduplication, aggregate-citation rejection, scenario
  synthesis, schema rejection, and artifact endpoint fixtures
  CP0 bundle: /tmp/mirofish-p1-m3-cp0

python3 scripts/verify_daily_freshness.py --fixture
  {"fixture": true, "report_id": "prediction_20260727_000500", "status": "ok"}

python3 -m py_compile scripts/crucix_to_mirofish.py
  scripts/verify_pipeline_contract.py scripts/verify_daily_freshness.py api_server.py
  exit 0, no output

git diff --check
  exit 0, no output

Named CP0 manifest inspection:
  report_id=prediction_cp0_fixture
  observation_count=10
  granular source/payload pairs:
    Crucix/Polymarket/fixture-contract
      /sources/Polymarket/top/0
    Crucix/Polymarket/fixture-shift-contract
      /sources/Polymarket/highProbShifts/1
  aggregate_count=1

Read-only current Crucix shape probe:
  {"aggregate": 1, "granular": 10, "index_chars": 9132, "labeled": 10}

Files before closeout commit:
  M PHASE2_PLAN.md
  M TODO.md
  M fixtures/scenario-expected-shape.json
  M fixtures/scenario-input.json
  M scripts/crucix_to_mirofish.py
  M scripts/verify_pipeline_contract.py

No live pipeline, service, timer, API, permission, historical output, or other
repository was changed. P2-M2 was not started.
```

### P2-M2 — 2026-08-02 — PASS

```text
Commit target:
  P2-M2 add signed Polymarket direction pairs

Implementation:
  - Added required market_directions lists without changing the
    scenario-synthesis.v1 envelope or endpoint contract.
  - Normalized only explicit fixture/draft source IDs to observation IDs; no
    claim-text inference, default, copy, or sign synthesis was introduced.
  - Enforced exact ordered pairing for granular direct-Polymarket citations,
    strict integer {-1,0,1} values with booleans rejected, and no direction for
    aggregate or other market/non-market observations.
  - Added exact YES-outcome semantics to generation and repair prompts, exposed
    UP/DOWN/ABSTAIN in Markdown, and made freshness verification return exact
    granular-observation, pair, and abstention counts.

DISCRETION:
  The static fixture uses explicit `source_id` direction records, which the
  deterministic builder converts to content-addressed observation IDs. This
  keeps fixture intent readable while validating the persisted exact shape.

python3 scripts/verify_pipeline_contract.py
  PASS: zero-byte, undersized, invalid UTF-8, partial, valid, atomic-write,
  stale/current checkpoint, idempotent skip, observation provenance, granular
  Polymarket identity/deduplication, aggregate-citation rejection, signed
  market directions, abstention, invalid-type rejection, ordered pairing,
  repair retry, scenario synthesis, schema rejection, and artifact endpoint
  fixtures
  CP0 bundle: /tmp/mirofish-p1-m3-cp0

python3 scripts/verify_daily_freshness.py --fixture
  {"checkpoint_current": false, "directional_abstentions": 1,
  "directional_pairs": 2, "fixture": true,
  "granular_polymarket_observations": 2, "pipeline_running": false,
  "report_date": "20260727", "report_id": "prediction_20260727_000500",
  "report_size_bytes": 4096, "source_age_seconds": 600, "status": "ok"}

python3 -m py_compile scripts/crucix_to_mirofish.py
  scripts/verify_pipeline_contract.py scripts/verify_daily_freshness.py api_server.py
  exit 0, no output

git diff --check
  exit 0, no output

Named CP0 structured/Markdown inspection:
  schema=scenario-synthesis.v1
  every hypothesis has market_directions=true
  direction values=[1,-1,0]
  Markdown labels=UP (+1), DOWN (-1), ABSTAIN (0)

Files before closeout commit:
  M PHASE2_PLAN.md
  M TODO.md
  M fixtures/scenario-expected-shape.json
  M fixtures/scenario-input.json
  M scripts/crucix_to_mirofish.py
  M scripts/verify_daily_freshness.py
  M scripts/verify_pipeline_contract.py

No live pipeline, service, timer, API, permission, historical output, or other
repository was changed. P2-M3 was not started.
```

### P2-M3 — 2026-08-03 — MIROFISH PASS; DOWNSTREAM CAPTURE PENDING

```text
Repair implementation:
  - Routed constrained repair through local ai_backend using
    deepseek-v4-flash, thinking disabled, JSON-object mode, and 8192 output
    tokens. The provider credential remains inside ai_backend.
  - Forwarded the allowlisted DeepSeek controls in ai_backend commit cda1dfa.
  - Added keyless private-gateway routing and the explicit
    http://host.docker.internal:9400 container URL.

Checkpoint resume:
  PID 15 reused sim_e539fd57be89 / report_48529846ff56.
  Both allowed semantic repair attempts returned JSON through ai_backend; the
  second passed unchanged strict validation.
  Published report_id=prediction_20260803_130651.
  Checkpoint cleared; no pipeline process remains.

python3 scripts/verify_daily_freshness.py --live
  {"checkpoint_current": false, "directional_abstentions": 15,
  "directional_pairs": 15, "granular_polymarket_observations": 10,
  "pipeline_running": false, "report_date": "20260803",
  "report_id": "prediction_20260803_130651",
  "report_size_bytes": 39665, "source_age_seconds": 141, "status": "ok"}

python3 scripts/verify_pipeline_contract.py
  PASS: zero-byte, undersized, invalid UTF-8, partial, valid, atomic-write,
  stale/current checkpoint, idempotent skip, observation provenance, granular
  Polymarket identity/deduplication, aggregate-citation rejection, signed
  market directions, abstention, invalid-type rejection, ordered pairing,
  ai_backend DeepSeek JSON repair, sanitized response diagnostics, repair
  retry, scenario synthesis, schema rejection, and artifact endpoint fixtures
  CP0 bundle: /tmp/mirofish-p1-m3-cp0

python3 scripts/verify_daily_freshness.py --fixture
  {"checkpoint_current": false, "directional_abstentions": 1,
  "directional_pairs": 2, "fixture": true,
  "granular_polymarket_observations": 2, "pipeline_running": false,
  "report_date": "20260727", "report_id": "prediction_20260727_000500",
  "report_size_bytes": 4096, "source_age_seconds": 600, "status": "ok"}

systemd:
  mirofish-daily.timer loaded, enabled, active (waiting)
  next MiroFish run: 2026-08-03 21:05 PDT
  next trading-intelligence snapshot: 2026-08-03 22:07 PDT

Current downstream status (expected pending before capture):
  {"first_report_id":"prediction_20260802_005827",
  "last_report_id":"prediction_20260802_044807",
  "schema":"mirofish-snapshot.v1","snapshots":2,"total_hypotheses":6}

Remaining gate:
  After the scheduled 22:07 PDT capture, require trading-intelligence
  last_report_id=prediction_20260803_130651. Then update the three closeout
  docs, mark P2-M3 complete, and make the contracted final closeout commit.
```

## Cross-session handoffs

### Planner to P2-M1 executor — 2026-08-02

- Start from `f24a6c0` plus planning documentation only.
- The aggregate observation is deliberately retained but becomes
  provenance-only for direct Polymarket hypothesis citations.
- Current live `highProbShifts[:5]` fully overlaps `top[:10]`; fixture tests
  must still cover a non-overlapping shift.
- P2-M1 ends before `market_directions` begins.

### P2-M1 to P2-M2 executor — 2026-08-02

- P2-M1 is verified and committed; begin only from its clean commit.
- Exact direct-Polymarket IDs are the manifest observations whose `source_id`
  begins `Crucix/Polymarket/`. The exact aggregate source remains present but
  validation rejects it from hypotheses whenever child IDs exist.
- `render_observation_reference_index` now requires the prompt-only label map
  when granular IDs exist; runtime and fixtures both pass it explicitly.
- Repair prompt truncation preserves the complete reference index. Do not
  regress that behavior while adding `market_directions`.
- Current live payload produces 10 unique labeled child contracts plus one
  aggregate. P2-M2 must not run a live pipeline.

### P2-M2 to P2-M3 executor — 2026-08-02

- P2-M2 is verified and committed; begin only from its clean commit.
- The v1 structured artifact now requires one ordered `market_directions` entry
  for every exact direct-Polymarket citation and rejects all other targets.
- The strict freshness verifier now requires at least one granular observation
  and one complete hypothesis/contract pair and reports pair/abstention counts.
- Wait for the first naturally scheduled 21:05 America/Tijuana report generated
  after P2-M2. Do not trigger an extra run or change runtime infrastructure.

### P2-M3 repair to downstream capture executor — 2026-08-03

- MiroFish live publication now passes as `prediction_20260803_130651`: 10
  exact Polymarket observations, 15 directional pairs, and 15 abstentions.
- The strict live, deterministic, fixture, process/checkpoint, and timer gates
  passed. Do not run or resume MiroFish again.
- P2-M3 remains open only because the external 22:07 PDT trading-intelligence
  snapshot has not run yet. After that window, read its status and require
  `last_report_id=prediction_20260803_130651`; do not write that repository.
- If it matches, update only `PHASE2_PLAN.md`, `TODO.md`, and `CLAUDE.md`, then
  make the original closeout commit. If it does not, record the external
  handoff/blocker and halt.

## Amendments

### A1 — 2026-08-02 — P2-M3: first scheduled live canary failed publication

Reality:
The enabled/active timer fired at 21:05 PDT and launched a fresh pipeline after
P2-M2. At 22:19 PDT the API reported no `20260803` report, no running process,
and a current checkpoint at completed step 6 updated at 22:08 PDT. The current
pipeline log shows a completed simulation (`sim_77c000f70603`, report
`report_e0b9d81f5623`) whose live report omitted the scenario-synthesis marker.
Both constrained repair attempts used `writer-qwen3.6-27b`; publication then
failed closed because the second response contained invalid JSON at line 38,
column 34. API logs also show an external `POST /resume` before this audit; this
executor did not trigger it. That resume reached the same two-attempt repair
failure. The 22:07 trading-intelligence capture ran, but its latest snapshot
remains the prior `prediction_20260802_044807` rather than a new report.

Decision class:
LOCKED. P2-M3 explicitly stops on failed publication, a current checkpoint, or
the need for a code change/resume/new run.

Impact:
The first naturally scheduled post-P2-M2 report does not exist, so live exact
contract observations, direction pairs, API identity, and downstream capture
cannot be verified. P2-M3 and Phase 2 remain open.

Executor action:
Recorded read-only API, systemd, container-log, and downstream snapshot evidence
and halted. Did not call `/run`, `/resume`, restart a service, modify code or
runtime infrastructure, mutate output, or write another repository.

Planner/user resolution required:
Decide whether to amend M3 to permit a targeted repair/retry investigation or
wait for another naturally scheduled canary. Do not mark Phase 2 complete from
the prior report.

### A2 — 2026-08-02 — P2-M3: authorize one targeted prompt repair and resume

Reality:
The user authorized "targeted repair and retry." Audit identified an invalid
JSON pseudo-example introduced by P2-M2 in both generation prompts:
`{"direction":-1|0|1}`. The local repair failure occurred while parsing JSON,
and the scripts directory is bind-mounted into `mirofish-api`, so this prompt
can be corrected without rebuilding or restarting infrastructure. The completed
simulation and step-6 checkpoint remain available.

Decision class:
User-approved amendment to the locked P2-M3 stop condition.

Impact:
P2-M3 may modify only `scripts/crucix_to_mirofish.py`,
`scripts/verify_pipeline_contract.py`, and the three existing M3 closeout docs.
Replace the pseudo-example with valid JSON examples, add a deterministic
regression assertion, run the local contract checks, then call `/resume`
exactly once against the existing checkpoint. Do not start a new simulation,
restart a service, change a model, relax validation, or add JSON repair logic.

Executor action:
Proceed with the narrow prompt correction and one checkpoint resume. If
publication or live verification fails again, record the new exact failure and
halt. If it passes, run all original M3 gates and close Phase 2 in one focused
commit named `P2-M3 repair and verify live market direction contract`.

Planner/user resolution required:
Resolved by the user's explicit authorization at 22:19 PDT.

### A3 — 2026-08-02 — P2-M3: targeted checkpoint retry also failed publication

Reality:
The invalid `-1|0|1` pseudo-JSON was replaced with three valid direction-object
examples in both prompts, and the deterministic contract/freshness/compile
checks passed. The single authorized `/resume` launched PID 43 and reused the
completed `sim_77c000f70603` simulation and `report_e0b9d81f5623`; no new
simulation ran. The embedded report still lacked the machine-readable marker.
Both local `writer-qwen3.6-27b` constrained-repair attempts returned, but the
second response was still invalid JSON: `Unterminated string starting at: line
31 column 9 (char 1176)`. The worker exited, no `20260803` report was published,
and the current step-6 checkpoint remains.

Decision class:
LOCKED under A2. A2 permits exactly one checkpoint resume and explicitly
forbids another run, model change, validation relaxation, or new JSON-repair
logic in this executor session.

Impact:
The prompt defect was real but not sufficient to make this local model produce
valid scenario JSON reliably. P2-M3 and Phase 2 remain open; none of the live
direction or downstream identity gates can pass without a published report.

Executor action:
Retained the valid prompt correction and its regression assertion, recorded the
failed retry, and halted. Did not issue a second resume, start a simulation,
change models, relax the schema, restart infrastructure, mutate output, or
write another repository.

Planner/user resolution required:
Replan the publication-repair boundary before another run. Likely options are a
more reliable configured repair backend or a separately contracted,
deterministic JSON-syntax repair layer that still passes the existing semantic
validator; neither is authorized by A2.

### A4 — 2026-08-02 — P2-M3: switch scenario repair to MiniMax M3

Reality:
The user rejected local-AI repair and explicitly selected MiniMax M3. Runtime
inspection confirms `LLM_MODEL_NAME=MiniMax-M3`,
`LLM_BASE_URL=https://api.minimax.io/v1`, and a present primary API key. The
current function always selects local `writer-qwen3.6-27b`; remote backends are
disabled and, even when enabled, are reached only on transport failure rather
than locally invalid JSON.

Decision class:
User-approved amendment to A3's publication-repair boundary.

Impact:
Replace the scenario-repair backend list with the configured primary MiniMax
endpoint/model only. Add a deterministic test proving the endpoint, model,
temperature, authorization header presence, and absence of local routing. Keep
the prompt, two-attempt semantic-validation loop, schema, and fail-closed
behavior unchanged. After local verification, permit exactly one more `/resume`
against the existing completed checkpoint; do not start a new simulation.

Executor action:
Proceed with the backend switch, deterministic verification, and one checkpoint
resume. If publication succeeds, run the original M3 live/downstream gates and
close Phase 2. If it fails, record the exact result and halt.

Planner/user resolution required:
Resolved by the user's explicit MiniMax M3 selection.

### A5 — 2026-08-02 — P2-M3: MiniMax M3 returned no JSON object

Reality:
The scenario repair path was switched exclusively to the configured primary
MiniMax endpoint and a deterministic fixture proved the endpoint, model,
authorization, temperature, timeout, and absence of local routing. All local
contract checks passed. The single A4-authorized `/resume` launched PID 86 and
reused `sim_77c000f70603` / `report_e0b9d81f5623`. Both repair calls were
received from `primary (MiniMax-M3)`, but neither response contained a JSON
object, ending with `scenario repair returned no JSON object`. The worker
exited, no report was published, and the step-6 checkpoint remains current.

Decision class:
LOCKED under A4. A4 permits exactly one MiniMax checkpoint resume and requires
halt on another publication failure.

Impact:
The user's backend choice is now honored and local repair is removed, but
MiniMax M3's current response shape/content does not satisfy this JSON-only
repair call. P2-M3 and Phase 2 remain open.

Executor action:
Retained the verified MiniMax-only routing, recorded the failed retry, and
halted. Did not issue another resume, start a simulation, relax parsing or
semantic validation, change models again, restart infrastructure, mutate
output, or write another repository.

Planner/user resolution required:
Inspect a protected raw MiniMax response and its finish metadata before another
retry, then contract the smallest response-extraction or request-format fix.
Do not guess whether the response is prose, reasoning-only, or another API
message field from the current error alone.

### A6 — 2026-08-03 — P2-M3: switch repair to enforced Kimi K3 JSON mode

Reality:
The user selected Kimi K3 and asked whether it can force JSON. A minimal live
probe against the already configured Moonshot endpoint used model `kimi-k3`
with `response_format={"type":"json_object"}` and returned HTTP 200,
`finish_reason=stop`, and content `{"value": 1}`. This directly proves JSON
object mode is accepted for this account/model/endpoint combination.

Decision class:
User-approved amendment to A5's repair backend and request format.

Impact:
Switch scenario repair exclusively to `LLM_BOOST_BASE_URL` with the configured
Moonshot key, exact model `kimi-k3`, temperature `1`, and
`response_format={"type":"json_object"}`. Update the deterministic routing
fixture to prove those values and absence of local/MiniMax routing. Preserve the
same prompt, two-attempt semantic validator, and fail-closed publication. After
local verification, permit exactly one `/resume` against the existing completed
checkpoint; do not start a new simulation.

Executor action:
Proceed with the enforced-JSON K3 switch and one checkpoint resume. If
publication succeeds, run the MiroFish live gates immediately and wait for the
next authorized downstream snapshot capture before closeout. If publication
fails, record the exact result and halt.

Planner/user resolution required:
Resolved by the user's explicit Kimi K3 selection and the successful live
capability probe.

### A7 — 2026-08-03 — P2-M3: user prefers strict MiniMax M3 configuration

Reality:
While the A6 Kimi checkpoint worker (PID 130) was still running, the user
reversed the model choice and asked to try MiniMax M3 with strict prompt
engineering and the proper model temperature. The executor stopped PID 130
before changing routing; it published no report, and the step-6 checkpoint
remains intact. Official MiniMax M-series guidance recommends temperature `1`
for reasoning models, while A4 used `0.1`; A4 also used deprecated
`max_tokens` and did not separate reasoning content.

Decision class:
User-approved replacement of A6 before A6 produced a publication result.

Impact:
Route repair exclusively to configured MiniMax M3 with temperature `1`,
`max_completion_tokens=2048`, and `reasoning_split=true`. Strengthen the system
message to require one raw JSON object beginning with `{` and ending with `}`
and forbid fences, introductory prose, explanations, chain-of-thought, and
`<think>` tags. Do not claim API-enforced JSON Schema support, which the
MiniMax M-series endpoint does not document. Update the deterministic fixture
for the complete request contract. Preserve the two-attempt semantic validator
and fail-closed behavior.

Executor action:
Apply and locally verify the strict MiniMax configuration, then permit exactly
one checkpoint resume under the user's "let's try" authorization. Do not start
a new simulation or change the repair contract again in this executor session.

Planner/user resolution required:
Resolved by the user's strict MiniMax M3 configuration request and retry intent.

### A8 — 2026-08-03 — P2-M3: strict M3 config works in probe but full repair shape differs

Reality:
The strict M3 request passed deterministic checks and the one A7-authorized
resume launched PID 145 against cached `sim_e539fd57be89` /
`report_48529846ff56`. Publication failed before a repair candidate was parsed:
`scenario repair backends failed: primary: KeyError`. No report was published
and the step-6 checkpoint remains. A subsequent minimal diagnostic call using
the exact endpoint, model, temperature `1`, `max_completion_tokens=2048`, and
`reasoning_split=true` returned HTTP 200, `base_resp.status_code=0`,
`finish_reason=stop`, message fields including `content` and reasoning fields,
and a 12-character JSON object. Therefore these settings are accepted and can
produce JSON; the full production request returned a different structure that
the current direct indexing obscured.

Decision class:
LOCKED under A7. Its single checkpoint resume is exhausted; further production
calls or response-extraction changes require a new amendment.

Impact:
Strict M3 configuration is retained and local verification passes, but P2-M3
and Phase 2 remain open. The failure can no longer be attributed simply to
temperature, deprecated token parameters, or unsupported reasoning separation.

Executor action:
Recorded the full-repair failure and sanitized diagnostic evidence and halted.
Did not issue another resume, change models, relax validation, restart
infrastructure, mutate output, or write another repository.

Planner/user resolution required:
Instrument `_request_scenario_repair` to report sanitized response keys,
`base_resp`, finish reason, and content/reasoning lengths on missing fields (and
optionally save a protected raw failure response), then make one observed retry.
Do not guess at another M3 configuration change before seeing that evidence.

### A9 — 2026-08-03 — P2-M3: authorize sanitized M3 response diagnostics

Reality:
The user authorized the A8 diagnostic-and-retry recommendation. The current
backend directly indexes `choices[0].message.content` and collapses missing
fields to the string `KeyError`, which is insufficient to distinguish an empty
choice, policy response, alternate message field, or API status body.

Decision class:
User-approved amendment to A8's response-extraction boundary.

Impact:
Add sanitized shape metadata for MiniMax responses: top-level keys, base status,
model, choice count, finish reason, message keys, content type/length/object
presence, reasoning type/length/count, output-sensitivity flags, and usage keys.
Never log the prompt, credentials, raw content, or raw reasoning. Include this
metadata in missing/empty/non-object response errors and preserve the existing
strict semantic validator. Add a deterministic no-leak regression fixture,
then permit one checkpoint resume. Do not change model/configuration again.

Executor action:
Implement and locally verify sanitized diagnostics, then resume the existing
completed checkpoint exactly once. If publication succeeds, run MiroFish live
gates; if it fails, record the now-specific metadata and halt.

Planner/user resolution required:
Resolved by the user's explicit "ok do that" authorization.

### A10 — 2026-08-03 — P2-M3: M3 exhausts completion budget on reasoning

Reality:
The A9 sanitized diagnostics passed their no-leak fixture and the single
instrumented resume launched PID 160 against cached `sim_e539fd57be89` /
`report_48529846ff56`. The MiniMax response was HTTP-successful with one choice,
`base_resp.status_code=0`, and no sensitivity flag, but ended with
`finish_reason=length`. Its message had no `content` field, contained one
`reasoning_details` block and 7,901 reasoning characters, and used the configured
2,048-token completion budget before producing a final answer. No report was
published and the step-6 checkpoint remains.

Decision class:
LOCKED under A9. Its single observed retry is exhausted.

Impact:
The failure is now diagnosed: strict M3 did not return malformed JSON or an
alternate final field; its reasoning consumed the full completion budget, so
there was no final content to parse. The prompt and semantic validator were
never exercised against a candidate JSON object. P2-M3 and Phase 2 remain open.

Executor action:
Retained the sanitized no-leak diagnostics and strict M3 configuration,
recorded the exact response metadata, and halted. Did not issue another resume,
change the completion budget/reasoning mode, relax validation, restart
infrastructure, mutate output, or write another repository.

Planner/user resolution required:
Amend the M3 request to provide enough completion budget for reasoning plus the
final JSON, or use a documented way to suppress/reduce reasoning if MiniMax M3
supports it. Verify the chosen parameter with a minimal probe before one more
checkpoint retry.

### A11 — 2026-08-03 — P2-M3: expand M3 budget and request minimal reasoning

Reality:
The user authorized a larger completion budget and asked to disable thinking.
MiniMax documentation does not expose a supported M-series thinking-off switch:
`reasoning_split` separates reasoning, while Anthropic-compatible `thinking`
controls are documented as ignored. A minimal probe instead combined explicit
"simple formatting; no deep analysis; return final JSON immediately" guidance
with `max_completion_tokens=8192` and retained `reasoning_split=true`. It
returned HTTP 200, `finish_reason=stop`, valid JSON content, 75 reasoning
characters, and only 26 completion tokens.

Decision class:
User-approved amendment to A10's M3 request budget/prompt.

Impact:
Increase the production repair budget from 2,048 to 8,192 tokens and prepend
the verified simple-formatting/minimal-reasoning instruction to the strict raw
JSON system prompt. Keep temperature `1`, separated reasoning, sanitized
diagnostics, model routing, two-attempt semantic validator, and fail-closed
publication unchanged. Update the deterministic request fixture, then permit
one checkpoint resume.

Executor action:
Apply and locally verify the exact probed settings, then resume the existing
completed checkpoint once. If publication succeeds, run MiroFish live gates;
if it fails, record the sanitized result and halt.

Planner/user resolution required:
Resolved by the user's explicit budget/minimal-thinking request and successful
8,192-token live probe.

### A12 — 2026-08-03 — P2-M3: larger M3 budget scales reasoning without final content

Reality:
The A11 settings passed local verification and the single checkpoint resume
launched PID 175 against cached `sim_e539fd57be89` / `report_48529846ff56`.
MiniMax M3 again returned HTTP-success with `finish_reason=length`, no content,
one reasoning block, and no sensitivity flag. At the 8,192-token budget it
produced 32,277 reasoning characters—approximately four times A10's 7,901
characters at 2,048 tokens—without reaching final JSON. The explicit
simple-formatting/no-deep-analysis instruction did not suppress reasoning.

Decision class:
LOCKED under A11. Its one checkpoint resume is exhausted.

Impact:
Increasing the budget does not solve this full-prompt M3 behavior; it increases
latency/cost while the model fills the new budget with reasoning. P2-M3 and
Phase 2 remain open. Leaving the 8,192 setting active would expose the next
automatic checkpoint resume to the same higher cost.

Executor action:
Reverted the experimental budget/prompt delta to the prior committed strict-M3
configuration while retaining sanitized diagnostics and this audit. Did not
issue another resume, change models, relax validation, restart infrastructure,
mutate output, or write another repository.

Planner/user resolution required:
Do not increase M3's completion budget again without a documented reasoning
control. Choose a backend/mode that enforces final structured output (the probed
Kimi K3 JSON-object mode is one verified option), or materially redesign the
repair task/prompt under a new contract.

### A13 — 2026-08-03 — P2-M3: route non-thinking DeepSeek Flash through ai_backend

Reality:
The user selected DeepSeek V4 Flash, then clarified that provider access must
go through the shared local `ai_backend` gateway rather than a direct DeepSeek
credential. The gateway is healthy and advertises `deepseek-v4-flash`, but its
DeepSeek adapter accepted metadata without forwarding `max_tokens`,
`response_format`, or `thinking`. DeepSeek's current API documents all three
controls and defaults thinking to enabled. A no-network adapter fixture now
proves forwarding, and a minimal live `/callAI` probe returned
`{"value": 1}` with no reasoning content using thinking disabled and JSON-object
mode.

Decision class:
User-approved amendment to A12's repair backend and request contract.

Impact:
Route only constrained scenario repair through `http://localhost:9400/callAI`
(or `AI_BACKEND_URL`) with provider `deepseek`, model
`deepseek-v4-flash`, `max_tokens=8192`,
`response_format={"type":"json_object"}`, and
`thinking={"type":"disabled"}`. Forward these allowlisted fields in
`ai_backend/app/services/deepseek_service.py`; keep the provider credential in
the gateway. Preserve the strict prompt, two-attempt semantic validator,
fail-closed publication, and sanitized no-content diagnostics. Permit exactly
one `/resume` against the existing completed checkpoint; do not start a new
simulation.

Executor action:
Apply and locally verify the gateway and MiroFish request contracts, then resume
the existing completed checkpoint once. If publication succeeds, run the
original M3 live gates. If it fails, record the exact sanitized result and halt.
Commit the gateway compatibility fix separately in `ai_backend`; do not modify
any other external-repository files.

Planner/user resolution required:
Resolved by the user's explicit DeepSeek selection, `ai_backend` routing
clarification, and request to try it.

### A14 — 2026-08-03 — P2-M3: gateway routing skipped without an API key

Reality:
The A13 local request contract and gateway adapter fixture passed, and the
minimal live `ai_backend` probe returned valid JSON with no reasoning content.
The single authorized checkpoint resume launched PID 190 against cached
`sim_e539fd57be89` / `report_48529846ff56`, but publication failed before an
HTTP request was made: `scenario repair has no configured backend`. The repair
loop skips a backend whenever its key is empty. That behavior was inherited
from direct provider routing but is wrong for local `ai_backend`, where
localhost access intentionally requires no client key. The container also
requires `http://host.docker.internal:9400`, not the host-process default
`http://localhost:9400`. No report was published; the step-6 checkpoint remains
current.

Decision class:
LOCKED under A13. Its one checkpoint resume is exhausted.

Impact:
DeepSeek V4 Flash itself is not implicated: its thinking-off JSON probe passed.
The remaining failure is local gateway reachability/configuration in the
MiroFish caller. P2-M3 and Phase 2 remain open.

Executor action:
Recorded the exact failure and halted. Did not issue another resume, start a
simulation, relax validation, mutate output, or write another external
repository beyond A13's verified `ai_backend` compatibility change.

Planner/user resolution required:
Amend the MiroFish caller to allow an empty optional `AI_BACKEND_API_KEY`, set
the `mirofish-api` container's `AI_BACKEND_URL` to
`http://host.docker.internal:9400`, add a deterministic no-key fixture, and
permit one more checkpoint resume.

### A15 — 2026-08-03 — P2-M3: authorize keyless container gateway repair

Reality:
The user explicitly authorized the exact A14 repair. Local `ai_backend` permits
private-network callers without `X-API-Key`, and the MiroFish API container
already maps `host.docker.internal` to the host gateway.

Decision class:
User-approved amendment to A14's caller configuration boundary.

Impact:
Remove the obsolete empty-key skip from the single-backend scenario-repair
loop, while continuing to send `X-API-Key` when configured. Set only the
`mirofish-api` service's `AI_BACKEND_URL` to
`http://host.docker.internal:9400`. Update the deterministic fixture to prove a
keyless request is sent without either authentication header. Recreate only
`mirofish-api` to apply the environment change, verify gateway health from the
container, and permit exactly one `/resume` against the existing checkpoint.

Executor action:
Apply and verify the exact repair, recreate only `mirofish-api`, then perform
one checkpoint resume. If publication succeeds, run the original M3 live
gates; if it fails, record the exact result and halt.

Planner/user resolution required:
Resolved by the user's explicit “yes fix” authorization.

Use this format for a divergence:

```text
### A<N> — YYYY-MM-DD — <milestone>: <short title>

Reality:
Decision class:
Impact:
Executor action:
Planner/user resolution required:
```
