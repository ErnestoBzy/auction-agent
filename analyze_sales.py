#!/usr/bin/env python3
"""One-off analysis of the sales history DB.

Pulls all entries from the Notion sales history DB and answers:
in which categories/subcategories are the best deals?
"""

import os
import json
import statistics as stats
from collections import defaultdict
from pathlib import Path

import requests


def load_env_file(filename=".env"):
    path = Path(__file__).parent / filename
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

TOKEN = os.getenv("NOTION_TOKEN2") or os.getenv("NOTION_TOKEN")
DB_ID = os.getenv("NOTION_HISTORY_DB_ID")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def query_all(db_id):
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=HEADERS, json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        yield from data["results"]
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]


def text(p):
    if not p:
        return ""
    arr = p.get("rich_text") or p.get("title") or []
    return "".join(t.get("plain_text", "") for t in arr)


def num(p):
    return p.get("number") if p else None


def sel(p):
    s = (p or {}).get("select")
    return s["name"] if s else ""


def formula(p):
    f = (p or {}).get("formula") or {}
    return f.get("number") if f.get("type") == "number" else f.get("string")


def date_str(p):
    d = (p or {}).get("date")
    return d["start"] if d else ""


rows = []
for page in query_all(DB_ID):
    pr = page["properties"]
    rows.append({
        "title": text(pr.get("Title")),
        "outcome": sel(pr.get("Outcome")),
        "category": sel(pr.get("Category")),
        "subcategory": text(pr.get("Subcategory")),
        "auction_name": text(pr.get("Auction Name")),
        "sale_date": date_str(pr.get("Sale Date")),
        "hammer": num(pr.get("Hammer Price (€)")),
        "highest_bid": num(pr.get("Highest Bid (€)")),
        "total": num(pr.get("Total Price final (€)")),
        "estimate": num(pr.get("Estimate (€)")),
        "market_value": num(pr.get("Market Value (€)")),
        "bids": num(pr.get("Number of Bids")),
        "delta_market": formula(pr.get("Δ Market Value")),
        "delta_estimate": formula(pr.get("Δ Estimate")),
        "deal": formula(pr.get("Deal Quality")),
        "rating_before": sel(pr.get("Rating (before)")),
        "reserve": (pr.get("Reserve Met") or {}).get("checkbox"),
        "link": (pr.get("Link") or {}).get("url"),
    })

with open("/tmp/sales_dump.json", "w") as f:
    json.dump(rows, f, ensure_ascii=False, indent=1)

print(f"Total entries: {len(rows)}")
sold = [r for r in rows if r["outcome"] == "Sold"]
unsold = [r for r in rows if r["outcome"] == "Unsold"]
print(f"Sold: {len(sold)}  |  Unsold: {len(unsold)}")
print()


def pct(x):
    return f"{x*100:+.0f}%" if isinstance(x, (int, float)) else "—"


def group_report(key, min_n=3):
    grp = defaultdict(list)
    for r in rows:
        k = r[key] or "(empty)"
        grp[k].append(r)
    print(f"=== By {key} ===")
    lines = []
    for k, items in grp.items():
        v = [r for r in items if r["outcome"] == "Sold"]
        deltas = [r["delta_market"] for r in v if isinstance(r["delta_market"], (int, float))]
        deals = [r["deal"] or "" for r in v]
        top = sum(1 for d in deals if "💎" in d or "🔥" in d)
        unsold_rate = 1 - len(v) / len(items) if items else 0
        lines.append({
            "k": k, "n": len(items), "n_sold": len(v),
            "median_delta": stats.median(deltas) if deltas else None,
            "top_share": top / len(v) if v else None,
            "unsold": unsold_rate,
        })
    lines.sort(key=lambda z: (z["median_delta"] is None, z["median_delta"] or 0))
    for z in lines:
        md = pct(z["median_delta"])
        ts = f"{z['top_share']*100:.0f}%" if z["top_share"] is not None else "—"
        print(f"  {z['k']:<35} n={z['n']:>3} sold={z['n_sold']:>3} "
              f"medianΔMarket={md:>6} Top/Good deals={ts:>5} unsold={z['unsold']*100:.0f}%")
    print()


group_report("category")
group_report("subcategory")

# Deal quality overall distribution
print("=== Deal quality (sold lots) ===")
dealcount = defaultdict(int)
for r in sold:
    dealcount[r["deal"] or "?"] += 1
for d, c in sorted(dealcount.items(), key=lambda x: -x[1]):
    print(f"  {d:<25} {c}")
print()

# Best individual deals
print("=== Best 10 individual deals (Δ market value) ===")
best = sorted(
    (r for r in sold if isinstance(r["delta_market"], (int, float)) and (r["market_value"] or 0) > 0),
    key=lambda r: r["delta_market"],
)[:10]
for r in best:
    print(f"  {pct(r['delta_market']):>6}  {r['total']:>8.0f}€ (MV {r['market_value']:.0f}€)  "
          f"[{r['category']}] {r['title'][:60]}")
print()

# Estimate bias per category
print("=== Δ Estimate (how far below the platform estimate lots are bought) ===")
grp = defaultdict(list)
for r in sold:
    if isinstance(r["delta_estimate"], (int, float)):
        grp[r["category"] or "(empty)"].append(r["delta_estimate"])
for k, v in sorted(grp.items(), key=lambda x: stats.median(x[1])):
    print(f"  {k:<25} median={pct(stats.median(v)):>6}  n={len(v)}")
print()

# Bids vs deal
print("=== Number of bids vs. outcome (sold lots with market value) ===")
with_data = [r for r in sold if isinstance(r["delta_market"], (int, float)) and r["bids"] is not None]
for lo, hi, label in [(0, 5, "0–5"), (6, 15, "6–15"), (16, 30, "16–30"), (31, 10**9, "31+")]:
    sub = [r["delta_market"] for r in with_data if lo <= r["bids"] <= hi]
    if sub:
        print(f"  {label:>6} bids: medianΔ={pct(stats.median(sub)):>6}  n={len(sub)}")
