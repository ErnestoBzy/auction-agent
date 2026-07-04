#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 ErnestoBzy
"""
Sales history tracker (Playwright version).

Manual companion run to the main scraper. Reads active lots from the existing
Notion database, checks on the platform's lot pages whether the auction has
ended and the lot has sold, and writes sold lots into a separate history DB
to build up historical sales data.

Invoked manually by the user. No cron, no launchd.
"""

import sys
import time
import argparse
import os
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright


def load_env_file(filename=".env"):
    path = Path(filename)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()


def env_float(name, default=0.0):
    """Reads a number from the environment; empty/non-numeric value → default.
    This way an unreplaced {{...}} placeholder does not crash the program."""
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


# ============================================================
# CONFIGURATION
# ============================================================

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")           # active auctions DB
NOTION_TOKEN2 = os.getenv("NOTION_TOKEN2", "")         # sales history DB (separate workspace token)
NOTION_DB_ID = os.getenv("NOTION_DB_ID", "")           # active auctions
NOTION_HISTORY_DB_ID = os.getenv("NOTION_HISTORY_DB_ID", "")  # sales history
PLATFORM_BASE_URL = os.getenv("PLATFORM_BASE_URL", "")  # base URL of the auction platform, see CONFIG.md

PLATFORM_PREMIUM = env_float("PLATFORM_PREMIUM")     # buyer's premium as a fraction (e.g. 0.10 = 10%) — see CONFIG.md
PLATFORM_FIXED_FEE = env_float("PLATFORM_FIXED_FEE") # fixed transaction fee per purchase — see CONFIG.md

# How much time must have passed after auction end before we check
EXPIRY_BUFFER_MIN = 5

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

NOTION_HISTORY_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN2 or NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Property name candidates (to catch different spellings)
DATE_PROPERTY_CANDIDATES = [
    "Closes at", "Closes At", "Closing date",
    "Auction end", "End date", "Ends at",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Sales history tracker")
    parser.add_argument("--lot", action="append", metavar="AUCTION_ID",
                        help="Only check these lot ID(s) (repeatable, skips the Notion filter)")
    parser.add_argument("--max", type=int, default=0,
                        help="Max. number of lots per run (0 = all)")
    parser.add_argument("--debug", action="store_true",
                        help="Print the first API response object of the lot detail page")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write nothing to Notion, only log")
    parser.add_argument("--force", action="store_true",
                        help="Ignore existing history entries (re-test)")
    return parser.parse_args()


# ============================================================
# NOTION
# ============================================================

def notion_query_all(db_id, body=None):
    """Iterates over all pages of a Notion DB query."""
    cursor = None
    body = dict(body or {})
    while True:
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=NOTION_HEADERS,
            json=body,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        for page in data.get("results", []):
            yield page
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")


def notion_get_text(prop):
    """Reads rich_text/title content from a Notion property."""
    if not isinstance(prop, dict):
        return ""
    for key in ("rich_text", "title"):
        arr = prop.get(key)
        if isinstance(arr, list):
            return "".join(t.get("plain_text", "") for t in arr)
    return ""


def notion_get_number(prop):
    if not isinstance(prop, dict):
        return None
    val = prop.get("number")
    return val if isinstance(val, (int, float)) else None


def notion_get_select(prop):
    if not isinstance(prop, dict):
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if isinstance(sel, dict) else ""


def notion_get_url(prop):
    if not isinstance(prop, dict):
        return ""
    return prop.get("url", "") or ""


def notion_get_date(prop):
    """Reads the ISO date from a date property, or ''. """
    if not isinstance(prop, dict):
        return ""
    d = prop.get("date")
    if isinstance(d, dict):
        return d.get("start", "") or ""
    return ""


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def load_active_candidates():
    """Loads from the active DB all lots whose auction has already ended."""
    if not NOTION_TOKEN or not NOTION_DB_ID:
        print("❌ NOTION_TOKEN or NOTION_DB_ID missing — aborting")
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=EXPIRY_BUFFER_MIN)
    # The active DB has no status field — load all entries, the date filter runs in code
    body = {}

    candidates = []
    date_property = None

    for page in notion_query_all(NOTION_DB_ID, body):
        props = page.get("properties", {})

        if date_property is None:
            for cand in DATE_PROPERTY_CANDIDATES:
                if cand in props and isinstance(props[cand], dict) and "date" in props[cand]:
                    date_property = cand
                    print(f"   📅 Date property found: '{cand}'")
                    break

        closes = ""
        if date_property:
            closes = notion_get_date(props.get(date_property, {}))

        closes_dt = parse_iso(closes)

        # If no date is present: include anyway, the lot page decides
        # If a date is present: include only if already expired
        if closes_dt is not None and closes_dt > cutoff:
            continue

        candidates.append({
            "page_id": page["id"],
            "auction_id": notion_get_text(props.get("Auction_ID", {})),
            "title": notion_get_text(props.get("Title", {})),
            "category": notion_get_select(props.get("Category", {})),
            "estimate": notion_get_number(props.get("Estimate (€)", {})) or 0,
            "market_value": notion_get_number(props.get("Market Value (€)", {})) or 0,
            "buy_now": notion_get_number(props.get("BuyNow (€)", {})) or 0,
            "shipping": notion_get_number(props.get("Shipping (€)", {})) or 0,
            "rating": notion_get_select(props.get("Rating", {})),
            "link": notion_get_url(props.get("Link", {})),
            "closes_at": closes,
        })

    if date_property is None:
        print(f"   ⚠️  No date property found in the active DB — all active lots will be checked")
    print(f"   🗃️  {len(candidates)} candidates loaded for checking")
    return candidates


