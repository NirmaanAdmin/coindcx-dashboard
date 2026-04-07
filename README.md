# CoinDCX Futures Dashboard

Live trading dashboard for CoinDCX futures trades.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `COINDCX_API_KEY` | Yes | — | CoinDCX API key |
| `COINDCX_API_SECRET` | Yes | — | CoinDCX API secret |
| `STARTING_CAPITAL_INR` | No | 4490 | Starting wallet balance in INR |
| `START_DATE` | No | 2026-04-07 | Start tracking from this date (YYYY-MM-DD) |
| `USDT_INR_RATE` | No | 98 | USDT to INR conversion rate |
| `PORT` | No | 5050 | Server port (Railway sets this automatically) |

## Deploy to Railway

1. Create new GitHub repo and push this code
2. Create new Railway service from the repo
3. Add environment variables above
4. Deploy — dashboard will be at your Railway URL

## Local Development

```bash
export COINDCX_API_KEY=xxx
export COINDCX_API_SECRET=yyy
pip install -r requirements.txt
python app.py
# Open http://localhost:5050
```
