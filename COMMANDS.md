# Auction Agent — commands & reference

```bash
cd /path/to/project && source .venv/bin/activate
```

```bash
python auction_pipeline.py
```

```bash
python auction_pipeline.py --pages 10
```

```bash
python auction_sales_tracker.py
```

```bash
python verify_existing.py
```

---

## One-time setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Create `.env`: `cp .env.example .env`, then fill the placeholders per [CONFIG.md](CONFIG.md).

---

## Direct pipeline — DDG + cache + browser verification (main path)

```bash
python auction_pipeline.py
```

Flow: lots with a platform estimate are rated rule-based (0 API calls). Lots
without an estimate: DuckDuckGo search → Gemini Flash-Lite without grounding,
result stored in the local cache (7 days). Top ratings ("Once-in-a-century") are
additionally verified via DuckDuckGo in the running browser against fresh
comparison prices. No lot limit — all qualifying lots are rated.

### Options

| Flag | Default | Description |
|---|---|---|
| `--pages N` | 3 | Pages per category |
| `--hours N` | 48 | Only lots with ≤ N hours remaining |
| `--max-lots N` | 0 (all) | Lot limit per category (0 = unlimited) |
| `--cat "Name:URL"` | all | Single category (repeatable) |
| `--dry-run` | — | No Notion writes |
| `--debug` | — | First lot object as JSON |
| `--cache-info` | — | Show cache statistics and exit |

Cache file: `.price_cache.json` in the project directory (auto-created).

### Typical invocations

```bash
# Standard run — all qualifying lots, no limit
python auction_pipeline.py

# Check cache status
python auction_pipeline.py --cache-info

# Test a single category without writing to Notion
python auction_pipeline.py --cat "Coins:{{CATEGORY_URL_COINS}}" --dry-run

# Two categories in parallel
python auction_pipeline.py \
  --cat "Coins:{{CATEGORY_URL_COINS}}" \
  --cat "Rolex:{{CATEGORY_URL_ROLEX}}"

# With a lot limit (e.g. for testing)
python auction_pipeline.py --max-lots 10

# Debug: first lot object as JSON
python auction_pipeline.py --cat "Fossils:{{CATEGORY_URL_FOSSILS}}" --debug --dry-run
```

### Re-verify existing Notion entries

```bash
python verify_existing.py
```

Finds all current top deals + all lots with an old (potentially faulty)
verification note in the reasoning, shows the list, asks interactively. Updates
rating + reasoning with the current pipeline code.

---

## Scraper only (sends to the n8n webhook)

```bash
python auction_scraper.py [OPTIONS]
```

Optional path: instead of writing to Notion, the scraper sends the lots to the
n8n webhook, which handles research/rating (see `n8n/auction-workflow.json` and `n8n/README.md`).

### Options

| Flag | Default | Description |
|---|---|---|
| `--pages N` | 3 | Pages per category |
| `--hours N` | 48 | Max. remaining time in hours |
| `--cat "Name:URL"` | all | Single category (repeatable) |
| `--debug` | — | Print the first lot object as JSON |

### Typical invocations

```bash
# Standard run → sends all categories to n8n
python auction_scraper.py

# Test a single category
python auction_scraper.py --cat "Coins:{{CATEGORY_URL_COINS}}"

# Debug mode
python auction_scraper.py --debug --pages 1
```

---

## Sales history tracker

```bash
python auction_sales_tracker.py [OPTIONS]
```

Reads all active Notion entries with an expired `Closes at`, checks the auction
status via Playwright, and writes the result to the sales history DB.

### Options

| Flag | Default | Description |
|---|---|---|
| `--lot ID` | — | Only check this lot (ID = platform lot number) |
| `--max N` | all | Limit to N lots |
| `--dry-run` | — | No Notion writes |
| `--debug` | — | Dump: `__NEXT_DATA__`, dataLayer, API URLs, page text |
| `--force` | — | Skip the idempotency check (overwrites existing history entries) |

### Typical invocations

```bash
# Standard run — process all expired active lots
python auction_sales_tracker.py

# Test a single lot
python auction_sales_tracker.py --lot 12345678

# Single lot with debug output (diagnosis)
python auction_sales_tracker.py --lot 12345678 --debug

# Only 10 lots, without writing to Notion
python auction_sales_tracker.py --max 10 --dry-run

# Re-process a lot even though it is already in the history
python auction_sales_tracker.py --lot 12345678 --force
```

---

## Analyze the sales history (one-off)

```bash
python analyze_sales.py
```

Pulls all entries from the sales history DB and prints group reports (best
categories/subcategories, deal quality, Δ market value, bids vs. outcome).
Also writes a raw dump to `/tmp/sales_dump.json`.

---

## Files at a glance

