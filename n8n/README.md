# n8n path (Path B) — optional

This folder holds the **optional** n8n route. It was the project's first
architecture and is kept as an alternative to the direct Python pipeline.

## The two ways to run

- **Path A — direct pipeline (main path):** `auction_pipeline.py` does everything
  in Python (scraping → market research → rating → Notion). No n8n needed. This is
  the recommended path.
- **Path B — scraper + n8n (this folder):** `auction_scraper.py` only scrapes and
  POSTs the lots to an n8n webhook. The workflow in this folder then does the market
  research (Perplexity), rating (Claude Haiku) and the Notion upsert.

Both paths write to the same "Auctions (active)" Notion database and share the same
scraper module; the sales tracker and analysis scripts work regardless of the path.

## Setup

1. Import [`auction-workflow.json`](auction-workflow.json) into n8n
   (Workflow → Import from file).
2. Enter the credentials (see [../CONFIG.md](../CONFIG.md), section 3):
   - `YOUR_PERPLEXITY_KEY` in the **Market Research** node
   - `YOUR_CLAUDE_KEY` in the **AI Rating** node
   - `YOUR_NOTION_TOKEN` in the three **Notion** nodes
   - set `NOTION_DB_ID` as an n8n environment variable
3. Activate the workflow, then put the **production** webhook URL into `WEBHOOK_URL`
   in the project `.env` (not the `/webhook-test/...` URL).
4. Run the scraper so it feeds the webhook:

   ```bash
   python auction_scraper.py
   ```

## Node overview

```
Webhook Receive → Normalize Body → Pre-filter & Split
  → Perplexity Body → Market Research → Extract Market Research
  → Claude Body → AI Rating → Extract Rating
  → Notion Search → Check Notion Page → Exists in Notion?
       ├─ yes → Notion Update Body → Notion Update
       └─ no  → Notion Create Body → Notion Create
```

> Note: the workflow still uses Perplexity for market research. Swapping it for
> another search API (e.g. Tavily) only touches three nodes here and leaves the
> Python scraper untouched.
