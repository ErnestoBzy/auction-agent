# Auction Agent

An automated bargain finder for online auction platforms: it scrapes running
lots, estimates each lot's real market value, and only flags the ones that are
genuinely cheap relative to that value — written to a Notion database.

## Background

It started with a bad purchase. I had won a coin on an auction platform whose
expert appraisal was well above my purchase price — on paper a good deal. Only
after thorough research did it turn out that the platform's experts had strongly
overstated the coin's value. The real market value was below the platform's
listed estimate.

That raised the question: how can I automate this process so that I don't fall
for inflated expert values, and only get notified when an offer is actually a
good bargain — measured against the real market price?

The long-term goal is a dedicated ML model, trained on historical sales data,
that estimates in real time for running auctions whether a bid is worth it.

---

## Architecture

The active main path is the direct Python pipeline (`auction_pipeline.py`, see
below). The n8n workflow was the first architecture and is kept as an optional
path. The sales tracker runs independently of the chosen path.

### Scraper → n8n (first architecture, optional path)

```
auction_scraper.py  (run manually)
        │
        │  Playwright, headless=False, bypasses the Akamai Bot Manager
        ▼
  Auction website
        │
        │  API interceptor (internal JSON responses)
        │  page.goto() per lot (shipping + estimate)
        ▼
  Webhook → n8n
        │
        ├──▶ Perplexity AI  (market price research, max 200 tokens)
        │         [planned: replace with Tavily]
        │
        ├──▶ Claude Haiku  (rating, max 450 tokens)
        │
        └──▶ Notion "Auctions (active)" DB
                  (create or update by Auction_ID)


auction_sales_tracker.py  (run manually, after auction end)
        │
        │  Read Notion "Auctions (active)" DB
        │  (all entries with an expired closes-at date)
        ▼
  Lot pages of the platform
        │
        │  Playwright, headless=False
        │  Platform bids API (hammer price, bids)
        │  window.__NEXT_DATA__ (lot state)
        │  window.dataLayer (lot_state, taxonomy, auction data)
        │  og:image, schema.org (title, description)
        ▼
  Notion "Sales History" DB
        │
        ├── Sold   → hammer price + all fields + archive active entry
        └── Unsold → highest bid + all fields + archive active entry
```

### Direct pipeline (no n8n) — active main path

```
auction_pipeline.py  (run manually)
        │
        │  Playwright, headless=False, API interceptor + page.goto() per lot
        ▼
  Auction website
        │
        │  Lot data (title, prices, estimate, shipping, time left)
        ▼
  Notion check: skip already existing Auction_IDs
        │
  Pre-filter: only lots ≤ 105% of the reference price (estimate × ESTIMATE_FACTOR or BuyNow)
  No lot limit — all qualifying lots are rated
        │
        ├── Estimate > 0 ──▶ Rule-based scoring (0 API calls, 0 cost)
        │                       Market value = estimate × ESTIMATE_FACTOR
        │                       Category from a ratio table
        │
        └── No estimate ─▶ Check local cache (7-day TTL)
                              │
                              ├── Cache hit ──▶ Reuse the result (0 cost)
                              │
                              └── Cache miss ──▶ DuckDuckGo search (free)
                                                   Gemini Flash without grounding (~€0.00)
                                                   Store the result in the cache
        │
  Extra stage: top ratings ("Once-in-a-century") are verified via DuckDuckGo in
  the running browser against fresh comparison prices (confirmed/downgraded)
        │
        └──▶ Notion "Auctions (active)" DB (create or update by Auction_ID)
```

---

## Project structure

```
auction-agent/
├── auction_pipeline.py         ← main path (no n8n): DDG + cache + browser verification
├── auction_scraper.py          scraper base module (imported by the pipeline)
├── auction_sales_tracker.py    sales history tracker: post-auction lot scan
├── verify_existing.py          re-verification of existing Notion entries
├── analyze_sales.py            one-off analysis of the sales history DB
├── n8n/
│   ├── auction-workflow.json   n8n workflow (Path B, optional)
│   └── README.md               how the n8n path works
├── .env                        local configuration (not versioned)
├── .env.example                template with all variables
├── .price_cache.json           local price cache (auto-created, 7-day TTL)
├── requirements.txt            Python dependencies
├── .gitignore                  excludes .env, cache, __pycache__
├── COMMANDS.md                 all commands, cost reference
└── CONFIG.md                   central documentation of all {{...}} placeholders
```

