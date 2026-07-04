# CONFIG — central documentation of all placeholders

This project is published as a template. All platform- and account-specific
values are replaced with `{{...}}` placeholders and documented centrally here.
Before the first run they must be replaced with real values.

**Example-file pattern:** only `.env.example` is versioned. The real, filled-in
`.env` is git-ignored and stays local.

---

## 1. Environment variables (`.env`)

`cp .env.example .env`, then fill in:

| Placeholder | Variable | Description | Example format |
|---|---|---|---|
| `{{PLATFORM_BASE_URL}}` | `PLATFORM_BASE_URL` | Base URL of the auction platform, no trailing slash | `https://www.example-auctions.com` |
| `{{WEBHOOK_URL}}` | `WEBHOOK_URL` | n8n production webhook (only for the n8n path) | `https://<instance>.app.n8n.cloud/webhook/auctions` |
| `{{GOOGLE_API_KEY}}` | `GOOGLE_API_KEY` | Google AI Studio key for Gemini | `AIza...` |
| `{{NOTION_TOKEN}}` | `NOTION_TOKEN` | Notion integration token for the active auctions DB | `ntn_...` |
| `{{NOTION_DB_ID}}` | `NOTION_DB_ID` | Database ID of the active auctions DB (32 hex chars) | `0123abcd...` |
| `{{NOTION_TOKEN2}}` | `NOTION_TOKEN2` | Second Notion token with access to the history DB | `ntn_...` |
| `{{NOTION_HISTORY_DB_ID}}` | `NOTION_HISTORY_DB_ID` | Database ID of the sales history DB | `0123abcd...` |
| `{{PLATFORM_PREMIUM}}` | `PLATFORM_PREMIUM` | Buyer's premium of the platform, as a fraction | `0.10` for 10% |
| `{{PLATFORM_FIXED_FEE}}` | `PLATFORM_FIXED_FEE` | Fixed transaction fee per purchase (respective currency) | `3.0` |

All other variables in `.env.example` have working defaults.
If `PLATFORM_PREMIUM` / `PLATFORM_FIXED_FEE` are left empty or `0`, the total-price
calculation runs without a fee surcharge (no crash).

## 2. Placeholders in the code (must be edited)

These placeholders live directly in the source files because they are
platform-specific. Without replacement the scraper runs but finds no lots
or leaves fields empty.

| Placeholder | File | Description |
|---|---|---|
| `{{CATEGORY_URL_COINS}}` etc. | [auction_scraper.py](auction_scraper.py) (`CATEGORIES`) | Full category overview URLs of the platform. Freely extensible — pattern: `{"name": "Display name", "url": "https://..."}`. Important: use the category listing URL, not collection/theme pages. |
| `{{CSS_CLASS_ESTIMATE}}` | [auction_scraper.py](auction_scraper.py) (`extract_lot_html_data`) | CSS class fragment of the estimate container on the lot detail page (find it via DevTools). If left as a placeholder, no HTML estimate is extracted (no crash). |
| `{{PLATFORM_NAME}}` | [auction_sales_tracker.py](auction_sales_tracker.py) (title cleanup) | Display name of the platform as it appears as a suffix in the page title (e.g. `" | PlatformName"`). If left as a placeholder, titles keep their suffix (cosmetic). |

In the docs (README/COMMANDS) the same `{{CATEGORY_URL_*}}` placeholders appear
for further categories (`ANCIENT_COINS`, `MODERN_COINS`, `EURO_COINS`, `PRINTS`,
`CLASSICAL_ART`, `MODERN_ART`, `JEWELLERY`, `FOSSILS`, `WATCHES`, `ROLEX`) — fill
them all following the same pattern.

## 3. Placeholders in the n8n workflow

Enter these in [n8n/auction-workflow.json](n8n/auction-workflow.json) after importing into n8n:

| Placeholder | Node | Description |
|---|---|---|
| `YOUR_PERPLEXITY_KEY` | "Market Research" | Perplexity API key |
| `YOUR_CLAUDE_KEY` | "AI Rating" | Anthropic API key |
| `YOUR_NOTION_TOKEN` | "Notion Search/Update/Create" (3×) | Notion integration token |
| `$env.NOTION_DB_ID` | "Notion Search", "Notion Create Body" | Set as an n8n environment variable |

## 4. Adjustments that may be needed depending on the platform

- **Lot URL path:** the code builds lot links as
  `{PLATFORM_BASE_URL}/en/l/<lot_id>`. Adjust the path scheme in
  `auction_scraper.py` and `auction_sales_tracker.py` if needed.
- **Shipping label:** the shipping-cost extraction looks for the visible text
  `Shipping to Germany` on the lot page — adapt it to the destination
  country/language of your own platform (`auction_scraper.py`).
- **API interceptor:** the scraper captures internal JSON responses whose URL
  contains `api` and `/lots` or `/auctions`; the sales tracker reads a bids
  endpoint (`…/lots/<id>/bids`). Adjust the naming scheme if needed.
- **Fee model:** `PLATFORM_PREMIUM` (buyer's premium, as a fraction) and
  `PLATFORM_FIXED_FEE` (fixed transaction fee) are read from `.env`
  (`auction_scraper.py`, `auction_sales_tracker.py`). Without values, no fee
  is applied.
