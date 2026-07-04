#!/usr/bin/env python3
"""
Auction agent (Playwright version).
Runs a real Chromium instance → bypasses the Akamai Bot Manager.
"""

import sys
import time
import argparse
import os
import requests
from datetime import datetime, timezone
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
# CONFIGURATION — adjust here
# ============================================================

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PLATFORM_BASE_URL = os.getenv("PLATFORM_BASE_URL", "")  # base URL of the auction platform, see CONFIG.md
MAX_HOURS = 48            # auctions with more than X hours remaining are ignored
MIN_HOURS = 0.5           # auctions with less than 30 minutes remaining are ignored
MAX_PAGES = 3             # how many pages to click through per category
PLATFORM_PREMIUM = env_float("PLATFORM_PREMIUM")     # buyer's premium as a fraction (e.g. 0.10 = 10%) — see CONFIG.md
PLATFORM_FIXED_FEE = env_float("PLATFORM_FIXED_FEE") # fixed transaction fee per purchase — see CONFIG.md

# Notion (optional) — to skip already stored auctions.
# Set your Notion integration token, then active lots are not processed twice.
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")          # e.g. "secret_abc123..."
NOTION_DB_ID = os.getenv("NOTION_DB_ID", "")

# Categories — enter a name and the platform's category URL, the rest runs automatically.
# Replace the {{...}} placeholders with real platform URLs — see CONFIG.md
CATEGORIES = [
    {"name": "Coins",        "url": "{{CATEGORY_URL_COINS}}"},
    {"name": "Watches",      "url": "{{CATEGORY_URL_WATCHES}}"},
    {"name": "Jewellery",    "url": "{{CATEGORY_URL_JEWELLERY}}"},
    {"name": "Ancient Coins", "url": "{{CATEGORY_URL_ANCIENT_COINS}}"},
    {"name": "Prints",       "url": "{{CATEGORY_URL_PRINTS}}"},
]

# ============================================================


def parse_args():
    parser = argparse.ArgumentParser(description="Auction scraper")
    parser.add_argument("--hours", type=int, default=MAX_HOURS,
                        help=f"Max. remaining time in hours (default: {MAX_HOURS})")
    parser.add_argument("--pages", type=int, default=MAX_PAGES,
                        help=f"Max. pages per category (default: {MAX_PAGES})")
    parser.add_argument("--cat", action="append", metavar="NAME:URL",
                        help="Category as 'Name:URL' (can be used multiple times)")
    parser.add_argument("--debug", action="store_true",
                        help="Print the first lot object as JSON")
    return parser.parse_args()


def fetch_notion_ids():
    """Loads all active auction IDs from Notion to avoid double processing."""
    if not NOTION_TOKEN or not NOTION_DB_ID:
        return set()
    ids = set()
    cursor = None
    try:
        while True:
            body = {"filter": {"property": "Status", "select": {"equals": "🟢 Active"}}}
            if cursor:
                body["start_cursor"] = cursor
            r = requests.post(
                f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
                headers={
                    "Authorization": f"Bearer {NOTION_TOKEN}",
                    "Notion-Version": "2022-06-28",
                },
                json=body,
                timeout=15,
            )
            data = r.json()
            for page in data.get("results", []):
                for t in page.get("properties", {}).get("Auction_ID", {}).get("rich_text", []):
                    ids.add(t.get("plain_text", ""))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        print(f"   🗃️  Notion: {len(ids)} active auctions already stored → will be skipped")
    except Exception as e:
        print(f"   ⚠️  Notion query failed: {e} → all lots will be sent")
    return ids


def build_page_url(base_url, page_no):
    if page_no == 1:
        return base_url
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}page={page_no}"


def load_page(page, url, api_lots, seen_ids):
    def on_response(response):
        r_url = response.url
        if "api" in r_url and any(k in r_url for k in ["/lots", "/auctions"]):
            try:
                data = response.json()
                raw = (
                    data.get("lots")
                    or data.get("auctions")
                    or data.get("data")
                    or (data if isinstance(data, list) else [])
                )
                # Some endpoints nest lots inside auction objects
                lots_flat = []
                for item in (raw if isinstance(raw, list) else []):
                    if not isinstance(item, dict):
                        continue
                    inner = item.get("lots")
                    if isinstance(inner, list) and inner:
                        lots_flat.extend(inner)
                    else:
                        lots_flat.append(item)
                if lots_flat:
                    new_lots = [l for l in lots_flat if str(l.get("id", "")) not in seen_ids]
                    for l in new_lots:
                        seen_ids.add(str(l.get("id", "")))
                    if new_lots:
                        print(f"   📡 API: {r_url.split('?')[0]} → +{len(new_lots)} lots")
                    api_lots.extend(new_lots)
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="load", timeout=20000)
        page.wait_for_timeout(3000)  # give API calls (lots/auctions) time to flow through on_response

        catalog = {}
        next_data = page.evaluate("() => window.__NEXT_DATA__ || null")
        if next_data:
            page_props = next_data.get("props", {}).get("pageProps", {})
            cl = page_props.get("categoryLots", {})
            for lot in (cl.get("lots") if isinstance(cl, dict) else []) or []:
                if lot.get("id"):
                    catalog[str(lot["id"])] = lot
        return catalog
    finally:
        page.remove_listener("response", on_response)