---

## Notion databases

### Auctions (active)

Filled by the main scraper via n8n or by the direct Python pipeline. Contains
running auctions.

| Field | Type | Description |
|---|---|---|
| Title | Title | Lot title |
| Auction_ID | Text | Lot ID (deduplication) |
| Category | Select | e.g. Coins, Watches, Jewellery |
| Closes at | Date | Auction end time |
| Current Price (€) | Number | Last bid at scan time |
| Platform Fee (€) | Number | Buyer's premium (fraction) + fixed fee of the platform |
| Total Price (€) | Number | Price + fee |
| Estimate (€) | Number | Platform's expert estimate (not BuyNow) |
| BuyNow (€) | Number | Buy-now price (info only) |
| Market Value (€) | Number | With estimate: estimate × ESTIMATE_FACTOR. Without: DuckDuckGo research via Gemini Flash |
| Shipping (€) | Number | Shipping cost to destination |
| Rating | Select | Rule-based (with estimate) or Gemini Flash (without estimate) |
| Reasoning | Text | Rating reasoning |
| Market Comparison | Text | Research result (DDG source or cache note) |
| Risks | Text | AI risk assessment |
| Link | URL | Direct link to the auction |
| Time Left | Formula | Hours until auction end (live) |

**Rating levels** (total price as % of the corrected market value):

| Level | Ratio | Meaning |
|---|---|---|
| 💎 Once-in-a-century | < 10% | Far below market value |
| 🔥🔥 Sensational | 10–20% | Very far below market value |
| 🔥 Exceptional deal | 20–35% | Clearly below market value |
| ⭐⭐⭐ Very good bargain | 35–50% | Well below market value |
| ⭐⭐ Good bargain | 50–65% | Below market value |
| ⭐ Offer | 65–80% | Slightly below market value |
| ✅ Solid offer | 80–105% | Fair market price range |
| ❌ No bargain | > 105% | Too expensive (filtered, not rated) |

*(Thresholds per `_auto_rating` in `auction_pipeline.py`, recalibrated against the real distribution.)*

With estimate: rule-based, 0 API calls. Market value = estimate × `ESTIMATE_FACTOR`.
Without estimate: DuckDuckGo search → Gemini Flash without grounding → cache. Cost: ~€0.00.

### Sales History

Filled by the sales tracker. Contains completed auctions — sold and unsold.

| Field | Type | Source |
|---|---|---|
| Title | Title | Lot page (schema.org / og:title) |
| Auction_ID | Text | Platform lot ID |
| Outcome | Select | `Sold` / `Unsold` |
| Category | Select | Active DB carry-over |
| Subcategory | Text | dataLayer L1 › L2 |
| Auction Name | Text | dataLayer auction_name |
| Description | Text | schema.org description |
| Sale Date | Date | Closes at, from the active DB |
| Hammer Price (€) | Number | Bids endpoint — only if Sold |
| Highest Bid (€) | Number | Bids endpoint — only if Unsold |
| Platform Fee final (€) | Number | Premium × hammer + fixed fee — only if Sold |
| Total Price final (€) | Number | Hammer + fee + shipping |
| Estimate (€) | Number | Active DB carry-over |
| Market Value (€) | Number | Active DB carry-over |
| BuyNow (€) | Number | Active DB carry-over |
| Shipping (€) | Number | Active DB carry-over |
| Rating (before) | Select | Active DB carry-over |
| Number of Bids | Number | Bids endpoint |
| Unique Bidders | Number | Bids endpoint (0, the platform anonymizes) |
| Reserve Met | Checkbox | Lot page |
| Number of Images | Number | dataLayer lot_no_of_images |
| Auction Duration (Days) | Number | BiddingEndTime − BiddingStartTime |
| Δ Market Value | Formula | (Total Price − Market Value) / Market Value in % |
| Δ Estimate | Formula | (Total Price − Estimate) / Estimate in % |
| Deal Quality | Formula | 💎 Top deal / 🔥 Good deal / ✅ Fair / ❌ Bad deal / ⚫ Not sold |
| Link | URL | Active DB carry-over |

