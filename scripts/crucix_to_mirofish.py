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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

MIROFISH_URL = os.getenv("MIROFISH_URL", "http://localhost:5005")
CRUCIX_LATEST = os.getenv(
    "CRUCIX_LATEST",
    os.path.expanduser("~/Projects/Crucix/runs/latest.json"),
)
SCHWALPACA_URL = os.getenv("SCHWALPACA_URL", "http://localhost:8855")
SCHWALPACA_API_KEY = os.getenv("SCHWALPACA_API_KEY", "")
_TIMEOUT = 10

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
            ["python3", NEWS_AGGREGATOR_SCRIPT, "--source", "all", "--limit", str(limit), "--deep"],
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
# Movers
# ---------------------------------------------------------------------------


def fetch_movers(index='$SPX.X'):
    """Fetch top gainers/losers from schwalpaca API (Schwab)."""
    try:
        headers = {"X-API-Key": SCHWALPACA_API_KEY}
        r = requests.get(
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
    """Fetch news for top movers (excluding held). Returns {symbol: [articles]}."""
    if not movers:
        return {}

    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    headers = {"X-API-Key": SCHWALPACA_API_KEY}
    results = {}
    count = 0
    for m in movers:
        if count >= 8:
            break
        sym = m.get('symbol')
        if not sym or sym in held_symbols:
            continue
        try:
            r = requests.get(
                f"{SCHWALPACA_URL}/intel/news/{sym}",
                headers=headers,
                params={"from": from_date, "limit": 3},
                timeout=_TIMEOUT,
            )
            if r.ok:
                articles = r.json()
                if articles:
                    results[sym] = articles
                    count += 1
        except Exception:
            pass
    return results


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
    """Fetch social sentiment for top movers (excluding held). Cap at 5 (60s timeout each)."""
    if not movers:
        return {}

    headers = {"X-API-Key": SCHWALPACA_API_KEY}
    results = {}
    count = 0
    for m in movers:
        if count >= 5:
            break
        sym = m.get('symbol')
        if not sym or sym in held_symbols:
            continue
        try:
            r = requests.get(
                f"{SCHWALPACA_URL}/intel/social-sentiment/{sym}",
                headers=headers,
                timeout=60,
            )
            if r.ok:
                data = r.json()
                if data and not data.get("error"):
                    results[sym] = data
                    count += 1
                    print(f"  ✓ {sym}: {data.get('overall', '?')}")
                else:
                    print(f"  ✗ {sym}: {data.get('error', 'no data') if data else 'failed'}")
        except Exception as e:
            print(f"  Warning: sentiment fetch failed for {sym}: {e}")
    return results


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
        r = requests.get(
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
        r = requests.get(
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
        r = requests.get(
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
    results = {}
    for sym in list(screener_symbols)[:10]:
        try:
            r = requests.get(
                f"{SCHWALPACA_URL}/intel/news/{sym}",
                headers=headers,
                params={"from": from_date, "limit": 3},
                timeout=10,
            )
            if r.ok:
                articles = r.json()
                if articles:
                    results[sym] = articles
        except Exception:
            pass
    return results


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


def _state_path():
    """Path to the pipeline checkpoint file."""
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    return output_dir / ".pipeline_state.json"


def _save_state(state: dict):
    """Save pipeline checkpoint."""
    state["updated_at"] = datetime.now().isoformat()
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_state() -> dict | None:
    """Load pipeline checkpoint, or None if no valid state."""
    p = _state_path()
    if not p.exists():
        return None
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        # Only resume today's runs
        today = datetime.now().strftime("%Y-%m-%d")
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


def run_pipeline(md_path: str, max_rounds: int, project_name: str, resume: bool = False):
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
            "date": datetime.now().strftime("%Y-%m-%d"),
            "completed_step": 0,
            "md_path": md_path,
            "max_rounds": max_rounds,
            "project_name": project_name,
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
        state["completed_step"] = 4
        _save_state(state)
    else:
        print("[4/6] Preparing simulation... (cached)")

    # --- Step 5: Start simulation ---
    if completed < 5:
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

        state["report_id"] = report_id
        state["completed_step"] = 6
        _save_state(state)
    else:
        report_id = state.get("report_id")
        print("[6/6] Generating prediction report... (cached)")

    # Fetch the report
    r = requests.get(f"{base}/api/report/{report_id}", timeout=30)
    r.raise_for_status()
    report = r.json().get("data", {})

    print(f"\n{'='*60}")
    print(f"  DONE")
    print(f"  Project:    {state['project_id']}")
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

    # Clean up checkpoint on success
    _clear_state()

    return {
        "project_id": state["project_id"],
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

    # Fetch news for held positions
    if held_symbols:
        print("Fetching news for held positions...")
        held_news = {}
        for sym in held_symbols[:5]:
            try:
                headers = {"X-API-Key": SCHWALPACA_API_KEY}
                r = requests.get(
                    f"{SCHWALPACA_URL}/intel/news/{sym}",
                    headers=headers,
                    params={"limit": 5},
                    timeout=_TIMEOUT,
                )
                if r.ok:
                    articles = r.json()
                    if articles:
                        held_news[sym] = articles
            except Exception:
                pass
        if held_news:
            held_news_md = screener_news_to_markdown(held_news)
            if held_news_md:
                md += "\n" + held_news_md
                print(f"  Added news for {len(held_news)} held positions")

    # Fetch market movers (before screeners — screener candidate news uses movers)
    print("Fetching market movers...")
    movers = fetch_movers()
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
            candidate_news = {}
            for sym in list(candidate_symbols)[:12]:
                try:
                    r = requests.get(
                        f"{SCHWALPACA_URL}/intel/news/{sym}",
                        headers=headers,
                        params={"from": from_date, "limit": 3},
                        timeout=_TIMEOUT,
                    )
                    if r.ok:
                        articles = r.json()
                        if articles:
                            candidate_news[sym] = articles
                except Exception:
                    pass
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
            screener_sentiments = {}
            for sym in list(screener_syms)[:5]:
                try:
                    headers = {"X-API-Key": SCHWALPACA_API_KEY}
                    r = requests.get(
                        f"{SCHWALPACA_URL}/intel/social-sentiment/{sym}",
                        headers=headers,
                        timeout=60,
                    )
                    if r.ok:
                        data = r.json()
                        if data and not data.get("error"):
                            screener_sentiments[sym] = data
                            print(f"  ✓ {sym}: {data.get('overall', '?')}")
                        else:
                            print(f"  ✗ {sym}: {data.get('error', 'no data') if data else 'failed'}")
                except Exception as e:
                    print(f"  Warning: sentiment fetch failed for {sym}: {e}")
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
        if flow_md:
            md += "\n" + flow_md
            print(f"  Added options flow for {len(all_flows)} symbols")
    else:
        print("  No symbols for options flow")

    # Earnings calendar (next 7 days)
    print("Fetching earnings calendar (next 7 days)...")
    earnings = fetch_earnings(days_ahead=7)
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

    # Write brief to stable path (survives crashes for --resume)
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    md_path = str(output_dir / f".brief_{today}.md")
    Path(md_path).write_text(md, encoding="utf-8")

    project_name = args.project_name or f"Crucix Trading Intel {sweep_ts[:10]}"
    run_pipeline(md_path, args.max_rounds, project_name)


if __name__ == "__main__":
    main()
