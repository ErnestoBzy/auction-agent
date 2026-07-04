#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 ErnestoBzy
"""Re-verifies existing Notion entries with the pipeline's current verification code.

Finds candidates:
  1. Lots currently marked "💎 Once-in-a-century" (not yet verified)
  2. Lots with an old verification note (potentially miscategorized by a bug)

Updates rating + reasoning in Notion.
"""

import os
import re
import sys
import time
import requests
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import auction_pipeline as pipeline


def load_env():
    for line in Path(".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


def fetch_candidates(token, db_id):
    """Loads all lots that should be re-verified."""
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}
    candidates = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=headers, json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for page in data.get("results", []):
            p = page.get("properties", {})
            rating = ((p.get("Rating") or {}).get("select") or {}).get("name", "")
            reasoning = "".join(t.get("plain_text", "") for t in (p.get("Reasoning") or {}).get("rich_text", []))
            title = "".join(t.get("plain_text", "") for t in (p.get("Title") or {}).get("title", []))
            auction_id = "".join(t.get("plain_text", "") for t in (p.get("Auction_ID") or {}).get("rich_text", []))
            tp = (p.get("Total Price (€)") or {}).get("number") or 0
            mv = (p.get("Market Value (€)") or {}).get("number") or 0
            est = (p.get("Estimate (€)") or {}).get("number") or 0
            cat = ((p.get("Category") or {}).get("select") or {}).get("name", "")

            is_top_deal = rating == "💎 Once-in-a-century"
            has_verification = "🔬 Verification" in reasoning

            if is_top_deal or has_verification:
                candidates.append({
                    "page_id": page["id"],
                    "auction_id": auction_id,
                    "title": title,
                    "category": cat,
                    "total_price": tp,
                    "market_value_eur": mv,
                    "estimate": est,
                    "current_rating": rating,
                    "current_reasoning": reasoning,
                    "deal_ratio": (tp / mv) if mv > 0 else 0,
                })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return candidates


def strip_old_verification(reasoning):
    """Removes the old '🔬 Verification ...' block at the beginning."""
    # Matches up to the first double newline
    pattern = r"^🔬 Verification.*?\n\n"
    return re.sub(pattern, "", reasoning, flags=re.DOTALL).strip()


def update_notion(token, page_id, new_rating_label, new_reasoning):
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}
    payload = {
        "properties": {
            "Rating": {"select": {"name": new_rating_label}},
            "Reasoning": {"rich_text": [{"text": {"content": new_reasoning[:1900]}}]},
        }
    }
    r = requests.patch(f"https://api.notion.com/v1/pages/{page_id}",
                       headers=headers, json=payload, timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")


def main():
    load_env()
    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_DB_ID"]

    print("Loading candidates from Notion…")
    candidates = fetch_candidates(token, db_id)
    print(f"Found: {len(candidates)} lots to re-verify")

    if not candidates:
        print("Nothing to do.")
        return

    print("\nCandidate overview:")
    for k in candidates[:20]:
        print(f"  {k['auction_id']:>10s} | {k['current_rating']:30s} | {k['title'][:60]}")
    if len(candidates) > 20:
        print(f"  …and {len(candidates)-20} more")

    answer = input(f"\nContinue with {len(candidates)} re-verifications? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        return

    print("\nStarting browser…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = context.new_page()

        ok = fail = unchanged = 0
        for i, lot in enumerate(candidates, 1):
            print(f"\n[{i}/{len(candidates)}] {lot['auction_id']}: {lot['title'][:60]}")
            print(f"      Current: {lot['current_rating']} (total: {lot['total_price']}€, MV: {lot['market_value_eur']}€)")
            try:
                # For verification we must pass the ORIGINAL rating as input (top deal) so the
                # verification logic runs at all. The verify function does not check the rating_notion field.
                lot_input = {**lot, "rating_notion": "💎 Once-in-a-century", "description": ""}
                verif = pipeline.verify_top_deal_browser(page, lot_input)

                # New rating
                if verif.get("verif_changed"):
                    new_rating = verif["verif_label"]
                else:
                    new_rating = "💎 Once-in-a-century"

                # Reasoning: strip the old verification, prepend the new one
                cleaned = strip_old_verification(lot["current_reasoning"])
                new_reasoning = f"{verif['verif_text']}\n\n{cleaned}"

                update_notion(token, lot["page_id"], new_rating, new_reasoning)
                if new_rating == lot["current_rating"]:
                    print(f"      ✅ Unchanged: {new_rating}")
                    unchanged += 1
                else:
                    print(f"      ⬇️  {lot['current_rating']} → {new_rating}")
                    ok += 1
                time.sleep(3)  # rate-limit protection
            except Exception as e:
                fail += 1
                print(f"      ❌ Error: {e}")

        browser.close()

    print(f"\nDone — {ok} corrected, {unchanged} unchanged, {fail} errors")


if __name__ == "__main__":
    main()
