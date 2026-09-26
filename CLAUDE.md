# MiroFish — Local Configuration

## Phase 1 handoff — closed 2026-07-27

Phase 1 is closed. P1-M5 is committed after the first corrected live report.

- Live report: `prediction_20260727_135603`
- Artifacts: Markdown, `scenario-synthesis.v1`, and `observation-manifest.v1`
  all share that report ID.
- Strict freshness verifier: passed; 59 observations, 45 Crucix-derived and 8
  market-derived; no active process or current checkpoint.
- `mirofish-daily.timer`: enabled and active for 21:05 `America/Tijuana`.
- User lingering: enabled.
- Hermes baseline: all 8 jobs remain enabled.
- Crucix is live on committed `453b3fa` with 43 registry/sourceHealth entries.
- MiroFish hotfixes `3b2cfc7`, `5d1dd98`, and `8d0e187` remain separate and
  preserve raw simulation, local schema repair, exact provenance, and market
  evidence separation.
- Failed intermediate artifacts remain recoverable under
  `output/recovery/`; do not delete them during later cleanup.

Closeout checks used:

```bash
python3 scripts/verify_daily_freshness.py --live
systemctl --user status mirofish-daily.timer
systemctl --user list-timers --all
python3 scripts/verify_pipeline_contract.py
```

These checks passed.

## Current handoff — Phase 2 closed 2026-08-03

`PHASE2_PLAN.md` is COMPLETE. All three serial milestones are committed:

1. P2-M1 — exact per-contract Polymarket observations and prompt-visible
   ID-to-question mapping;
2. P2-M2 — signed `{-1,0,+1}` direction per exact hypothesis/contract pair;
3. P2-M3 — natural live canary, downstream capture, and phase closeout.

The closing natural report is `prediction_20260804_011212`: strict live
verification reports 10 granular Polymarket observations, 9 signed direction
pairs, 6 abstentions, no active pipeline, and no current checkpoint. The
matching trading-intelligence snapshot is banked (3 snapshots / 9 hypotheses),
and its armed capture timer now runs at 22:45 PDT after the measured 67-minute
MiroFish run raced the former 22:07 trigger.

Constrained scenario repair now routes `deepseek-v4-flash` through local
`ai_backend` with thinking disabled and JSON-object mode; the provider
credential remains in the gateway. MiroFish's own daily timer remains enabled
at 21:05 PDT. No Phase 3 is planned; the Planner/Executor workflow is dormant
here until a new phase is explicitly requested.

## Phase 7 (Schwalpaca) — Generator lane, 2026-09-22

> This project runs on the Planner/Executor workflow — see ~/Projects/PLANNING.md.

M8 of `PHASE7_PLAN.md` (in `/home/irvins/Projects/worktrees/schwalpaca-phase6`)
is implemented on branch `schwalpaca-phase7` in the isolated worktree
`/home/irvins/Projects/worktrees/mirofish-schwalpaca-phase7`. The canonical
checkout `/home/irvins/Projects/MiroFish` is **not** edited. The translation
model switch (`granite4.1-8b` → `router-qwen3.5-9b`, S plan amendment A7-12) is
committed (45ff44e, e17bb45 on 2026-09-22): granite mistranslated 3000亿元 as
"30 billion yuan". granite4.1-8b was removed from llama-swap on 2026-09-25.

- Lab mode activates only when the trusted launcher supplies both
  `--lab-feedback PATH` and `--lab-proposals-dir DIR` (configured via
  `MIROFISH_LAB_FEEDBACK_PATH` / `MIROFISH_LAB_PROPOSALS_DIR` /
  `MIROFISH_LAB_EXCHANGE_GID` on `mirofish-api`). Neither set → byte-identical
  legacy prompts, parsers, argv and publication set.
- `scripts/lab_contract.py` mirrors the frozen Core wire contracts;
  `fixtures/lab-feedback.json` is byte-identical to the Core fixture
  (SHA-256 `8a7a936a016f3dbb38553692da28268e2786bb9d6ea726b69b93b41f10cc2a24`).
- Verify: `python3 scripts/verify_lab_feedback_contract.py` →
  `ALL LAB FEEDBACK CONTRACT CHECKS PASSED` (95 checks, zero model/network).
  `python3 scripts/verify_pipeline_contract.py` keeps its pre-existing
  diagnostics baseline failure signature (measured, not fixed here).
- Deploying the launcher flags requires rebuilding/recreating the
  `mirofish-api` image at CP7-LOOP (M10) — staged source only here.

## Phase 2 scope source — 2026-08-02

`REQUEST-trading-intelligence-phase6.md` (repo root) is an incoming
request from the trading-intelligence Phase 6 experiment lane. It has now
been incorporated into `PHASE2_PLAN.md`; it remains a scope source, not a
patch to apply directly. Two asks:

1. **Per-market Polymarket observations** — emit one observation per
   market carrying `venueContractId`, instead of one per source. Today
   `market_observation_ids` resolves to `Crucix/Polymarket` (the whole
   feed), so a consumer cannot tell WHICH market a hypothesis is about.
   Follows the existing `Crucix/Adanos/{section}` per-section pattern in
   `crucix_to_mirofish.py`. Small.
2. **A signed direction per (hypothesis, market) pair**, in `{-1,0,+1}`.
   Larger — touches the scenario prompt, `HYPOTHESIS_FIELDS`, and
   `validate_scenario_synthesis`. The requester's argument is that if
   they infer direction from `claim` prose themselves, their experiment
   measures their reading of MiroFish rather than MiroFish. Direction is
   a property of the PAIR, not the claim: one real claim was clearly
   DOWN against "Israel x Iran ceasefire continues through August 3?"
   and indeterminate against "Iran leadership change by July 31?".

Ask 1 is useful alone. Neither ask requires a permissions change —
they read `/output/today/structured` and `/manifest` over the loopback
API on 5010, the same path `schwalpaca-trading` already uses.

## What This Is
MiroFish is a multi-agent social simulation engine. On Strixy it is the
**evidence and scenario-synthesis layer** for the private Schwalpaca and Kalshi
paper/small-stakes trading workflows. It explores conditional market reactions;
it is not an oracle or an order recommendation service.

## Local Setup
- **Docker**: Built locally (`build: .`, not a pre-built image)
- **Ports**: 3003 (frontend), 5005 (backend) — changed from defaults to avoid conflicts
- **LLMs**: MiniMax M2.7 (main) + Kimi K2.5 (boost) via OpenAI-compatible APIs
- **Knowledge graph**: local Graphiti API backed by Kuzu
- **Config**: `.env` file (not committed)
- **Direct host use**: `python3 scripts/crucix_to_mirofish.py`; Docker paths
  and service names are supplied explicitly by `docker-compose.yml`

## The Pipeline
```
4:00 AM ET   crucix_to_mirofish.py --max-rounds 10
             ├─ Reads ~/Projects/Crucix/runs/latest.json (registry-derived source count)
             ├─ Fetches news-aggregator (8 global news/tech sources)
             ├─ Builds a provenance manifest and markdown evidence brief
             └─ Drives MiroFish: ontology → Graphiti/Kuzu → simulation → scenario report

Outputs:     prediction_YYYYMMDD_HHMMSS.md
             prediction_YYYYMMDD_HHMMSS.json
             prediction_YYYYMMDD_HHMMSS.manifest.json
             simulation_YYYYMMDD_HHMMSS.md
```

The legacy prediction filename and output endpoints remain the compatibility
surface consumed by the Schwalpaca and Kalshi skills. The structured scenario
and observation-manifest siblings use the same report ID.

The canonical report starts with observations, evidence-linked inferences,
unknowns, competing scenarios, falsifiers, and watch conditions. The complete
original social-simulation narrative—including its trading-signal extraction—is
preserved afterward as a clearly labeled model-derived layer and in the separate
`simulation_*.md` source artifact. Those implications are conditional hypotheses,
not verified facts, outcome probabilities, or trade recommendations. An English
`prediction_*_en.md` sibling is still attempted without blocking publication.

**Fallback**: If MiroFish didn't run (no report for today), `cycle.py` fetches news-aggregator headlines directly so the agent still has global news context beyond Crucix.

## Three Intelligence Layers
1. **MiroFish** (strategic, overnight) — "what reactions are plausible, and what evidence would support or falsify them?"
2. **Crucix** (tactical, every 15 min) — "what's happening now?" (source registry is authoritative)
3. **Schwalpaca** (execution, real-time) — "what can I trade?" (quotes, positions, orders)

## Key Files
- `scripts/crucix_to_mirofish.py` — Main pipeline script (Crucix + news-aggregator → MiroFish)
- `scripts/verify_daily_freshness.py` — Read-only daily report, source, schema, and pipeline verifier
- `ops/systemd/mirofish-daily.{service,timer}` — Private Strixy user timer; 21:05 America/Tijuana
- `ops/install-user-unit.sh` — Dry-run-by-default installer for only the two MiroFish user units
- `backend/app/utils/llm_client.py` — Modified with JSON repair + Kimi temp override
- `docker-compose.yml` — Local ARM64 build, ports 3003/5005
- `frontend/vite.config.js` — Tailscale hostname allowed

## LLM Quirks (learned the hard way)
- **MiniMax M2.5/M2.7**: Produces broken JSON in creative ways. `_repair_json()` in llm_client.py handles it.
- **Kimi K2.5**: Only accepts `temperature=1.0`. Auto-detected by model name.
- **MiniMax base URL**: `https://api.minimax.io/v1` (NOT .chat or .com)
- **Kimi base URL**: `https://api.moonshot.ai/v1`