def load_active_entry_for_id(auction_id):
    """Looks up a single entry in the active DB — for the --lot CLI mode."""
    if not NOTION_TOKEN or not NOTION_DB_ID or not auction_id:
        return None
    try:
        r = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            headers=NOTION_HEADERS,
            json={
                "filter": {"property": "Auction_ID", "rich_text": {"equals": str(auction_id)}},
                "page_size": 1,
            },
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        page = results[0]
        props = page.get("properties", {})
        closes = notion_get_date(props.get("Closes at", {}))
        return {
            "page_id": page["id"],
            "auction_id": auction_id,
            "title": notion_get_text(props.get("Title", {})),
            "category": notion_get_select(props.get("Category", {})),
            "estimate": notion_get_number(props.get("Estimate (€)", {})) or 0,
            "market_value": notion_get_number(props.get("Market Value (€)", {})) or 0,
            "buy_now": notion_get_number(props.get("BuyNow (€)", {})) or 0,
            "shipping": notion_get_number(props.get("Shipping (€)", {})) or 0,
            "rating": notion_get_select(props.get("Rating", {})),
            "link": notion_get_url(props.get("Link", {})),
            "closes_at": closes,
        }
    except Exception as e:
        print(f"   ⚠️  Active DB lookup failed ({e})")
        return None


def archive_active_entry(page_id):
    """Archives an entry in the active auctions DB."""
    if not page_id:
        return
    try:
        r = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=NOTION_HEADERS,
            json={"archived": True},
            timeout=15,
        )
        if r.status_code == 200:
            print(f"      🗂️  Active entry archived")
        else:
            print(f"      ⚠️  Archiving failed ({r.status_code})")
    except Exception as e:
        print(f"      ⚠️  Archiving failed: {e}")


def history_has_auction_id(auction_id):
    """Checks whether a lot is already in the history DB (idempotency)."""
    if not auction_id:
        return False
    body = {
        "filter": {"property": "Auction_ID", "rich_text": {"equals": auction_id}},
        "page_size": 1,
    }
    try:
        r = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_HISTORY_DB_ID}/query",
            headers=NOTION_HISTORY_HEADERS,
            json=body,
            timeout=15,
        )
        r.raise_for_status()
        return bool(r.json().get("results"))
    except Exception as e:
        print(f"      ⚠️  History check failed ({e}) — skipping to be safe")
        return True