**Deal Quality thresholds** (total price vs. market value):
- ≤ −30%: 💎 Top deal
- ≤ −15%: 🔥 Good deal
- ≤ +10%: ✅ Fair
- > +10%: ❌ Bad deal
- Outcome = Unsold: ⚫ Not sold
- Market Value = 0: ❓ no market value

---

## Setup

### Prerequisites

```bash
pip install -r requirements.txt
playwright install chromium
```

### Environment variables (`.env`)

```bash
# Base URL of the auction platform (no trailing slash)
PLATFORM_BASE_URL={{PLATFORM_BASE_URL}}

# n8n webhook (production URL, not the test URL) — only needed for the n8n path
WEBHOOK_URL={{WEBHOOK_URL}}

# Platform fee model
PLATFORM_PREMIUM={{PLATFORM_PREMIUM}}        # buyer's premium as a fraction (e.g. 0.10 = 10%)
PLATFORM_FIXED_FEE={{PLATFORM_FIXED_FEE}}    # fixed transaction fee per purchase

# Google Gemini (rating without grounding)
GOOGLE_API_KEY={{GOOGLE_API_KEY}}
GOOGLE_SCORING_MODEL=gemini-2.5-flash-lite   # rating without grounding
GOOGLE_MAX_RETRIES=5
GOOGLE_BACKOFF_BASE_SEC=4
REQUEST_GAP_SEC=1.5

# Cost control
# Platform estimates are typically 10-20% above market value → correction factor
# 0.85 = 15% discount (default). Range: 0.80–0.90
ESTIMATE_FACTOR=0.85

# DuckDuckGo + cache
DDG_GAP_SEC=2.0          # pause between DDG requests (rate limiting)
CACHE_TTL_DAYS=7         # cache validity in days

# Notion: active auctions DB
NOTION_TOKEN={{NOTION_TOKEN}}
NOTION_DB_ID={{NOTION_DB_ID}}

# Notion: sales history DB (separate token with access to the history DB)
NOTION_TOKEN2={{NOTION_TOKEN2}}
NOTION_HISTORY_DB_ID={{NOTION_HISTORY_DB_ID}}
```

Template is [.env.example](.env.example). All placeholders are documented in [CONFIG.md](CONFIG.md).

### Configure the scraper

Relevant constants in [auction_scraper.py](auction_scraper.py):

```python
MAX_HOURS = 48    # skip lots with more than X hours remaining
MIN_HOURS = 0.5   # skip lots with less than 30 minutes remaining
MAX_PAGES = 3     # pages per category (~24–48 lots/page)
```

Enable/disable categories (replace the `{{...}}` placeholders with real platform URLs):

```python
CATEGORIES = [
    {"name": "Coins",     "url": "{{CATEGORY_URL_COINS}}"},
    {"name": "Watches",   "url": "{{CATEGORY_URL_WATCHES}}"},
    # {"name": "Jewellery", "url": "{{CATEGORY_URL_JEWELLERY}}"},
]
```

### Set up the n8n workflow

1. Import `n8n/auction-workflow.json` into n8n (Workflow → Import from file) — see [n8n/README.md](n8n/README.md)
2. Enter credentials:
   - Perplexity API key *(planned: replace with Tavily)*
   - Anthropic API key (Claude Haiku)
   - Notion integration token + database ID
3. Activate the workflow → enter the production webhook URL in `.env`

### Direct pipeline without n8n

1. Set `GOOGLE_API_KEY` in `.env`
2. Optionally set the scoring model (`GOOGLE_SCORING_MODEL`)
3. Set Notion access (`NOTION_TOKEN`, `NOTION_DB_ID`)

---

## Commands

### Direct pipeline (no n8n) — main path

```bash
# Standard run (all qualifying lots, 3 pages, estimate correction 0.85)
python auction_pipeline.py

# Leanest run: 2 pages, max 5 lots/category, only 24h remaining
python auction_pipeline.py --pages 2 --hours 24 --max-lots 5

# Dry run — write nothing to Notion (API calls still run)
python auction_pipeline.py --dry-run

# Test a single category
python auction_pipeline.py --cat "Watches:{{CATEGORY_URL_WATCHES}}"

# Show cache statistics and exit
python auction_pipeline.py --cache-info

# Debug: print the first lot object as JSON
python auction_pipeline.py --debug
```