```
auction-agent/
├── auction_pipeline.py         ← main path (no n8n): DDG + cache + browser verification
├── auction_scraper.py          scraper base module (imported by the pipeline)
├── auction_sales_tracker.py    post-auction status check of expired lots
├── verify_existing.py          re-verification of existing Notion entries
├── analyze_sales.py            one-off analysis of the sales history DB
├── n8n/
│   ├── auction-workflow.json   n8n workflow (Path B, optional)
│   └── README.md               how the n8n path works
├── .env                        local configuration (not in git)
├── .env.example                template with all variables
├── requirements.txt            Python dependencies
├── COMMANDS.md                 this file
├── README.md                   full project documentation
└── CONFIG.md                   central documentation of all {{...}} placeholders
```

---

## Environment variables (`.env`)

Full template: [.env.example](.env.example). Quick overview:

```bash
# Base URL of the auction platform (no trailing slash)
PLATFORM_BASE_URL={{PLATFORM_BASE_URL}}

# n8n webhook — only needed for auction_scraper.py
WEBHOOK_URL={{WEBHOOK_URL}}

# Platform fee model
PLATFORM_PREMIUM={{PLATFORM_PREMIUM}}        # buyer's premium as a fraction (e.g. 0.10 = 10%)
PLATFORM_FIXED_FEE={{PLATFORM_FIXED_FEE}}    # fixed transaction fee per purchase

# Google Gemini API (rating without grounding)
GOOGLE_API_KEY={{GOOGLE_API_KEY}}
GOOGLE_SCORING_MODEL=gemini-2.5-flash-lite   # rating without grounding
GOOGLE_MAX_RETRIES=5                          # retries on 429/503
GOOGLE_BACKOFF_BASE_SEC=4                     # base backoff seconds on retry
REQUEST_GAP_SEC=1.5                           # pause between API calls

# Cost control
ESTIMATE_FACTOR=0.85          # correction of platform estimate → market value
                              # 0.80 = more aggressive, 0.90 = milder

# DuckDuckGo + cache
DDG_GAP_SEC=2.0               # pause between DDG requests (rate limiting)
CACHE_TTL_DAYS=7              # cache validity in days (default: 7)

# Notion: active auctions DB
NOTION_TOKEN={{NOTION_TOKEN}}
NOTION_DB_ID={{NOTION_DB_ID}}

# Notion: sales history (separate DB, separate token)
NOTION_TOKEN2={{NOTION_TOKEN2}}
NOTION_HISTORY_DB_ID={{NOTION_HISTORY_DB_ID}}
```

---

## Category URLs

| Category | Placeholder |
|---|---|
| Coins | `{{CATEGORY_URL_COINS}}` |
| Ancient Coins | `{{CATEGORY_URL_ANCIENT_COINS}}` |
| Modern Coins | `{{CATEGORY_URL_MODERN_COINS}}` |
| Euro Coins | `{{CATEGORY_URL_EURO_COINS}}` |
| Prints | `{{CATEGORY_URL_PRINTS}}` |
| Classical Art | `{{CATEGORY_URL_CLASSICAL_ART}}` |
| Modern Art | `{{CATEGORY_URL_MODERN_ART}}` |
| Jewellery | `{{CATEGORY_URL_JEWELLERY}}` |
| Fossils | `{{CATEGORY_URL_FOSSILS}}` |
| Watches | `{{CATEGORY_URL_WATCHES}}` |
| Rolex | `{{CATEGORY_URL_ROLEX}}` |

Insert the platform's real category URLs — placeholders are documented in [CONFIG.md](CONFIG.md).

---

## Cost reference — DuckDuckGo + cache

| Situation | Cost/lot |
|---|---|
| Lot with estimate | €0.00 (rule-based) |
| Lot without estimate, cache hit | €0.00 |
| Lot without estimate, cache miss | ~€0.0001 (Flash tokens) |

**Typical full run: ~€0.00–0.05** (all categories, unlimited lots)

### Manage the cache

```bash
# Show cache statistics
python auction_pipeline.py --cache-info

# Clear the cache (if needed)
rm .price_cache.json
```

---

## Rating levels

| Level | Ratio (total price / corrected market value) |
|---|---|
| 💎 Once-in-a-century | < 10% |
| 🔥🔥 Sensational | 10–20% |
| 🔥 Exceptional deal | 20–35% |
| ⭐⭐⭐ Very good bargain | 35–50% |
| ⭐⭐ Good bargain | 50–65% |
| ⭐ Offer | 65–80% |
| ✅ Solid offer | 80–105% |
| ❌ No bargain | > 105% (not rated) |

Market value = platform estimate × `ESTIMATE_FACTOR` (default 0.85).
Thresholds per `_auto_rating` in `auction_pipeline.py` (recalibrated against the real distribution).
