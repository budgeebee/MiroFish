# MiroFish — Local Configuration

## What This Is
MiroFish is a multi-agent social simulation engine. We use it as the **strategic prediction layer** in an autonomous trading pipeline running on a Raspberry Pi (ARM64).

## Local Setup
- **Docker**: Built locally (`build: .` not pre-built image) for ARM64 compatibility
- **Ports**: 3003 (frontend), 5005 (backend) — changed from defaults to avoid conflicts
- **LLMs**: MiniMax M2.7 (main) + Kimi K2.5 (boost) via OpenAI-compatible APIs
- **Zep Cloud**: Free tier for knowledge graph storage
- **Config**: `.env` file (not committed)

## The Pipeline
```
4:00 AM ET   crucix_to_mirofish.py --max-rounds 10
             ├─ Reads ~/Projects/Crucix/runs/latest.json (29 OSINT sources)
             ├─ Fetches news-aggregator (8 global news/tech sources)
             ├─ Combines into markdown brief
             └─ Drives MiroFish: ontology → graph → simulation → prediction report

Output:      ~/Projects/MiroFish/output/prediction_YYYYMMDD_HHMMSS.md
```

The prediction report is consumed by the schwalpaca-trading skill's 3 daily trading cycles (9:35 AM, 1:00 PM, 3:50 PM ET) via `~/.openclaw/workspace/skills/schwalpaca-trading/scripts/mirofish.py`.

**Fallback**: If MiroFish didn't run (no report for today), `cycle.py` fetches news-aggregator headlines directly so the agent still has global news context beyond Crucix.

## Three Intelligence Layers
1. **MiroFish** (strategic, overnight) — "how will people react to these signals?"
2. **Crucix** (tactical, every 15 min) — "what's happening now?" (29 OSINT sources)
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
