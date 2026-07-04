#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 ErnestoBzy
"""
Auction pipeline — scraping → DDG market research → Gemini Flash rating → Notion.

Flow:
  - Lots with a platform estimate: rule-based rating (0 API calls).
  - Lots without an estimate: market research via the DuckDuckGo library + local cache
    (7-day TTL), rating via Gemini Flash-Lite without grounding.
  - Top ratings ("Once-in-a-century") are additionally verified via DuckDuckGo in the
    running Playwright browser against fresh comparison prices (confirmed or downgraded).
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

import auction_scraper as scraper

try:
    from duckduckgo_search import DDGS
    DDG_AVAILABLE = True
except ImportError:
    DDG_AVAILABLE = False


# ============================================================
# CONFIGURATION
# ============================================================

GOOGLE_API_KEY          = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_SCORING_MODEL    = os.getenv("GOOGLE_SCORING_MODEL", "gemini-2.5-flash-lite")
GOOGLE_MAX_RETRIES      = int(os.getenv("GOOGLE_MAX_RETRIES", "5"))
GOOGLE_BACKOFF_BASE_SEC = float(os.getenv("GOOGLE_BACKOFF_BASE_SEC", "4"))
REQUEST_GAP_SEC         = float(os.getenv("REQUEST_GAP_SEC", "1.5"))
DDG_GAP_SEC             = float(os.getenv("DDG_GAP_SEC", "2.0"))  # pause between DDG requests
DDG_BROWSER_TIMEOUT_SEC = int(os.getenv("DDG_BROWSER_TIMEOUT_SEC", "20"))
DDG_BROWSER_GAP_SEC     = float(os.getenv("DDG_BROWSER_GAP_SEC", "3.0"))
NOTION_TOKEN            = os.getenv("NOTION_TOKEN", "")
NOTION_DB_ID            = os.getenv("NOTION_DB_ID", "")
ESTIMATE_FACTOR = float(os.getenv("ESTIMATE_FACTOR", "0.85"))

CACHE_FILE  = Path(__file__).parent / ".price_cache.json"
CACHE_TTL_DAYS = int(os.getenv("CACHE_TTL_DAYS", "7"))

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

RATING_MAP = {
    "Once-in-a-century": "💎 Once-in-a-century",
    "Sensational":       "🔥🔥 Sensational",
    "Exceptional deal":  "🔥 Exceptional deal",
    "Very good bargain": "⭐⭐⭐ Very good bargain",
    "Good bargain":      "⭐⭐ Good bargain",
    "Offer":             "⭐ Offer",
    "Solid offer":       "✅ Solid offer",
    "No bargain":        "❌ No bargain",
}


# ============================================================
# CACHE
# ============================================================

def _load_cache():
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache):
    try:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"      ⚠️  Failed to save cache: {e}")


def _cache_key(lot):
    text = f"{lot['title'].lower().strip()}|{lot['category'].lower().strip()}"
    return hashlib.md5(text.encode()).hexdigest()[:16]


def cache_lookup(key):
    cache = _load_cache()
    entry = cache.get(key)
    if not entry:
        return None
    cached_at = datetime.fromisoformat(entry.get("cached_at", "2000-01-01T00:00:00+00:00"))
    if datetime.now(timezone.utc) - cached_at > timedelta(days=CACHE_TTL_DAYS):
        return None
    return entry


def cache_store(key, data):
    cache = _load_cache()
    cache[key] = {**data, "cached_at": datetime.now(timezone.utc).isoformat()}
    _save_cache(cache)


# ============================================================
# DUCKDUCKGO SEARCH
# ============================================================

def ddg_search(query, max_results=4):
    if not DDG_AVAILABLE:
        return []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as e:
        print(f"      ⚠️  DDG search failed: {e}")
        return []


# ============================================================
# GEMINI FLASH (no grounding)
# ============================================================

def google_generate_json(prompt, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }

    last_error = None
    for attempt in range(1, GOOGLE_MAX_RETRIES + 1):
        response = requests.post(
            url,
            params={"key": GOOGLE_API_KEY},
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        if response.status_code in (429, 503):
            last_error = requests.HTTPError(
                f"Google rate limit ({response.status_code})", response=response
            )
            if attempt < GOOGLE_MAX_RETRIES:
                wait_sec = GOOGLE_BACKOFF_BASE_SEC * attempt
                print(f"      ⏳ Google {response.status_code}, retry in {wait_sec:.1f}s")
                time.sleep(wait_sec)
                continue
            raise last_error
        response.raise_for_status()
        data = response.json()
        break
    else:
        raise RuntimeError("Google API failed")

    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("Google API: no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        raise ValueError("Google API: no text content")

    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise ValueError("No JSON in response")
        return json.loads(match.group(0))


# ============================================================
# MARKET RESEARCH (DDG + cache + Flash without grounding)
# ============================================================

def research_market_value(lot):
    key = _cache_key(lot)

    # 1. Check cache
    cached = cache_lookup(key)
    if cached:
        ratio = (lot["total_price"] / cached["market_value_eur"]) if cached["market_value_eur"] > 0 else None
        print(f"      💾 Cache — {cached['market_value_eur']}€ ({cached.get('confidence', 'n/a')})")
        return {
            "market_value_eur": cached["market_value_eur"],
            "comparison_link": cached.get("comparison_link"),
            "market_info": cached.get("market_info", ""),
            "confidence": cached.get("confidence", "medium"),
            "deal_ratio": ratio,
        }

    # 2. DuckDuckGo search
    query = f"{lot['title']} {lot['category']} auction sold price EUR"
    results = ddg_search(query, max_results=8)
    time.sleep(DDG_GAP_SEC)

    if results:
        snippets = "\n".join(
            f"{i+1}. {r.get('title','')}: {r.get('body','')[:200]}"
            for i, r in enumerate(results)
        )
    else:
        snippets = "No search results found."

    # 3. Flash without grounding — requires the MEDIAN of at least 3 sold comparisons
    prompt = (
        "Determine the market value from these search results "
        "(eBay sold, marketplace shops, auction houses — last 2 years).\n\n"
        f"SEARCH RESULTS:\n{snippets}\n\n"
        f"Title: {lot['title']}\n"
        f"Category: {lot['category']}\n"
        f"Description: {str(lot.get('description', ''))[:150]}\n"
        f"Auction estimate: {lot.get('estimate', 0)} EUR\n"
        f"Current bid: {lot.get('current_price', 0)} EUR\n"
        f"Total price incl. fees: {lot.get('total_price', 0)} EUR\n\n"
        "Rules:\n"
        "- 'market_value_eur' = MEDIAN of comparable SOLD prices (no asks, no retail).\n"
        "- 'confidence' = 'high' with >=3 comparable sold prices, 'medium' with 1-2, 'low' if uncertain/only asks/no data.\n"
        "- When confidence='low' the market_value_eur MUST be 0.\n"
        "- NEVER take the total price or the current bid as the market value.\n\n"
        "Answer only as JSON:\n"
        '{"market_value_eur": 150, "confidence": "high|medium|low", "comparison_link": "https://..." or null, "market_info": "max 50 words, state number of comparisons"}'
    )

    result = google_generate_json(prompt, GOOGLE_SCORING_MODEL)
    confidence = str(result.get("confidence") or "low").lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    market_value = max(0, float(result.get("market_value_eur") or 0))
    # Discard market value on low confidence
    if confidence == "low":
        market_value = 0
    link = result.get("comparison_link")
    comparison_link = link if isinstance(link, str) and link.startswith("http") else None
    market_info = str(result.get("market_info") or "")[:500]
    ratio = (lot["total_price"] / market_value) if market_value > 0 else None

    # 4. Only cache when a market value was found
    if market_value > 0:
        cache_store(key, {
            "market_value_eur": market_value,
            "comparison_link": comparison_link,
            "market_info": market_info,
            "confidence": confidence,
        })

    return {
        "market_value_eur": market_value,
        "comparison_link": comparison_link,
        "market_info": market_info,
        "confidence": confidence,
        "deal_ratio": ratio,
    }


# ============================================================
# NOTION — load already existing IDs
# ============================================================

def fetch_known_ids():
    """Loads all auction IDs from the active Notion DB (no status filter)."""
    if not NOTION_TOKEN or not NOTION_DB_ID:
        return set()
    ids = set()
    cursor = None
    try:
        while True:
            body = {}
            if cursor:
                body["start_cursor"] = cursor
            r = requests.post(
                f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
                headers=NOTION_HEADERS,
                json=body,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            for page in data.get("results", []):
                for t in page.get("properties", {}).get("Auction_ID", {}).get("rich_text", []):
                    ids.add(t.get("plain_text", ""))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        print(f"   🗃️  Notion: {len(ids)} entries present → will be skipped")
    except Exception as e:
        print(f"   ⚠️  Notion query failed: {e} → all lots will be processed")
    return ids


# ============================================================
# SCORING
# ============================================================

def _auto_rating(ratio):
    # Thresholds recalibrated against the real distribution (median ~0.275 before the market-value filter)
    if ratio < 0.10: return "Once-in-a-century"
    if ratio < 0.20: return "Sensational"
    if ratio < 0.35: return "Exceptional deal"
    if ratio < 0.50: return "Very good bargain"
    if ratio < 0.65: return "Good bargain"
    if ratio < 0.80: return "Offer"
    if ratio <= 1.05: return "Solid offer"
    return "No bargain"


def rate_lot_no_api(lot):
    ratio = lot["deal_ratio"]
    rating = _auto_rating(ratio)
    estimate = lot["estimate"]
    market_value = lot["market_value_eur"]
    reasoning = (
        f"Total price {lot['total_price']}€ = {round(ratio * 100)}% of the "
        f"corrected market value {market_value}€ "
        f"(platform estimate {estimate}€ × {ESTIMATE_FACTOR}). "
        f"Time left: {lot.get('time_left_h', '?')}h."
    )
    return {
        "rating_notion": RATING_MAP.get(rating, "✅ Solid offer"),
        "reasoning": reasoning[:1900],
        "risks": f"Platform estimate typically {round((1-ESTIMATE_FACTOR)*100)}% above market value.",
    }


def rate_lot(lot):
    if lot["market_value_eur"] > 0:
        market_block = (
            f"Market value per research: {lot['market_value_eur']} EUR\n"
            f"Deal ratio: {round((lot['deal_ratio'] or 0) * 100)}%\n"
            f"Source: {lot.get('comparison_link') or 'n/a'}\n"
            f"Info: {lot.get('market_info') or ''}\n"
        )
    else:
        market_block = "No reliable market value determined.\n"

    prompt = (
        "Rate this auction lot.\n\n"
        f"Title: {lot['title']}\n"
        f"Category: {lot['category']}\n"
        f"Description: {str(lot.get('description', ''))[:150]}\n"
        f"Total price: {lot['total_price']} EUR\n"
        f"Estimate: {lot.get('estimate', 0)} EUR\n"
        f"Time left: {lot.get('time_left_h', 0)} h\n"
        f"{market_block}\n"
        "Rating levels (exactly one): Once-in-a-century, Sensational, Exceptional deal, "
        "Very good bargain, Good bargain, Offer, Solid offer, No bargain.\n"
        '{"rating":"...", "reasoning":"max 60 words", "risks":"max 25 words"}'
    )
    result = google_generate_json(prompt, GOOGLE_SCORING_MODEL)
    rating = str(result.get("rating") or "Solid offer")
    return {
        "rating_notion": RATING_MAP.get(rating, "✅ Solid offer"),
        "reasoning": str(result.get("reasoning") or "Automatic rating not possible.")[:1900],
        "risks": str(result.get("risks") or "Manual review recommended.")[:1900],
    }


# ============================================================
# VERIFICATION (DDG browser scrape + Flash-Lite interpretation)
# ============================================================

def _ddg_browser_hits(page, query):
    """Opens a new tab in the existing browser session and scrapes DDG HTML hits.

    DDG bot detection requires headful mode — this only works because the pipeline
    starts the browser with headless=False.
    """
    p_page = page.context.new_page()
    try:
        url = f"https://duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        p_page.goto(url, wait_until="domcontentloaded", timeout=DDG_BROWSER_TIMEOUT_SEC * 1000)
        p_page.wait_for_timeout(2000)
        hits = p_page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('.result').forEach(r => {
                if (r.classList.contains('result--ad') || r.classList.contains('result--more')) return;
                const titleEl = r.querySelector('.result__title a, h2 a, a.result__a');
                const title = titleEl?.innerText?.trim() || '';
                const snippet = r.querySelector('.result__snippet')?.innerText?.trim() || '';
                let link = titleEl?.href || '';
                // Unwrap DDG tracking redirect
                try {
                    const u = new URL(link);
                    if (u.hostname.includes('duckduckgo.com') && u.searchParams.get('uddg')) {
                        link = decodeURIComponent(u.searchParams.get('uddg'));
                    }
                } catch(e) {}
                if (title) out.push({ title, snippet, url: link });
            });
            return out.slice(0, 15);
        }""")
        return hits or []
    finally:
        p_page.close()


