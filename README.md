# CoinDCX Futures Dashboard

Live trading dashboard for CoinDCX futures trades.

## Quick diagnostic when something's broken

The dashboard exposes three diagnostic endpoints:

- `/api/health` — shows whether API keys are set, how old the cache is, and the last error
- `/api/diagnose` — does a one-shot single-trade fetch and reports exactly what failed
- `/api/data` — the main payload, now always returns 200 with an `error` field on failure

Open `/api/diagnose` in your browser first when the dashboard is empty or red. It will tell you whether the issue is missing keys, an auth rejection, IP whitelist, rate limit, timeout, etc.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `COINDCX_API_KEY` | Yes | — | CoinDCX API key |
| `COINDCX_API_SECRET` | Yes | — | CoinDCX API secret |
| `STARTING_CAPITAL_INR` | No | 4490 | Starting wallet balance in INR |
| `START_DATE` | No | 2026-04-07 | Start tracking from this date (YYYY-MM-DD) |
| `USDT_INR_RATE` | No | 98 | USDT to INR conversion rate |
| `PAGE_LIMIT` | No | 100 | Trades per API page (was 10 — main perf fix) |
| `MAX_PAGES` | No | 200 | Pagination safety cap |
| `CACHE_TTL` | No | 60 | Seconds between background refetches |
| `CACHE_PATH` | No | /tmp/coindcx_dashboard_cache.pkl | Disk cache file path |
| `BOT_URL` | No | (empty) | Optional URL of separate trading bot |
| `PORT` | No | 5050 | Server port (Railway sets this automatically) |

## What changed in the revival

The previous version was hitting gunicorn's 120s worker timeout on `/api/data` and crashing the worker. Specifically:

- **Pagination was 10 trades/page** with a 0.15s sleep between pages, so accounts with many trades blew past the timeout.
- **`sign_and_post` had no exception handling** — any timeout or transient network blip propagated up and killed the worker.
- **No fetch lock** — every 30s auto-refresh could stampede a still-running fetch.
- **Cache was in-process only** — every worker restart re-fetched from scratch.
- **The frontend showed a single misleading message** ("Failed to fetch data. Check API keys.") regardless of the actual cause.

This version:

- Bumps `limit` to 100 per page (10x fewer round-trips)
- Catches all `requests` exceptions and returns useful error strings
- Serializes fetches with a `threading.Lock`, so concurrent requests use cached data instead of stampeding
- Persists the cache to disk so worker restarts don't lose data
- Returns partial data on partial failure (so the UI keeps working when only the most recent page fails)
- Adds `/api/health` and `/api/diagnose` endpoints
- Frontend now displays the real error message from the server, with a stale-data warning when relevant

## Deploy to Railway

1. Push this code to a GitHub repo
2. Create a Railway service from the repo
3. Add `COINDCX_API_KEY` and `COINDCX_API_SECRET` in Variables
4. (Optional) Add a Railway volume mounted at `/data` and set `CACHE_PATH=/data/cache.pkl` for cross-redeploy persistence
5. Deploy. Visit `your-url.up.railway.app/api/diagnose` first to confirm the API is reachable.

## Local development

```bash
export COINDCX_API_KEY=xxx
export COINDCX_API_SECRET=yyy
pip install -r requirements.txt
python app.py
# Open http://localhost:5050
# Diagnostic: http://localhost:5050/api/diagnose
```

## If `/api/diagnose` returns an error

| Error | Most likely cause | Fix |
|---|---|---|
| `API keys not configured` | Env vars not set in Railway | Add them under Variables, redeploy |
| `401 unauthorized` | Keys wrong or revoked | Regenerate on coindcx.com → API Dashboard |
| `403 forbidden` | IP whitelist on the key | Either remove IP binding, or add Railway's egress IP |
| `429 rate limited` | Too many calls | Increase `CACHE_TTL` (e.g., to 120) |
| `timeout after Ns` | Network path to api.coindcx.com is slow/blocked | Try lowering `PAGE_LIMIT`, or check Railway region |
| `partial fetch: …` | First few pages worked, later one failed | Usually transient — wait for next refresh |
