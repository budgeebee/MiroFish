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
    MIROFISH_URL        MiroFish backend (default: http://localhost:5005)
    CRUCIX_LATEST       Path to latest.json (default: ~/Projects/Crucix/runs/latest.json)
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

MIROFISH_URL = os.getenv("MIROFISH_URL", "http://localhost:5005")
CRUCIX_LATEST = os.getenv(
    "CRUCIX_LATEST",
    os.path.expanduser("~/Projects/Crucix/runs/latest.json"),
)
SCHWALPACA_URL = os.getenv("SCHWALPACA_URL", "http://localhost:8855")
SCHWALPACA_API_KEY = os.getenv("SCHWALPACA_API_KEY", "")

SIMULATION_REQUIREMENT = """\
Simulate how active traders, market analysts, institutional investors, retail \
traders, geopolitical analysts, and financial media would react to the \
intelligence signals described in this brief over the next 24-48 hours.

Focus on:
1. Which narratives gain traction and which fade — what does the crowd latch onto?
2. Sentiment shifts across asset classes (equities, energy, bonds, crypto, defense)
3. Time-delayed correlations the market hasn't priced in yet — 2nd and 3rd order effects
4. Where retail and institutional sentiment diverge — that gap is often the edge
5. Sector rotation signals — which sectors see inflows vs outflows based on this intel
6. Risk-off vs risk-on sentiment trajectory — are participants hedging or reaching?

The goal is to identify exploitable trading edges: asymmetric information advantages, \
narrative momentum before consensus forms, and cross-domain correlations that most \
market participants will miss or react to slowly.\
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

    parts = []
    parts.append(f"# OSINT Intelligence Brief — {ts[:19]}Z\n")
    parts.append(f"*{ok} sources reporting, {failed} failed*\n\n")

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

NEWS_AGGREGATOR_SCRIPT = os.path.expanduser(
    "~/.openclaw/workspace/skills/news-aggregator-skill/scripts/fetch_news.py"
)


def fetch_news_aggregator(limit=12):
    """Run news-aggregator and return parsed JSON list, or [] on failure."""
    if not os.path.exists(NEWS_AGGREGATOR_SCRIPT):
        print("  Warning: news-aggregator-skill not found, skipping")
        return []
    try:
        result = subprocess.run(
            ["python3", NEWS_AGGREGATOR_SCRIPT, "--source", "all", "--limit", str(limit)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            print(f"  Warning: news-aggregator failed: {result.stderr[:200]}")
            return []
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
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

    return _section("Global News & Tech Headlines (8 sources)", "\n".join(lines))


# ---------------------------------------------------------------------------
# Social Sentiment for held positions
# ---------------------------------------------------------------------------


def get_held_symbols():
    """Read open positions from the schwalpaca trading journal."""
    journal_path = os.path.expanduser("~/.openclaw/workspace/trading-journal.json")
    if not os.path.exists(journal_path):
        return []
    try:
        with open(journal_path) as f:
            journal = json.load(f)
        return list({t["symbol"] for t in journal.get("trades", []) if t.get("status") == "open" and t.get("symbol")})
    except Exception:
        return []


def fetch_social_sentiment(symbol):
    """Fetch social sentiment for a symbol from schwalpaca API."""
    try:
        headers = {"X-API-Key": SCHWALPACA_API_KEY}
        r = requests.get(
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
        r = requests.get(
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
# MiroFish API pipeline
# ---------------------------------------------------------------------------


def poll_task(task_id: str, label: str, endpoint: str = "/api/graph/task"):
    """Poll a MiroFish async task until completion."""
    url = f"{MIROFISH_URL}{endpoint}/{task_id}"
    spinner = ["|", "/", "-", "\\"]
    i = 0
    while True:
        r = requests.get(url, timeout=30)
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


def run_pipeline(md_path: str, max_rounds: int, project_name: str):
    """Drive the full MiroFish pipeline: ontology -> graph -> sim -> report."""
    base = MIROFISH_URL
    print(f"\n{'='*60}")
    print(f"  MiroFish Pipeline — {project_name}")
    print(f"  Max rounds: {max_rounds}")
    print(f"  Backend: {base}")
    print(f"{'='*60}\n")

    # --- Step 1: Upload & generate ontology ---
    print("[1/6] Uploading brief & generating ontology...")
    with open(md_path, "rb") as f:
        r = requests.post(
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
    project_id = resp["data"]["project_id"]
    entity_types = len(resp["data"]["ontology"].get("entity_types", []))
    edge_types = len(resp["data"]["ontology"].get("edge_types", []))
    print(f"  OK: project={project_id}, {entity_types} entity types, {edge_types} edge types")

    # --- Step 2: Build graph ---
    print("\n[2/6] Building knowledge graph...")
    r = requests.post(
        f"{base}/api/graph/build",
        json={"project_id": project_id},
        timeout=30,
    )
    r.raise_for_status()
    resp = r.json()
    if not resp.get("success"):
        print(f"  FAILED: {resp.get('error')}")
        sys.exit(1)
    task_id = resp["data"]["task_id"]
    task_result = poll_task(task_id, "graph build")
    graph_id = task_result.get("result", {}).get("graph_id")
    print(f"  graph_id={graph_id}")

    # --- Step 3: Create simulation ---
    print("\n[3/6] Creating simulation...")
    r = requests.post(
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
    simulation_id = resp["data"]["simulation_id"]
    print(f"  OK: simulation_id={simulation_id}")

    # --- Step 4: Prepare simulation ---
    print("\n[4/6] Preparing simulation (generating agent profiles & config)...")
    r = requests.post(
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

    # --- Step 5: Start simulation ---
    print(f"\n[5/6] Running simulation (max {max_rounds} rounds)...")
    r = requests.post(
        f"{base}/api/simulation/start",
        json={
            "simulation_id": simulation_id,
            "platform": "parallel",
            "max_rounds": max_rounds,
        },
        timeout=30,
    )
    r.raise_for_status()
    resp = r.json()
    if not resp.get("success"):
        print(f"  FAILED: {resp.get('error')}")
        sys.exit(1)
    print(f"  OK: simulation running (pid={resp['data'].get('process_pid')})")

    # Poll simulation status
    spinner = ["|", "/", "-", "\\"]
    i = 0
    while True:
        r = requests.get(f"{base}/api/simulation/{simulation_id}/run-status", timeout=30)
        r.raise_for_status()
        status_data = r.json().get("data", {})
        runner_status = status_data.get("runner_status", "unknown")
        current = status_data.get("current_round", 0)
        total = status_data.get("total_rounds", "?")
        pct = status_data.get("progress_percent", 0)

        sys.stdout.write(f"\r  {spinner[i % 4]} [simulation] {runner_status} round {current}/{total} ({pct}%)   ")
        sys.stdout.flush()
        i += 1

        if runner_status in ("completed", "stopped"):
            print(f"\n  OK: simulation {runner_status}.")
            break
        elif runner_status == "failed":
            print(f"\n  FAILED: simulation failed")
            sys.exit(1)

        time.sleep(5)

    # --- Step 6: Generate report (with retry for rate limits) ---
    max_report_retries = 3
    report_id = None
    for report_attempt in range(max_report_retries):
        print(f"\n[6/6] Generating prediction report{f' (retry {report_attempt})' if report_attempt else ''}...")
        r = requests.post(
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

    # Fetch the report
    r = requests.get(f"{base}/api/report/{report_id}", timeout=30)
    r.raise_for_status()
    report = r.json().get("data", {})

    print(f"\n{'='*60}")
    print(f"  DONE")
    print(f"  Project:    {project_id}")
    print(f"  Simulation: {simulation_id}")
    print(f"  Report:     {report_id}")
    print(f"{'='*60}")

    # Save report markdown locally
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"prediction_{ts}.md"
    md_content = report.get("markdown_content", "")
    if md_content:
        report_path.write_text(md_content, encoding="utf-8")
        print(f"  Report saved: {report_path}")
    else:
        print("  Warning: report markdown was empty")

    # Also save the brief for reference
    brief_out = output_dir / f"brief_{ts}.md"
    brief_out.write_text(Path(md_path).read_text(encoding="utf-8"), encoding="utf-8")
    print(f"  Brief saved: {brief_out}")

    return {
        "project_id": project_id,
        "simulation_id": simulation_id,
        "report_id": report_id,
        "report_path": str(report_path),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Feed Crucix OSINT into MiroFish for trading predictions")
    parser.add_argument("--max-rounds", type=int, default=40, help="Max simulation rounds (default: 40)")
    parser.add_argument("--crucix-json", default=CRUCIX_LATEST, help="Path to latest.json")
    parser.add_argument("--dry-run", action="store_true", help="Just generate markdown, don't run simulation")
    parser.add_argument("--project-name", default=None, help="Override project name")
    parser.add_argument("--no-news", action="store_true", help="Skip news-aggregator fetch")
    args = parser.parse_args()

    # Load Crucix data
    json_path = args.crucix_json
    if not os.path.exists(json_path):
        print(f"Error: Crucix latest.json not found at {json_path}")
        sys.exit(1)

    print(f"Reading Crucix data from {json_path}...")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sweep_ts = data.get("crucix", {}).get("timestamp", "unknown")
    sources_ok = data.get("crucix", {}).get("sourcesOk", 0)
    print(f"  Sweep time: {sweep_ts}")
    print(f"  Sources OK: {sources_ok}/29")

    # Convert to markdown
    md = crucix_to_markdown(data)
    print(f"  Crucix brief: {len(md)} characters")

    # Fetch and append news-aggregator headlines
    if not args.no_news:
        print("Fetching news-aggregator (8 global sources)...")
        news_items = fetch_news_aggregator(limit=12)
        if news_items:
            news_md = news_to_markdown(news_items)
            md += "\n" + news_md
            print(f"  Added {len(news_items)} headlines from {len(set(i.get('source') for i in news_items))} sources")
        else:
            print("  No news items retrieved")
    print(f"  Total brief: {len(md)} characters")

    # Fetch social sentiment for held positions
    print("Fetching social sentiment for held positions...")
    held_symbols = get_held_symbols()
    if held_symbols:
        print(f"  Held symbols: {', '.join(held_symbols)}")
        sentiments = {}
        for sym in held_symbols[:5]:
            data = fetch_social_sentiment(sym)
            if data and not data.get("error"):
                sentiments[sym] = data
                print(f"  ✓ {sym}: {data.get('overall', '?')}")
            else:
                print(f"  ✗ {sym}: {data.get('error', 'no data') if data else 'failed'}")
        sentiment_md = sentiment_to_markdown(sentiments)
        if sentiment_md:
            md += "\n" + sentiment_md
            print(f"  Added sentiment for {len(sentiments)} symbols")
    else:
        print("  No held positions found in journal")

    # Fetch market screeners
    print("Fetching market screeners...")
    screeners = fetch_screeners()
    if screeners:
        near_high = len(screeners.get("near_52w_high", []))
        unusual_vol = len(screeners.get("unusual_volume", []))
        momentum = len(screeners.get("strong_momentum", []))
        print(f"  Near 52w high: {near_high}, Unusual volume: {unusual_vol}, Momentum: {momentum}")
        screeners_md = screeners_to_markdown(screeners)
        if screeners_md:
            md += "\n" + screeners_md
    else:
        print("  Screeners unavailable")

    # Write to temp file (or output dir for dry-run)
    if args.dry_run:
        output_dir = Path(__file__).parent.parent / "output"
        output_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        md_path = output_dir / f"brief_{ts}.md"
        md_path.write_text(md, encoding="utf-8")
        print(f"\n  Dry run — brief saved to: {md_path}")
        print(f"\n--- Preview (first 2000 chars) ---\n")
        print(md[:2000])
        return

    # Write temp file for upload
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(md)
        md_path = f.name

    try:
        project_name = args.project_name or f"Crucix Trading Intel {sweep_ts[:10]}"
        run_pipeline(md_path, args.max_rounds, project_name)
    finally:
        os.unlink(md_path)


if __name__ == "__main__":
    main()