def _clean_title_for_query(title):
    """Removes platform-specific formatting (dashes, 'tw.', 'kt.' etc.) for usable DDG queries."""
    t = title
    for pattern in [" - No reserve price", "No reserve price - ", " tw.", " kt.", " ct.", "ct.", "  "]:
        t = t.replace(pattern, " ")
    t = " ".join(t.replace(" - ", " ").split())
    return t[:80]


def _build_verification_queries(lot):
    """Several query strategies — from specific to general.
    The last query is intentionally broad to capture the category price range.
    """
    clean = _clean_title_for_query(lot["title"])
    cat = lot.get("category", "")
    # Shorter, broader variant (first 4-5 words) for a more general price range
    broad = " ".join(clean.split()[:5])
    return [
        f'{clean} sold price ebay',
        f'{broad} sold ebay completed',
        f'{broad} {cat} auction price EUR',
    ]


def verify_top_deal_browser(page, lot):
    """Verifies a top deal via DDG browser scrape + Gemini Flash-Lite.

    Steps:
      1. Try several queries (specific → general)
      2. Scrape hits from the first successful query
      3. Gemini Flash-Lite interprets → MEDIAN of sold prices
      4. New rating using the same threshold logic
    """
    hits = []
    used_query = ""
    for q in _build_verification_queries(lot):
        hits = _ddg_browser_hits(page, q)
        if hits:
            used_query = q
            break

    if not hits:
        return {
            "verif_changed": False,
            "verif_text": "🔬 Verification (DDG browser): no search results — rating unchanged.",
            "verif_sources": [],
        }

    snippets = "\n".join(
        f"{i+1}. {t.get('title','')[:120]} — {t.get('snippet','')[:240]} ({t.get('url','')[:80]})"
        for i, t in enumerate(hits)
    )

    # The LLM ONLY estimates the market value — the rating is computed in Python
    prompt = (
        "Estimate the market value from the DuckDuckGo hits. "
        "A rough price direction is enough — the exact item does not have to be found.\n\n"
        f"LOT:\n"
        f"Title: {lot['title']}\n"
        f"Category: {lot['category']}\n"
        f"Description: {str(lot.get('description', ''))[:200]}\n\n"
        f"DDG HITS ({len(hits)} items):\n{snippets}\n\n"
        "Procedure:\n"
        "1. Look for price figures (€, EUR, USD, $, £) in the snippets.\n"
        "2. Accept SIMILAR items as comparisons (same category, similar material, "
        "similar magnitude) — not only exact matches.\n"
        "3. Determine a plausible market value (typically: median or middle of the found range). "
        "Ignore outliers (>5x median).\n"
        "4. Confidence:\n"
        "   - 'high': >=3 clear sale prices of comparable items\n"
        "   - 'medium': 1-2 comparable prices OR 3+ rough reference values\n"
        "   - 'low': no usable price information → verified_market_value_eur=0\n"
        "5. You do NOT judge whether the lot is a good deal — give ONLY the market value + confidence.\n\n"
        "Answer only as JSON:\n"
        '{"verified_market_value_eur": 200, "confidence": "high|medium|low", '
        '"num_comparisons": 5, '
        '"price_range_eur": "e.g. 150-250 or null", '
        '"reasoning": "max 60 words — states number and range of comparisons", '
        '"comparison_source": "https://... or null"}'
    )

    result = google_generate_json(prompt, GOOGLE_SCORING_MODEL)
    confidence = str(result.get("confidence") or "low").lower()
    count = int(result.get("num_comparisons") or 0)
    verif_market_value = float(result.get("verified_market_value_eur") or 0)
    price_range = str(result.get("price_range_eur") or "")
    llm_reasoning = str(result.get("reasoning") or "")[:500]
    source = result.get("comparison_source")

    hit_urls = [t.get("url") for t in hits if t.get("url")][:5]

    if confidence == "low" or verif_market_value <= 0:
        return {
            "verif_changed": False,
            "verif_text": (
                f"🔬 Verification (DDG browser, confidence: low, {count} comparisons): "
                f"insufficient sale data — rating unchanged. {llm_reasoning}"[:1900]
            ),
            "verif_sources": hit_urls,
        }

    # Compute the rating deterministically in Python — no LLM hallucination risk
    total_price = float(lot.get("total_price") or 0)
    new_ratio = total_price / verif_market_value if verif_market_value > 0 else 1.0
    new_rating = _auto_rating(new_ratio)
    label = RATING_MAP.get(new_rating, "💎 Once-in-a-century")
    changed = label != "💎 Once-in-a-century"

    text = (
        f"🔬 Verification (DDG browser, confidence: {confidence}, {count} comparisons): "
        f"verified market value {round(verif_market_value)}€"
        f"{' (range ' + price_range + ')' if price_range and price_range != 'null' else ''}. "
        f"New ratio: {total_price}€ / {round(verif_market_value)}€ = {round(new_ratio*100, 1)}%. "
        f"{'Rating confirmed.' if not changed else f'Corrected to {new_rating}.'} "
        f"{llm_reasoning}"
    )
    if source and isinstance(source, str) and source.startswith("http"):
        text += f"\nComparison: {source}"

    return {
        "verif_changed": changed,
        "verif_label": label,
        "verif_text": text[:1900],
        "verif_market_value": verif_market_value,
        "verif_sources": hit_urls,
    }


