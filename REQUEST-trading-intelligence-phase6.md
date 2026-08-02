# Request to MiroFish — per-market Polymarket observations, and a signed direction

**From:** trading-intelligence Phase 6 (Lane R), 2026-08-02
**Two asks.** Ask 1 (granularity) is small and follows a pattern already
in `crucix_to_mirofish.py`. Ask 2 (direction) is the one that actually
decides whether our experiment measures MiroFish or measures our reading
of MiroFish — please read that section even if Ask 1 is declined.

## Ask 1 — per-market observations

### What we need

Today `scenario-synthesis.v1` hypotheses carry `market_observation_ids`
that resolve to **one observation per SOURCE**:

```
obs-35ca02c161fcec6f69ad  source_id "Crucix/Polymarket"  payload_ref /sources/Polymarket
```

That tells a consumer *"Polymarket was consulted"* — not *which market*.
A manifest observation carries `source_id`, `payload_ref`,
`content_sha256`, staleness, and **no market identity at all**.

We need one observation **per Polymarket market**, so a hypothesis can
cite the specific contract it is about.

## Why this is not a redesign

`crucix_to_mirofish.py` already does exactly this for Adanos. In the
source loop (~L280-307) there is a special branch emitting one
observation per *section*:

```python
observations.append(_observation(
    f"Crucix/Adanos/{section}",
    section_payload,
    ...
    payload_ref=f"/sources/Adanos/{section}",
    observed_at=(_latest_market_observed_at({"markets": section_payload})
                 if provenance.get("marketDerived") else None),
))
```

The ask is the same shape for Polymarket, keyed on the market rather
than a section:

```python
source_id   = f"Crucix/Polymarket/{market['venueContractId']}"
payload_ref = f"/sources/Polymarket/top/{i}"
market_derived = True
```

`venueContractId` is already present on every entry in the Crucix
Polymarket payload (alongside `questionId`, `question`, `outcomeTokenId`,
`slug`, `rulesSha256`) — nothing new needs to be derived or fetched.

## Why it works end-to-end without other changes

- `allowed` (L646) is built by iterating `manifest["observations"]`, so
  the finer observations appear in the prompt automatically.
- The prompt already instructs the model to use exact allowed IDs and
  confines market-derived IDs to `market_observation_ids`.
- `resolve(draft, "market_observation_ids", "market_source_ids")` already
  maps source ids to observation ids.
- `validate_scenario_synthesis` and `HYPOTHESIS_FIELDS` need no change;
  the field set is identical, only its granularity improves.

## One real cost, and a suggested mitigation

The Crucix Polymarket block holds ~30 markets. Emitting one observation
each grows the manifest from ~58 to ~87 observations and adds roughly
3-4 KB to the `ALLOWED OBSERVATIONS` JSON in the prompt. Given
`brief_text` is capped at 8000 chars and `report_text` at 6000, prompt
budget is clearly something you already manage.

Suggestion: emit per-market observations only for the markets that
actually reach the brief — `top[:10]` plus `highProbShifts[:5]` — and
keep a single source-level observation for the remainder. That captures
everything a hypothesis can realistically cite while adding ~15
observations rather than ~30. We have no stake in which option you pick;
either solves our problem.

## What this unlocks, measured

Measured on 2026-08-01/02 against the live report and our market tape:

```
today, source-level:
  3 hypotheses, all carrying market_observation_ids
  but ids identify a SOURCE, so we must fall back to matching
  affected_entities against market question text
  -> 1 of 3 hypotheses joins anything at all
     (hyp-01 ['Iran',...] matched 23/101 markets;
      hyp-02 ['Retail Investors','AI Sentiment Tools (Perplexity)',...] and
      hyp-03 ['Gold Market','U.S. Treasury','Global Equities'] matched 0)

with per-market ids:
  -> 3 of 3 hypotheses join, by exact id rather than fuzzy text
```

The entity-text fallback works only when `affected_entities` are concrete
named things (a country) and fails on abstract categories ("Gold Market",
"Retail Investors"). That is not a defect in MiroFish — those are
reasonable entities for the claim being made. It is simply not a join key.

## Cost of delay

We now snapshot `/output/today/structured` + `/manifest` nightly at 22:07
(the API serves only today, and a monthly cron rotates the on-disk
history away, so unsnapshotted days are lost permanently). Those
snapshots are stored **verbatim**.