def history_create(entry, dry_run=False, outcome="Sold"):
    """Creates an entry in the sales history DB.

    outcome: "Sold" or "Unsold"
    """
    hammer = float(entry["hammer_price"])
    shipping = float(entry.get("shipping") or 0)
    if outcome == "Sold":
        fee = round(hammer * PLATFORM_PREMIUM + PLATFORM_FIXED_FEE, 2)
        total = round(hammer + fee + shipping, 2)
    else:
        # Not sold → no fees, no total price
        fee = 0.0
        total = 0.0

    closes_iso = entry.get("closes_at") or datetime.now(timezone.utc).isoformat()

    # Title: prefer the lot page (always current), carry-over as fallback
    title = (entry.get("title_page") or entry.get("title") or "Unknown")[:200]
    # Category: carry-over takes priority (manual curation), lot page as fallback
    category = entry.get("category") or entry.get("category_page") or ""

    properties = {
        "Title": {"title": [{"text": {"content": title}}]},
        "Auction_ID": {"rich_text": [{"text": {"content": str(entry["auction_id"])}}]},
        "Outcome": {"select": {"name": outcome}},
        "Category": {"select": {"name": category}} if category else {"select": None},
        "Sale Date": {"date": {"start": closes_iso}},
        "Hammer Price (€)": {"number": hammer if outcome == "Sold" else 0},
        "Highest Bid (€)": {"number": hammer if outcome == "Unsold" else 0},
        "Platform Fee final (€)": {"number": fee},
        "Total Price final (€)": {"number": total},
        "Estimate (€)": {"number": float(entry.get("estimate") or 0)},
        "Market Value (€)": {"number": float(entry.get("market_value") or 0)},
        "BuyNow (€)": {"number": float(entry.get("buy_now") or 0)},
        "Shipping (€)": {"number": shipping},
        "Number of Bids": {"number": int(entry.get("bid_count") or 0)},
        "Unique Bidders": {"number": int(entry.get("unique_bidders") or 0)},
        "Reserve Met": {"checkbox": bool(entry.get("reserve_met", True))},
        "Link": {"url": entry.get("link") or f"{PLATFORM_BASE_URL}/en/l/{entry['auction_id']}"},
    }

    # Set optional fields only when content is available
    if entry.get("description"):
        properties["Description"] = {
            "rich_text": [{"text": {"content": entry["description"][:1900]}}]
        }
    if entry.get("subcategory"):
        properties["Subcategory"] = {
            "rich_text": [{"text": {"content": entry["subcategory"][:200]}}]
        }
    if entry.get("auction_name"):
        properties["Auction Name"] = {
            "rich_text": [{"text": {"content": entry["auction_name"][:200]}}]
        }
    if entry.get("num_images"):
        properties["Number of Images"] = {"number": int(entry["num_images"])}
    if entry.get("auction_duration_days"):
        properties["Auction Duration (Days)"] = {"number": float(entry["auction_duration_days"])}
    if entry.get("rating"):
        properties["Rating (before)"] = {"select": {"name": entry["rating"]}}

    payload = {
        "parent": {"database_id": NOTION_HISTORY_DB_ID},
        "properties": properties,
    }

    if dry_run:
        if outcome == "Sold":
            print(f"      🟡 DRY-RUN: {outcome} — hammer {hammer:.2f}€ + fee {fee:.2f}€ "
                  f"+ shipping {shipping:.2f}€ = total {total:.2f}€, {entry.get('bid_count', 0)} bids")
        else:
            print(f"      🟡 DRY-RUN: {outcome} — highest bid {hammer:.2f}€, {entry.get('bid_count', 0)} bids")
        return True

    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HISTORY_HEADERS,
        json=payload,
        timeout=20,
    )
    if r.status_code >= 300:
        print(f"      ❌ Notion create failed ({r.status_code}): {r.text[:300]}")
        return False
    return True


# ============================================================
# SCAN LOT PAGE
# ============================================================