# ============================================================
# NOTION
# ============================================================

def build_market_research_text(lot):
    if lot["market_value_eur"] <= 0:
        return "No reliable market value determined (confidence too low)."
    confidence = lot.get("confidence", "medium")
    text = (
        f"Market value: {int(round(lot['market_value_eur']))}€ | "
        f"Deal: {round((lot.get('deal_ratio') or 0) * 100)}% of market value | "
        f"Confidence: {confidence}"
    )
    if lot.get("market_info"):
        text += f"\n{lot['market_info']}"
    if lot.get("comparison_link"):
        text += f"\nComparison: {lot['comparison_link']}"
    return text[:1900]


def notion_page_id_for_auction(auction_id):
    r = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={
            "filter": {"property": "Auction_ID", "rich_text": {"equals": str(auction_id)}},
            "page_size": 1,
        },
        timeout=20,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0]["id"] if results else None


def notion_properties(lot, include_auction_id):
    props = {
        "Title":               {"title": [{"text": {"content": lot["title"][:200]}}]},
        "Category":            {"select": {"name": lot["category"]}} if lot.get("category") else {"select": None},
        "Current Price (€)":   {"number": float(lot.get("current_price") or 0)},
        "Platform Fee (€)":    {"number": float(lot.get("platform_fee") or 0)},
        "Total Price (€)":     {"number": float(lot.get("total_price") or 0)},
        "Estimate (€)":        {"number": float(lot.get("estimate") or 0)},
        "BuyNow (€)":          {"number": float(lot.get("buy_now_eur") or 0)},
        "Market Value (€)":    {"number": float(lot.get("market_value_eur") or 0)},
        "Shipping (€)":        {"number": float(lot.get("shipping_eur") or 0)},
        "Rating":              {"select": {"name": lot["rating_notion"]}},
        "Reasoning":           {"rich_text": [{"text": {"content": lot["reasoning"]}}]},
        "Risks":               {"rich_text": [{"text": {"content": lot["risks"]}}]},
        "Market Comparison":   {"rich_text": [{"text": {"content": build_market_research_text(lot)}}]},
        "Link":                {"url": lot["link"]},
    }
    if include_auction_id:
        props["Auction_ID"] = {"rich_text": [{"text": {"content": str(lot["auction_id"])}}]}
    if lot.get("closes_at"):
        props["Closes at"] = {"date": {"start": lot["closes_at"]}}
    return props