That means every day captured before this change is banked in the
source-level form and stays that way. Our experiment needs ~30
independent hypotheses; at ~1 usable per day today versus ~3 with this
change, landing it soon is the difference between ~30 days of mostly
weak evidence and ~30 days of mostly strong evidence. Retro-fixing is
impossible — the reports are gone.

## What we are NOT asking for

- No change to `scenario-synthesis.v1`'s field set or schema version.
- No change to hypothesis count, prompt semantics, or model choice.
- No file-permission change. Your artifacts are root-owned 0600 and that
  is fine — we read the API on port 5010, the same supported path
  `schwalpaca-trading/scripts/mirofish.py` uses.

## Verification we would suggest

`scripts/verify_pipeline_contract.py:400` already asserts
`market_observation_ids` is populated. Extending it to assert that at
least one resolves to a `source_id` matching `Crucix/Polymarket/*` would
pin the new granularity with the contract test you already run.


---

# Ask 2 — a signed direction per (hypothesis, market)

**This is the more important of the two.** Ask 1 makes our join exact;
Ask 2 decides whether the experiment is sound at all.

## The problem, concretely

Your hypotheses carry `claim` as prose. To test whether a hypothesis
predicted a market's movement, we need to know which way it predicted —
UP or DOWN — for **that specific market**. Prose does not carry that in
a machine-readable way, and the polarity is not a property of the claim
alone. From today's real report:

```
claim (hyp-mena-escalation-risk):
  "Security situation ... remains elevated with active US military
   strikes and heightened alert levels, DESPITE prediction markets
   pricing in ceasefire continuity."

vs "Israel x Iran ceasefire continues through August 3?"  -> clearly DOWN
vs "Iran leadership change by July 31?"                   -> indeterminate
```

One claim, one entity (`Iran`), two different polarities. So a
per-*claim* direction field would not solve it — the direction is a
property of the **(hypothesis, market) pair**.

## Why we are asking you rather than doing it ourselves

We can annotate directions on our side. We do not want to, and the
reason is not convenience.

Phase 6 exists to answer one question: does the intelligence stack
predict better than the market already does? If we read your prose and
assign the directions, then what we measure is **our interpretation of
MiroFish**, not MiroFish. An annotator sitting between the producer and
the scorer is a confound that no amount of preregistration removes — the
methodological equivalent of grading your own translation. A direction
you emit is your claim, and testing it is a real test.

An independent review of our draft contract (2026-08-02) flagged this as
one of five blocking defects and named it the one where "a null result
becomes a false positive."

## What we would like

For each hypothesis, for each market it is actually about, a signed
direction — ideally alongside Ask 1's per-market observation ids:

```json
"market_directions": [
  {"observation_id": "obs-...", "direction": -1},
  {"observation_id": "obs-...", "direction": 0}
]
```

with `direction` in `{-1, 0, +1}` where:

```
+1  the claim implies this market's YES becomes MORE likely
-1  ... LESS likely
 0  the claim is genuinely indeterminate for this market
```

**`0` is not a failure mode and we want you to use it freely.** A model
forced to pick a sign on an indeterminate pair produces noise that looks
like signal, which is worse for us than an honest abstention. We count
and report abstentions; they cost us nothing but sample size.

If Ask 1 is declined, this still works keyed on question text or
`venueContractId` instead of `observation_id` — the pairing is what
matters, not the key.

## Prompt-level note

Your scenario prompt already enumerates ALLOWED OBSERVATIONS and confines
market-derived ids to `market_observation_ids`. Extending that to ask for
a direction per cited market looks like an additive instruction plus one
field in `HYPOTHESIS_FIELDS`, with `validate_scenario_synthesis` gaining
a membership check on `{-1,0,1}`. We have not tried it, so treat that as
an outsider's guess at the size, not a claim.

## Timing

Less urgent than Ask 1. Snapshots store your artifacts verbatim, and
direction can be applied to already-banked reports as long as it is
committed before the prediction window resolves — so this is not
perishable the way the nightly capture was. Roughly a week of slack
before we would build a fallback annotator on our side, which we would
rather not do for the reason above.

## What we are NOT asking

- No probability, no confidence, no numeric forecast. A sign only.
- No trade recommendation, and nothing that changes what your
  `epistemic_status` or `confidence` fields mean.
- No commitment to be right. We are testing that, and a null result is
  an acceptable and publishable outcome on our side.