def load_category(page, category, debug=False):
    print(f"📦  Loading {category['name']} → {category['url']}")

    api_lots = []
    seen_ids = set()
    catalog_all = {}

    for page_no in range(1, MAX_PAGES + 1):
        before = len(api_lots)
        url = build_page_url(category["url"], page_no)
        catalog_all.update(load_page(page, url, api_lots, seen_ids))
        new_count = len(api_lots) - before
        print(f"   📄 Page {page_no}/{MAX_PAGES} — +{new_count} new lots (total: {len(api_lots)})")

        if debug and api_lots:
            import json as _json
            print("\n🔬 DEBUG — first lot object (API):")
            print(_json.dumps(api_lots[0], indent=2, default=str)[:2000])
            debug = "merged"

        if new_count == 0:
            print(f"      ↳ No new lots on page {page_no}, stopping.")
            break

    merged = []
    for bid_lot in api_lots:
        lot_id = str(bid_lot.get("id", ""))
        cat = catalog_all.get(lot_id, {})
        combined = {**cat}
        for k, v in bid_lot.items():
            if v is not None:
                combined[k] = v
        merged.append(combined)

    print(f"   🔎 Lots merged: {len(merged)}")

    if debug == "merged" and merged:
        import json as _json
        print("\n🔬 DEBUG — first lot object (MERGED):")
        print(_json.dumps(merged[0], indent=2, default=str)[:3000])

    return filter_lots(merged, category["name"])


def extract_estimate(lot):
    """Returns only the real auction estimate — NOT BuyNow (too unreliable)."""
    for field in ["estimate", "price_estimate", "estimates"]:
        est = lot.get(field)
        if est is None:
            continue
        if isinstance(est, dict):
            low = float(est.get("low") or est.get("low_estimate") or est.get("min") or 0)
            high = float(est.get("high") or est.get("high_estimate") or est.get("max") or 0)
            if low > 0:
                return round((low + high) / 2) if high else int(low)
        elif isinstance(est, (int, float)) and est > 0:
            return int(est)

    low = float(lot.get("low_estimate") or 0)
    high = float(lot.get("high_estimate") or 0)
    if low > 0:
        return round((low + high) / 2) if high else int(low)

    return 0


def extract_buynow(lot):
    """Returns the BuyNow price (info only, not a reference value)."""
    buy_now = lot.get("buyNow") or {}
    if isinstance(buy_now, dict):
        bn = float(buy_now.get("price_eur") or buy_now.get("price") or 0)
        if bn > 0:
            return int(bn)
    return 0