def notion_upsert(lot, dry_run=False):
    if dry_run:
        print(f"      🟡 DRY-RUN: {lot['auction_id']} | {lot['rating_notion']}")
        return

    page_id = notion_page_id_for_auction(lot["auction_id"])
    if page_id:
        requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=NOTION_HEADERS,
            json={"properties": notion_properties(lot, include_auction_id=False)},
            timeout=20,
        ).raise_for_status()
        print(f"      ♻️  Notion updated: {lot['auction_id']}")
        return

    requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={
            "parent": {"database_id": NOTION_DB_ID},
            "properties": notion_properties(lot, include_auction_id=True),
        },
        timeout=20,
    ).raise_for_status()
    print(f"      ✅ Notion created: {lot['auction_id']}")


# ============================================================
# CANDIDATE SELECTION (no fixed limit)
# ============================================================

def select_candidates(lots, max_lots):
    def deal_ratio(l):
        if l.get("estimate", 0) > 0:
            return l["total_price"] / (l["estimate"] * ESTIMATE_FACTOR)
        if l.get("buy_now_eur", 0) > 0:
            return l["total_price"] / l["buy_now_eur"]
        return None

    candidates = [l for l in lots if deal_ratio(l) is not None and deal_ratio(l) <= 1.05]
    ordered = sorted(candidates, key=deal_ratio)
    if max_lots and max_lots > 0:
        return ordered[:max_lots]
    return ordered  # 0 = no limit