def scan_lot_page(page, auction_id, debug=False):
    """Opens the lot page and tries to determine the sale status.

    Returns: dict with
        status: "sold" | "unsold" | "running" | "unknown"
        hammer_price: float (only if sold)
        bid_count: int
        unique_bidders: int
        reserve_met: bool
    """
    result = {
        "status": "unknown",
        "hammer_price": 0.0,
        "bid_count": 0,
        "unique_bidders": 0,
        "reserve_met": True,
        # Data from the lot page (overrides carry-over when present)
        "title_page": "",
        "description": "",
        "subcategory": "",
        "auction_name": "",
        "category_page": "",
        "num_images": 0,
        "auction_duration_days": 0.0,
    }

    api_hits = []

    def on_response(response):
        url = response.url
        if "api" not in url and "lots" not in url:
            return
        try:
            data = response.json()
        except Exception:
            return
        api_hits.append({"url": url, "data": data})

    page.on("response", on_response)
    try:
        try:
            page.goto(
                f"{PLATFORM_BASE_URL}/en/l/{auction_id}",
                wait_until="networkidle",
                timeout=30000,
            )
            page.wait_for_timeout(2500)
        except Exception as e:
            print(f"      ⚠️  Page load failed: {e}")
            return result

        rendered = page.evaluate("""() => {
            const result = {
                next_data: null,
                data_layer: [],
                schema_offer: null,
                schema_name: '',
                schema_description: '',
                page_title: document.title || '',
                meta_description: '',
                body_text: (document.body.innerText || '').substring(0, 8000),
            };
            try { result.next_data = window.__NEXT_DATA__ || null; } catch(e) {}
            try {
                result.data_layer = (window.dataLayer || [])
                    .filter(o => o && (o.lot_state || o.lot_id))
                    .map(o => ({
                        lot_id: o.lot_id,
                        lot_state: o.lot_state,
                        BiddingStartTime: o.BiddingStartTime,
                        BiddingEndTime: o.BiddingEndTime,
                        auction_name: o.auction_name || o.auction_best_name,
                        category_L0_name: o.category_L0_name,
                        category_L1_name: o.category_L1_name,
                        category_L2_name: o.category_L2_name,
                        lot_no_of_images: o.lot_no_of_images,
                    }));
            } catch(e) {}
            const ld = document.querySelector('#ld_schema');
            if (ld) {
                try {
                    const parsed = JSON.parse(ld.textContent || '{}');
                    result.schema_offer = parsed.offers || null;
                    result.schema_name = parsed.name || '';
                    result.schema_description = parsed.description || '';
                } catch(e) {}
            }
            const md = document.querySelector('meta[name="description"]');
            if (md) result.meta_description = md.getAttribute('content') || '';
            return result;
        }""")

        # Title: schema.org > og:title > document.title (with cleanup)
        title = (rendered.get("schema_name") or "").strip()
        if not title:
            pt = (rendered.get("page_title") or "").strip()
            # Remove platform-specific title suffixes (replace {{PLATFORM_NAME}}, see CONFIG.md)
            for suffix in (" - auction online {{PLATFORM_NAME}}", " | {{PLATFORM_NAME}}", " - {{PLATFORM_NAME}}"):
                if pt.endswith(suffix):
                    pt = pt[: -len(suffix)].strip()
            title = pt
        result["title_page"] = title[:250]

        # Description: schema.org > meta description
        description = (rendered.get("schema_description") or rendered.get("meta_description") or "").strip()
        result["description"] = description[:1900]  # Notion rich_text block limit is 2000

        # Auction name & subcategory & images & duration from dataLayer
        for dl in rendered.get("data_layer", []) or []:
            if dl.get("auction_name") and not result["auction_name"]:
                result["auction_name"] = str(dl["auction_name"])[:200]
            if not result["category_page"]:
                result["category_page"] = str(dl.get("category_L0_name") or "")[:100]
            if not result["subcategory"]:
                l1 = (dl.get("category_L1_name") or "").strip()
                l2 = (dl.get("category_L2_name") or "").strip()
                parts = [p for p in (l1, l2) if p]
                if parts:
                    result["subcategory"] = " › ".join(parts)[:200]
            if not result["num_images"]:
                noi = dl.get("lot_no_of_images")
                if isinstance(noi, (int, float)) and noi > 0:
                    result["num_images"] = int(noi)
            if not result["auction_duration_days"]:
                start_dt = parse_iso(dl.get("BiddingStartTime"))
                end_dt = parse_iso(dl.get("BiddingEndTime"))
                if start_dt and end_dt and end_dt > start_dt:
                    result["auction_duration_days"] = round(
                        (end_dt - start_dt).total_seconds() / 86400, 2
                    )

        # 1) Evaluate the bids endpoint (most reliable source for hammer price & bid count)
        process_bids_endpoint(api_hits, auction_id, result)

        # 2) Fish the lot object out of __NEXT_DATA__
        next_lot = find_lot_in_next_data(rendered.get("next_data"), auction_id)
        if next_lot:
            interpret_lot_object(next_lot, result)

        # 3) Supplement from API responses
        api_lot = select_lot_from_api(api_hits, auction_id)
        if api_lot:
            interpret_lot_object(api_lot, result)

        # 4) dataLayer as an additional state indicator
        for dl in rendered.get("data_layer", []) or []:
            dl_state = (dl.get("lot_state") or "").lower()
            if dl_state and result["status"] in ("unknown", "running"):
                interpret_state(dl_state, result)

        # 5) Text fallback (the platform renders "Sold" / "Did not sell" as a banner)
        interpret_text(rendered.get("body_text", "") or "", result)

        # Reserve not met = always unsold, the bid was not a hammer price
        if not result["reserve_met"] and result["status"] == "sold":
            result["status"] = "unsold"

        if debug:
            import json as _json
            print(f"\n🔬 DEBUG Lot {auction_id}")
            print(f"  next_lot found: {next_lot is not None}")
            if next_lot:
                interesting = {k: v for k, v in next_lot.items() if k in (
                    'id','state','lot_state','auction_state','winning_bid','final_bid',
                    'current_bid_amount','bid_count','bids_count','unique_bidders_count',
                    'has_reserve','reserve_met','sold_price','hammer_price','price'
                )}
                print(f"  next_lot fields: {_json.dumps(interesting, indent=2, default=str)[:1500]}")
            print(f"  dataLayer: {rendered.get('data_layer')}")
            print(f"  schema offer: {rendered.get('schema_offer')}")
            print(f"  API responses: {len(api_hits)}")
            for f in api_hits[:8]:
                u = f['url'].split('?')[0].replace(PLATFORM_BASE_URL, '')
                print(f"    - {u}")
            print(f"  body_text start: {rendered.get('body_text','')[:600]!r}")
            print()

    finally:
        page.remove_listener("response", on_response)

    return result