### Scraper only (sends to the n8n webhook)

```bash
# Standard run
python auction_scraper.py

# Fewer pages, tighter time window
python auction_scraper.py --pages 2 --hours 24

# Test a single category
python auction_scraper.py --cat "Coins:{{CATEGORY_URL_COINS}}"

# Debug: print the first lot object as JSON
python auction_scraper.py --debug
```

### Sales history tracker

```bash
# Standard run — scan all expired active lots
python auction_sales_tracker.py

# Test a single lot (carry-over from the active DB is loaded automatically)
python auction_sales_tracker.py --lot 12345678

# Dry run — write nothing to Notion
python auction_sales_tracker.py --dry-run

# Process only N lots
python auction_sales_tracker.py --max 10

# Debug mode — shows __NEXT_DATA__, dataLayer, API URLs, body text
python auction_sales_tracker.py --lot 12345678 --debug

# Re-test: overwrite an existing history entry
python auction_sales_tracker.py --lot 12345678 --force
```

---

## Compliance notes

Auction platforms typically protect all content by copyright and forbid
automated tools that burden their infrastructure. This project is designed for
personal market analysis and keeps within the following limits:

- **No images** — neither image files nor image URLs are stored (in the pipeline and sales tracker)
- **No personal data** — bidder data is not stored (the platform anonymizes it anyway)
- **Rate limiting** — `slow_mo=300`, `REQUEST_GAP_SEC`, 30s pause between categories
- **Prices/categories/titles only** — no full description texts for reuse
- **Private use only** — the Notion DB is not public, data is not shared

---

## Known pitfalls

**Category URLs must use the platform's category listing format, not the collection format**
Collection/theme URLs look valid in the browser but return 0 lots. Always use the
category overview URL (see CONFIG.md). This mistake was made twice.

**Headless mode must stay disabled**
`headless=False` is not optional. In headless mode Akamai detects the bot. All
scripts must run on a machine with a visible display.

**Webhook URL: test vs. production**
n8n has two URLs: the test URL (`/webhook-test/...`, only active during a manual
test) and the production URL (`/webhook/...`). The production URL must go in `.env`.

**Do not store image data**
The pipeline and the sales tracker deliberately store no image files and no image
URLs — neither in the active auctions DB nor in the sales history DB. Applies to
`auction_pipeline.py` and `auction_sales_tracker.py`.

**Google 429 (rate limit)**
The pipeline uses automatic retries with backoff for 429/503. On frequent 429s:
- increase `REQUEST_GAP_SEC` (e.g. 4–6)
- lower `--max-lots`
- optionally switch to a faster/different model

**Estimate ≠ BuyNow**
BuyNow is set by sellers themselves and is typically strongly inflated. It does
not factor into the rating — only the platform's expert estimate is used.

**Platform estimate is typically 10-20% above market value**
The pipeline corrects this bias with `ESTIMATE_FACTOR` (default: 0.85 = 15%
discount). Without the correction factor, lots would be systematically rated too
positively. Adjust the factor in `.env` as needed — range 0.80–0.90 depending on
your own experience with the categories.

**Unique bidders stays 0**
The platform anonymizes bidder IDs in the bids endpoint. The field stays empty
for now.

---

## Available categories

| Category | Placeholder |
|---|---|
| Coins | {{CATEGORY_URL_COINS}} |
| Ancient Coins | {{CATEGORY_URL_ANCIENT_COINS}} |
| Modern Coins | {{CATEGORY_URL_MODERN_COINS}} |
| Euro Coins | {{CATEGORY_URL_EURO_COINS}} |
| Prints | {{CATEGORY_URL_PRINTS}} |
| Classical Art | {{CATEGORY_URL_CLASSICAL_ART}} |
| Modern Art | {{CATEGORY_URL_MODERN_ART}} |
| Jewellery | {{CATEGORY_URL_JEWELLERY}} |
| Fossils | {{CATEGORY_URL_FOSSILS}} |
| Watches | {{CATEGORY_URL_WATCHES}} |
| Rolex | {{CATEGORY_URL_ROLEX}} |

Insert the platform's real category URLs — placeholders are documented in [CONFIG.md](CONFIG.md).