# ============================================================
# CLI
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Auction pipeline — DDG + cache + browser verification"
    )
    parser.add_argument("--pages", type=int, default=scraper.MAX_PAGES)
    parser.add_argument("--hours", type=int, default=scraper.MAX_HOURS)
    parser.add_argument("--max-lots", type=int, default=0,
                        help="Max. lots per category (0 = all qualifying)")
    parser.add_argument("--cat", action="append", metavar="NAME:URL")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--cache-info", action="store_true",
                        help="Show cache statistics and exit")
    return parser.parse_args()


def show_cache_info():
    cache = _load_cache()
    now = datetime.now(timezone.utc)
    active = sum(
        1 for e in cache.values()
        if now - datetime.fromisoformat(e.get("cached_at", "2000-01-01T00:00:00+00:00"))
        <= timedelta(days=CACHE_TTL_DAYS)
    )
    print(f"Cache: {len(cache)} entries total, {active} still valid ({CACHE_TTL_DAYS}-day TTL)")
    print(f"File: {CACHE_FILE}")


def parse_categories(args):
    if not args.cat:
        return scraper.CATEGORIES
    categories = []
    for entry in args.cat:
        if ":" not in entry:
            print(f"❌ Invalid --cat format: {entry}")
            sys.exit(1)
        name, url = entry.split(":", 1)
        categories.append({"name": name.strip(), "url": url.strip()})
    return categories