def extract_lot_html_data(page, lot_id):
    """Fetches shipping cost + estimate via a real browser page visit (bypasses Akamai)."""
    result = {"shipping_eur": 0, "estimate_html": 0}
    try:
        page.goto(
            f"{PLATFORM_BASE_URL}/en/l/{lot_id}",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        page.wait_for_timeout(1500)

        data = page.evaluate("""() => {
            const result = { shipping: 0, estimate: 0 };

            // Estimate: platform-specific CSS class fragment — replace {{CSS_CLASS_ESTIMATE}} (CONFIG.md)
            const estEl = document.querySelector('[class*="{{CSS_CLASS_ESTIMATE}}"]');
            if (estEl) {
                const prices = [];
                estEl.querySelectorAll('span').forEach(s => {
                    const m = s.textContent.match(/[€$]?\s*([\d][,\d]*\.?[\d]*)/g);
                    if (m) m.forEach(p => {
                        const n = parseFloat(p.replace(/[^\d.]/g, ''));
                        if (n > 0) prices.push(n);
                    });
                });
                if (prices.length >= 2) result.estimate = Math.round((prices[0] + prices[prices.length-1]) / 2);
                else if (prices.length === 1) result.estimate = prices[0];
            }

            // Shipping: span with "Shipping to Germany", then the adjacent span with the price
            // (adapt the destination label to your platform/language)
            document.querySelectorAll('span').forEach(span => {
                if (result.shipping === 0 && span.textContent.trim().startsWith('Shipping to Germany')) {
                    const next = span.nextElementSibling;
                    if (next) {
                        const m = next.textContent.match(/([\d]+[.,][\d]+|[\d]+)/);
                        if (m) result.shipping = parseFloat(m[1].replace(',', '.'));
                    }
                }
            });
            return result;
        }""")

        result["shipping_eur"] = float(data.get("shipping") or 0)
        result["estimate_html"] = int(data.get("estimate") or 0)
    except Exception as e:
        print(f"      ⚠️  Lot {lot_id}: {e}")
    return result


def filter_lots(lots, category_name):
    """Filters by remaining time ≤ MAX_HOURS, computes fees."""
    now = datetime.now(timezone.utc)
    result = []
    seen = set()

    for lot in lots:
        lot_id = str(lot.get("id", ""))
        if not lot_id or lot_id in seen:
            continue
        seen.add(lot_id)

        closing_raw = (
            lot.get("bidding_end_time")
            or lot.get("closing_date")
            or lot.get("ends_at")
            or lot.get("end_time")
            or lot.get("end_date")
        )
        if not closing_raw:
            continue

        try:
            closing = datetime.fromisoformat(str(closing_raw).replace("Z", "+00:00"))
            hours = (closing - now).total_seconds() / 3600
        except Exception:
            continue

        if hours <= MIN_HOURS or hours > MAX_HOURS:
            continue

        price = 0
        cba = lot.get("current_bid_amount")
        if isinstance(cba, dict):
            price = float(cba.get("EUR") or cba.get("USD") or cba.get("GBP") or 0)
        if not price:
            for price_field in ["current_bid", "current_price", "price", "minimum_bid", "start_bid"]:
                cp = lot.get(price_field)
                if cp is None:
                    continue
                if isinstance(cp, dict):
                    val = cp.get("amount") or cp.get("value") or cp.get("euros") or 0
                    if val:
                        price = float(val)
                        break
                elif isinstance(cp, (int, float)) and cp > 0:
                    price = float(cp)
                    break

        estimate = extract_estimate(lot)
        buy_now = extract_buynow(lot)
        platform_fee = round(price * PLATFORM_PREMIUM + PLATFORM_FIXED_FEE, 2)
        total_price = round(price + platform_fee, 2)

        result.append({
            "auction_id": lot_id,
            "title": str(lot.get("title") or lot.get("name") or "Unknown")[:250],
            "description": str(lot.get("description") or lot.get("subtitle") or "")[:400],
            "category": category_name,
            "current_price": price,
            "estimate": float(estimate),     # real auction estimate (0 if not available)
            "buy_now_eur": float(buy_now),   # BuyNow price (info only, not a reference value)
            "platform_fee": platform_fee,
            "total_price": total_price,
            "shipping_eur": 0,               # filled in main() via HTML fetch
            "time_left_h": round(hours, 1),
            "closes_at": closing.isoformat(),
            "link": lot.get("url") or f"{PLATFORM_BASE_URL}/en/l/{lot_id}",
        })

    return result


def send_category(name, lots):
    """Sends one category to n8n and waits for the response."""
    print(f"\n📤  Sending {len(lots)} lots ({name}) to n8n...")
    try:
        r = requests.post(
            WEBHOOK_URL,
            json={"lots": lots},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        print(f"✅  {name}: sent successfully (HTTP {r.status_code})")
        return True
    except Exception as e:
        print(f"❌  {name}: error while sending — {e}")
        return False


def main():
    args = parse_args()

    if not WEBHOOK_URL:
        print("❌ WEBHOOK_URL is missing. Please set it in .env or as an environment variable.")
        sys.exit(1)

    global MAX_HOURS, MAX_PAGES
    MAX_HOURS = args.hours
    MAX_PAGES = args.pages
    categories = CATEGORIES

    if args.cat:
        categories = []
        for entry in args.cat:
            if ":" not in entry:
                print(f"⚠️  Invalid format '{entry}' — expected 'Name:URL'")
                sys.exit(1)
            name, url = entry.split(":", 1)
            categories.append({"name": name.strip(), "url": url.strip()})

    print("=" * 55)
    print("  🔍  Auction search (Playwright)")
    print(f"  ⏱️   Filter: ≤ {MAX_HOURS} hours remaining")
    print(f"  📄  Max. pages per category: {MAX_PAGES}")
    print(f"  📂  Categories: {', '.join(k['name'] for k in categories)}")
    print("=" * 55 + "\n")

    # Load IDs already stored in Notion (prevents double processing)
    notion_ids = fetch_notion_ids()

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

        for i, cat in enumerate(categories):
            lots = load_category(page, cat, debug=args.debug)

            # Extract shipping cost via HTML request
            if lots:
                print(f"   🚚 Fetching shipping cost for {len(lots)} lots...")
                for j, lot in enumerate(lots):
                    data = extract_lot_html_data(page, lot["auction_id"])
                    lot["shipping_eur"] = data["shipping_eur"]
                    # Use the HTML estimate only if the API did not provide one
                    if lot["estimate"] == 0 and data["estimate_html"] > 0:
                        lot["estimate"] = data["estimate_html"]
                    if (j + 1) % 5 == 0:
                        print(f"      → {j + 1}/{len(lots)} fetched")

            # Skip already stored lots
            if notion_ids:
                before = len(lots)
                lots = [l for l in lots if l["auction_id"] not in notion_ids]
                skipped = before - len(lots)
                if skipped:
                    print(f"   ⏭️  {skipped} already in Notion → skipped")

            print(f"   → {len(lots)} new lots\n")

            if lots:
                send_category(cat["name"], lots)
                # Pause between categories so n8n can process
                if i < len(categories) - 1:
                    print("   ⏳ Waiting 30s before the next category...")
                    time.sleep(30)

        browser.close()

    print("\n✅  All categories processed. Results appear in Notion.")


if __name__ == "__main__":
    main()
