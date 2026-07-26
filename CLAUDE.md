# MiroFish — Local Configuration

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
- `backend/app/utils/llm_client.py` — Modified with JSON repair + Kimi temp override
- `docker-compose.yml` — Local ARM64 build, ports 3003/5005
- `frontend/vite.config.js` — Tailscale hostname allowed

## LLM Quirks (learned the hard way)
- **MiniMax M2.5/M2.7**: Produces broken JSON in creative ways. `_repair_json()` in llm_client.py handles it.
- **Kimi K2.5**: Only accepts `temperature=1.0`. Auto-detected by model name.
- **MiniMax base URL**: `https://api.minimax.io/v1` (NOT .chat or .com)
- **Kimi base URL**: `https://api.moonshot.ai/v1`