def find_lot_in_next_data(next_data, auction_id):
    """Recursively finds the matching lot object in __NEXT_DATA__.

    The platform uses dehydratedState.queries — the real lot data is
    deeply nested. We accept any dict with id=auction_id and at least
    5 additional fields.
    """
    if not next_data:
        return None
    aid = str(auction_id)
    candidates = []

    def visit(obj, depth=0):
        if depth > 14:
            return
        if isinstance(obj, dict):
            if str(obj.get("id", "")) == aid and len(obj) >= 5:
                candidates.append(obj)
            for v in obj.values():
                visit(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                visit(v, depth + 1)

    visit(next_data)
    if not candidates:
        return None

    # Prefer the object with the most relevant auction fields
    relevant_keys = {
        "state", "lot_state", "current_bid_amount", "winning_bid", "final_bid",
        "bid_count", "bids_count", "auction_state", "title", "closing_date",
        "bidding_end_time", "ends_at",
    }
    return max(candidates, key=lambda o: len(set(o.keys()) & relevant_keys))


def process_bids_endpoint(api_hits, auction_id, result):
    """Reads the platform's bids endpoint (…/lots/{id}/bids) and extracts hammer price + bid count."""
    aid = str(auction_id)
    for f in api_hits:
        url = f.get("url", "")
        if f"/lots/{aid}/bids" not in url:
            continue
        data = f.get("data")
        bids = []
        if isinstance(data, dict):
            bids = data.get("bids") or data.get("data") or data.get("results") or []
        elif isinstance(data, list):
            bids = data
        if not bids:
            continue

        max_amount = 0.0
        unique_users = set()
        for bid in bids:
            if not isinstance(bid, dict):
                continue
            amount = bid.get("amount") or bid.get("bid_amount") or bid.get("price")
            if isinstance(amount, dict):
                for sub in ("EUR", "amount", "value", "price_eur"):
                    v = amount.get(sub)
                    try:
                        fv = float(v) if v else 0
                        if fv > max_amount:
                            max_amount = fv
                    except Exception:
                        pass
            elif isinstance(amount, (int, float)) and amount > max_amount:
                max_amount = float(amount)
            uid = bid.get("user_id") or bid.get("bidder_id") or bid.get("user_alias")
            if uid is not None:
                unique_users.add(uid)

        if max_amount > result["hammer_price"]:
            result["hammer_price"] = max_amount
        if len(bids) > result["bid_count"]:
            result["bid_count"] = len(bids)
        if len(unique_users) > result["unique_bidders"]:
            result["unique_bidders"] = len(unique_users)
        return  # the first matching endpoint is enough


def interpret_state(state_str, result):
    """Maps a single state string → status. For `closed`, price-/bid-based."""
    s = (state_str or "").lower()
    if not s:
        return
    if "sold" in s and "unsold" not in s and "not_sold" not in s:
        result["status"] = "sold"
    elif "unsold" in s or "not_sold" in s:
        result["status"] = "unsold"
    elif "reserve_not_met" in s:
        result["status"] = "unsold"
        result["reserve_met"] = False
    elif "closed" in s or "ended" in s or "finished" in s:
        # For `closed`, price & bids decide
        if result["hammer_price"] > 0 and result["bid_count"] > 0:
            result["status"] = "sold"
        elif result["hammer_price"] > 0 or result["bid_count"] > 0:
            result["status"] = "sold"
        else:
            result["status"] = "unsold"
    elif any(k in s for k in ("open", "running", "active", "live", "in_progress")):
        result["status"] = "running"


def interpret_lot_object(lot, result):
    """Reads a lot data object (whether from the API or __NEXT_DATA__)."""
    if not isinstance(lot, dict):
        return

    # Hammer price from various possible fields
    for key in ("winning_bid", "final_bid", "sold_price", "hammer_price"):
        val = lot.get(key)
        if isinstance(val, dict):
            for sub in ("EUR", "amount", "value", "price_eur", "amount_eur"):
                v = val.get(sub)
                try:
                    fv = float(v) if v else 0
                    if fv > result["hammer_price"]:
                        result["hammer_price"] = fv
                except Exception:
                    pass
        elif isinstance(val, (int, float)) and val > result["hammer_price"]:
            result["hammer_price"] = float(val)

    # current_bid_amount = the final price for `closed`
    cba = lot.get("current_bid_amount")
    if isinstance(cba, dict):
        for sub in ("EUR", "amount", "value"):
            v = cba.get(sub)
            try:
                fv = float(v) if v else 0
                if fv > result["hammer_price"]:
                    result["hammer_price"] = fv
            except Exception:
                pass
    elif isinstance(cba, (int, float)) and cba > result["hammer_price"]:
        result["hammer_price"] = float(cba)

    # Bid count
    for key in ("bid_count", "bids_count", "number_of_bids"):
        val = lot.get(key)
        if isinstance(val, (int, float)) and val > result["bid_count"]:
            result["bid_count"] = int(val)

    # Unique bidders
    for key in ("unique_bidders_count", "bidders_count", "number_of_bidders"):
        val = lot.get(key)
        if isinstance(val, (int, float)) and val > result["unique_bidders"]:
            result["unique_bidders"] = int(val)

    # Reserve
    if lot.get("reserve_met") is False or (lot.get("has_reserve") and lot.get("reserve_met") is False):
        result["reserve_met"] = False

    # Interpret the state last — hammer/bid info is present by now
    state = (
        lot.get("state")
        or lot.get("lot_state")
        or lot.get("auction_state")
        or ""
    )
    if state:
        interpret_state(str(state), result)


def interpret_text(text, result):
    """Detects status banners and numbers in the body innerText.

    After the auction ends the platform renders a banner as a standalone word:
      "...\nNO. 12345678\n\nSold\n{lot title}..."  → sold
      "...\nNO. 12345678\n\nUnsold\n{lot title}..."  → unsold
    """
    if not text:
        return
    import re

    # Status banner: "Sold" / "Unsold" / "Did not sell" as a standalone token
    sold_banner = re.search(r'NO\.\s*\d+\s*\n+\s*Sold\b', text)
    unsold_banner = re.search(
        r'NO\.\s*\d+\s*\n+\s*(Unsold|Did not sell|Not sold|Reserve not met|Withdrawn)\b',
        text, re.IGNORECASE
    )
    # Additionally: banner without the NO. prefix
    if not sold_banner:
        sold_banner = re.search(r'\n\s*Sold\s*\n', text)
    if not unsold_banner:
        unsold_banner = re.search(r'\n\s*(Unsold|Did not sell|Not sold|Reserve not met)\s*\n',
                                  text, re.IGNORECASE)

    # The banner is the most reliable source — it overrides a prior `unsold` assumption too
    if sold_banner:
        result["status"] = "sold"
    elif unsold_banner and result["status"] != "sold":
        result["status"] = "unsold"
        if "reserve" in unsold_banner.group(0).lower():
            result["reserve_met"] = False

    # Fallback: explicit "sold for price" phrases
    if result["status"] in ("unknown", "running") or result["hammer_price"] == 0:
        price_patterns = [
            r'Sold\s+for[^€\n]*€\s*([\d][\d.,]*)',
            r'Final\s+bid[:\s]*€\s*([\d][\d.,]*)',
            r'Winning\s+bid[:\s]*€\s*([\d][\d.,]*)',
            r'Hammer\s+price[:\s]*€\s*([\d][\d.,]*)',
            r'Current\s+bid[:\s]*€\s*([\d][\d.,]*)',
        ]
        for pat in price_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                if result["status"] == "unknown":
                    result["status"] = "sold"
                if result["hammer_price"] == 0:
                    raw = m.group(1)
                    try:
                        if ',' in raw and raw.rfind(',') > raw.rfind('.'):
                            clean = raw.replace('.', '').replace(',', '.')
                        else:
                            clean = raw.replace(',', '')
                        result["hammer_price"] = float(clean)
                    except Exception:
                        pass
                break

    if result["status"] == "unknown":
        if re.search(r'did not sell|reserve not met|auction ended without', text, re.IGNORECASE):
            result["status"] = "unsold"
        elif re.search(r'time left|ends in', text, re.IGNORECASE):
            result["status"] = "running"

    if result["bid_count"] == 0:
        m = re.search(r'(\d+)\s*bids?', text, re.IGNORECASE)
        if m:
            try: result["bid_count"] = int(m.group(1))
            except Exception: pass

    if result["unique_bidders"] == 0:
        m = re.search(r'(\d+)\s*bidders?', text, re.IGNORECASE)
        if m:
            try: result["unique_bidders"] = int(m.group(1))
            except Exception: pass

    if re.search(r'reserve not met|reserve price not met', text, re.IGNORECASE):
        result["reserve_met"] = False


def select_lot_from_api(hits, auction_id):
    """Picks the matching lot object from the captured API responses."""
    aid = str(auction_id)
    best = None
    for f in hits:
        data = f["data"]
        if isinstance(data, dict):
            if str(data.get("id", "")) == aid:
                return data
            for key in ("lot", "data"):
                inner = data.get(key)
                if isinstance(inner, dict) and str(inner.get("id", "")) == aid:
                    return inner
            for key in ("lots", "auctions"):
                arr = data.get(key)
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, dict):
                            if str(item.get("id", "")) == aid:
                                return item
                            inner_lots = item.get("lots") if isinstance(item, dict) else None
                            if isinstance(inner_lots, list):
                                for l in inner_lots:
                                    if isinstance(l, dict) and str(l.get("id", "")) == aid:
                                        return l
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and str(item.get("id", "")) == aid:
                    return item
        if best is None and isinstance(data, dict) and any(
            k in data for k in ("state", "lot_state", "winning_bid", "final_bid")
        ):
            best = data
    return best