# ============================================================
# MAIN
# ============================================================

def main():
    args = parse_args()

    if args.cache_info:
        show_cache_info()
        return

    if not DDG_AVAILABLE:
        print("❌ duckduckgo-search not installed: pip install duckduckgo-search")
        sys.exit(1)
    if not GOOGLE_API_KEY:
        print("❌ GOOGLE_API_KEY missing in .env")
        sys.exit(1)
    if not NOTION_TOKEN or not NOTION_DB_ID:
        print("❌ NOTION_TOKEN or NOTION_DB_ID missing in .env")
        sys.exit(1)

    scraper.MAX_HOURS = args.hours
    scraper.MAX_PAGES = args.pages
    categories = parse_categories(args)
    known_ids = fetch_known_ids()

    limit_text = str(args.max_lots) if args.max_lots > 0 else "all"
    print("=" * 62)
    print("  🚀 Auction pipeline — DDG + cache + browser verification")
    print(f"  🔍 Research: DuckDuckGo → Gemini Flash | 🔬 Top-deal verification: DDG browser")
    print(f"  💾 Cache: {CACHE_TTL_DAYS}-day TTL | {CACHE_FILE.name}")
    print(f"  📦 Lots/category: {limit_text}")
    print("=" * 62)

    processed = 0
    errors = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=300)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = context.new_page()

        for cat in categories:
            print(f"\n📂 Category: {cat['name']}")
            lots = scraper.load_category(page, cat, debug=args.debug)

            if lots:
                print(f"   🚚 Fetching shipping cost for {len(lots)} lots...")
                for idx, lot in enumerate(lots):
                    html = scraper.extract_lot_html_data(page, lot["auction_id"])
                    lot["shipping_eur"] = html["shipping_eur"]
                    if lot["estimate"] == 0 and html["estimate_html"] > 0:
                        lot["estimate"] = html["estimate_html"]
                    if (idx + 1) % 5 == 0:
                        print(f"      → {idx + 1}/{len(lots)} fetched")

            if known_ids:
                before = len(lots)
                lots = [l for l in lots if l["auction_id"] not in known_ids]
                if before - len(lots) > 0:
                    print(f"   ⏭️  {before - len(lots)} already in Notion → skipped")

            candidates = select_candidates(lots, args.max_lots)
            cache_hits = sum(1 for l in candidates if cache_lookup(_cache_key(l)) is not None)
            print(f"   📊 Candidates: {len(candidates)} | cache hits: {cache_hits} | DDG calls: {len(candidates) - cache_hits}")

            for lot in candidates:
                print(f"   🔎 Lot {lot['auction_id']}: {lot['title'][:70]}")
                try:
                    if lot.get("estimate", 0) > 0:
                        market_value_corr = round(lot["estimate"] * ESTIMATE_FACTOR, 2)
                        ratio = lot["total_price"] / market_value_corr
                        lot.update({
                            "market_value_eur": market_value_corr,
                            "comparison_link": None,
                            "market_info": f"Platform estimate {lot['estimate']}€ × {ESTIMATE_FACTOR} = {market_value_corr}€",
                            "deal_ratio": ratio,
                        })
                        lot.update(rate_lot_no_api(lot))
                        print(f"      ✨ Rule-based — {round(ratio*100)}% of corrected market value")
                    else:
                        lot.update(research_market_value(lot))
                        if REQUEST_GAP_SEC > 0:
                            time.sleep(REQUEST_GAP_SEC)
                        lot.update(rate_lot(lot))
                        if REQUEST_GAP_SEC > 0:
                            time.sleep(REQUEST_GAP_SEC)
                        print(f"      🌐 DDG research — market value: {lot.get('market_value_eur', 0)}€")

                    # Browser verification for top deals (DDG in the browser + Flash-Lite)
                    if lot.get("rating_notion") == "💎 Once-in-a-century":
                        try:
                            print(f"      🔬 Verifying top deal via DDG browser…")
                            verif = verify_top_deal_browser(page, lot)
                            lot["reasoning"] = f"{verif['verif_text']}\n\n{lot.get('reasoning', '')}"[:1900]
                            if verif.get("verif_changed") and verif.get("verif_label"):
                                prev = lot["rating_notion"]
                                lot["rating_notion"] = verif["verif_label"]
                                print(f"      ⬇️  Verification: {prev} → {verif['verif_label']}")
                            elif "confidence: low" in verif.get("verif_text", ""):
                                print(f"      ⚠️  Verification: too little comparison data — rating unchanged (no real gain)")
                            else:
                                print(f"      ✅ Verification confirms top deal")
                            if DDG_BROWSER_GAP_SEC > 0:
                                time.sleep(DDG_BROWSER_GAP_SEC)
                        except Exception as e:
                            print(f"      ⚠️  Verification failed: {e} — rating unchanged")

                    notion_upsert(lot, dry_run=args.dry_run)
                    processed += 1

                except requests.RequestException as e:
                    errors += 1
                    print(f"      ❌ API error: {e}")
                except (ValueError, KeyError, TypeError) as e:
                    errors += 1
                    print(f"      ❌ Processing error: {e}")

        browser.close()

    print(f"\n✅ Done — processed: {processed}, errors: {errors}")


if __name__ == "__main__":
    main()
