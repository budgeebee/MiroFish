# Ticker Contract: make MiroFish predictions reach the schwalpaca universe

Status: **AWAITING LIVE VERIFICATION** — steps 1-5 done 2026-09-17 (`1b90b84`,
`4611bf8`). Step 6 verifies on the 1am run.

## The problem

The intended architecture already holds on the input side:

- **Intel layer** (schwalpaca screeners, Schwab movers, options flow, earnings,
  social sentiment, Insider Capitol) supplies the basket of tickers on the table.
- **Crucix** supplies the pulse of the planet — world context.
- **MiroFish** is the round table that discusses them and predicts.

`crucix_to_mirofish.py` already fetches all of it (`/market-data/screen/all`,
movers, options flow, earnings, per-symbol sentiment and news) and renders it
into the brief.

The break is on the **output** side. Hypotheses carry `affected_entities`, and
schwalpaca's `adapt_mirofish` maps a candidate to an instrument only when an
entity arrives as an object carrying an explicit `ticker`. That adapter
deliberately refuses to parse tickers out of free text — "Hypothesis claims are
never parsed for tickers" — because guessing would let narrative text mint
positions.

The synthesis prompt never states the shape of `affected_entities`, so the model
returns bare strings. Live evidence — all 12 MiroFish candidates ever ingested:

```
Federal Reserve · US bonds · equity indices · Crude oil markets · Gold market
Natural gas market · Defense stocks · Shipping/insurance · Prediction markets
```

All flagged `unresolved_entity_name`; **0 of 12 mapped**. An older report shows
`"affected_entities": ["INTC","AMZN","TSLA","X/Twitter","Reddit","VIX","Iran"]`
— real tickers mixed with organizations in one flat list, so even correct picks
are dropped.

Two failures, then: the round table answers in themes rather than tickers, and
when it does name a ticker it is indistinguishable from an organization.

## The fix

**One contract change, in MiroFish only.** Schwalpaca's adapter already accepts
the target shape; no change is needed there.

`affected_entities` becomes a list of objects:

```json
{"name": "Intel", "ticker": "INTC"}     // tradable, ticker from the basket
{"name": "Federal Reserve", "ticker": null}  // thematic entity
```

Rules the prompt must enforce:

1. `ticker` is non-null **only** for an exchange-listed security that appeared in
   the basket supplied with the brief. Never invent or infer one.
2. Thematic entities (Fed, oil, defense sector, a country) keep `ticker: null`.
   They remain valuable context; they are simply not candidates.
3. Every hypothesis that concerns tradable names should name them, so the
   prediction is attached to instruments rather than to a theme.

The basket is passed to the prompt explicitly rather than left implicit in the
truncated brief — the brief is cut to 5000+3000 characters before the model sees
it, which can drop the ticker sections entirely.

## Touch points

| Repo | Change |
|---|---|
| `~/Projects/MiroFish` | Collect basket tickers during brief assembly; pass to the synthesis prompt; specify the `affected_entities` object shape and the basket-only rule; keep the validator permissive to both shapes |
| `~/Projects/schwalpaca` (phase6 worktree) | **No adapter change.** Intake automation is a separate, checkpoint-gated Phase 6 item |
| `~/.hermes/skills/` | No change. `mirofish.py` only launches the run |

## Steps

1. [x] Fix the `insider_capitol` schema break that crashed every run since
   2026-09-05 (`mean_ar_90` → `estimate_pct`; trifecta → relationship leads).
   Commit `1b90b84`.
2. [x] Collect the basket: symbols from screeners, movers, and earnings, into a
   module-level list during brief assembly.
3. [x] Thread it into `_scenario_repair_prompt` and render a `TRADABLE BASKET`
   block.
4. [x] Rewrite the `affected_entities` paragraph of the prompt to specify the
   object shape and the basket-only rule.
5. [x] Back-compat: `adapt_mirofish` already handles both shapes; confirm the
   MiroFish-side validator and `verify_pipeline_contract.py` accept objects.
6. [x] Independent review (Kimi, 2026-09-17) found the fix reached only the
   *repair* prompt. There are two synthesis paths — the simulation's own
   `## Scenario Synthesis` block (tried first) and the deepseek repair
   (fallback) — so on any night the simulation emitted valid JSON, nothing
   would have changed. Fixed in `4ea60fd`: the object-shape rule now also sits
   in `SIMULATION_REQUIREMENT`, and `_normalize_affected_entities()` coerces
   both paths' output into `{name, ticker}`, promoting a bare string only on an
   exact basket match and refusing an off-basket ticker. Also fixed from the
   same review: relationship-lead rows read `type`/`amount_mid` (the payload has
   no `transaction_type`/`amount_min`/`amount_max`); the basket walk reads only
   symbol-ish keys so an `"exchange": "NYSE"` cannot mint a ticker; `--resume`
   restores the basket from checkpoint state instead of forcing every ticker
   null; and the fetch log line no longer reports a permanently-zero trifecta
   count.
7. [ ] Verify on the 1am run: hypotheses carry ticker objects, then
   `ingest-candidates --source mirofish` maps at least one instrument.

## Out of scope tonight

- **Intake automation.** `ingest-candidates` is a manual CLI command with no
  timer; it last ran 2026-08-29, which is why every source looks frozen. Adding
  a timer touches Phase 6's live unit set and needs its own checkpoint.
- **MiroFish output file permissions** (`root:root` mode 600). Ingest reads the
  HTTP API, not the files, so this blocks humans, not the pipeline.
- **Instrument mapping for `insider_capitol`** (12 of 84 candidates map). Its
  disclosures carry real tickers; worth a separate look.