# ============================================================
# MAIN
# ============================================================

def main():
    args = parse_args()

    if not NOTION_TOKEN:
        print("❌ NOTION_TOKEN missing (.env or environment variable) — aborting")
        sys.exit(1)
    if not NOTION_HISTORY_DB_ID and not args.dry_run:
        print("❌ NOTION_HISTORY_DB_ID missing — set it in .env, or start with --dry-run")
        sys.exit(1)
    if not NOTION_TOKEN2 and not args.dry_run:
        print("⚠️  NOTION_TOKEN2 not set — using NOTION_TOKEN for the history DB too")

    print("=" * 55)
    print("  💰  Sales history tracker")
    print(f"  ⏱️   Buffer after auction end: {EXPIRY_BUFFER_MIN} min")
    if args.dry_run:
        print("  🟡  DRY-RUN: no Notion write operations")
    print("=" * 55 + "\n")

    if args.lot:
        candidates = []
        for lot_id in args.lot:
            # Search the active DB for carry-over data (rating, market value, etc.)
            cand = load_active_entry_for_id(lot_id)
            if cand:
                print(f"   🔗 Active entry found for lot {lot_id}")
            else:
                cand = {
                    "page_id": "",
                    "auction_id": lot_id,
                    "title": "",
                    "category": "",
                    "estimate": 0,
                    "market_value": 0,
                    "buy_now": 0,
                    "shipping": 0,
                    "rating": "",
                    "link": f"{PLATFORM_BASE_URL}/en/l/{lot_id}",
                    "closes_at": "",
                }
            candidates.append(cand)
        print(f"   🎯 CLI mode: {len(candidates)} lot(s) → active DB queried")
    else:
        candidates = load_active_candidates()

    if args.max and args.max > 0:
        candidates = candidates[: args.max]
        print(f"   ✂️  Limited to {args.max} lots")

    if not candidates:
        print("\nNothing to do. Done.")
        return

    counter = {"sold_new": 0, "sold_dup": 0, "unsold": 0, "running": 0, "unknown": 0, "errors": 0}

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

        for i, cand in enumerate(candidates, 1):
            aid = cand["auction_id"]
            if not aid:
                print(f"[{i}/{len(candidates)}] ⏭️   Entry without Auction_ID skipped")
                continue

            print(f"\n[{i}/{len(candidates)}] 🔎 Lot {aid} — {cand.get('title','')[:60]}")

            scan = scan_lot_page(page, aid, debug=(args.debug and i == 1))
            status = scan["status"]
            print(f"      Status: {status}  Hammer: {scan['hammer_price']:.2f}€  "
                  f"Bids: {scan['bid_count']}  Bidders: {scan['unique_bidders']}  "
                  f"Reserve: {'OK' if scan['reserve_met'] else 'not met'}")

            if status == "running":
                counter["running"] += 1
                print(f"      ⏳ still active — skipped")
                continue
            if status == "unknown":
                counter["unknown"] += 1
                print(f"      ❓ Status not clear — skipped, retried on the next manual run")
                continue

            if status == "unsold":
                counter["unsold"] += 1
                entry = {**cand, **scan}
                ok = history_create(entry, dry_run=args.dry_run, outcome="Unsold")
                if ok:
                    print(f"      ⚪ History entry (Unsold) {'simulated' if args.dry_run else 'created'}")
                    if not args.dry_run and cand.get("page_id"):
                        archive_active_entry(cand["page_id"])
                else:
                    counter["errors"] += 1
                continue

            if scan["hammer_price"] <= 0:
                print(f"      ⚠️  Status sold but no hammer price detected — skipped")
                counter["errors"] += 1
                continue

            if not args.dry_run and not args.force and history_has_auction_id(aid):
                print(f"      ↩️  already in history — skipped (--force to replace)")
                counter["sold_dup"] += 1
                if cand.get("page_id"):
                    archive_active_entry(cand["page_id"])
                continue

            entry = {**cand, **scan}
            ok = history_create(entry, dry_run=args.dry_run, outcome="Sold")
            if ok:
                print(f"      ✅ History entry (Sold) {'simulated' if args.dry_run else 'created'}")
                counter["sold_new"] += 1
                if not args.dry_run and cand.get("page_id"):
                    archive_active_entry(cand["page_id"])
            else:
                counter["errors"] += 1

        browser.close()

    print("\n" + "=" * 55)
    print(f"  Processed: {len(candidates)} lots")
    print(f"   ✅ Sold → history          : {counter['sold_new']}")
    print(f"   ↩️  Sold (duplicate)        : {counter['sold_dup']}")
    print(f"   ⚪ Unsold → history        : {counter['unsold']}")
    print(f"   ⏳ Still active            : {counter['running']}")
    print(f"   ❓ Status unclear          : {counter['unknown']}")
    print(f"   ⚠️  Errors                  : {counter['errors']}")
    print("=" * 55)


if __name__ == "__main__":
    main()
