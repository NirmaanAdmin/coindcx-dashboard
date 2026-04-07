import os, json, time, hmac, hashlib, requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from flask import Flask, render_template, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("COINDCX_API_KEY", "")
API_SECRET = os.environ.get("COINDCX_API_SECRET", "")
BASE_URL = "https://api.coindcx.com"
ENDPOINT = "/exchange/v1/derivatives/futures/trades"
USDT_INR = float(os.environ.get("USDT_INR_RATE", "98"))
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL_INR", "4490"))
START_DATE = os.environ.get("START_DATE", "2026-04-07")
IST = timezone(timedelta(hours=5, minutes=30))

# Cache to avoid hammering API on every page load
_cache = {"trades": [], "last_fetch": 0, "ttl": 30}


def sign_and_post(body):
    body["timestamp"] = int(round(time.time() * 1000))
    json_body = json.dumps(body, separators=(",", ":"))
    sig = hmac.new(API_SECRET.encode(), json_body.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": API_KEY,
        "X-AUTH-SIGNATURE": sig,
    }
    resp = requests.post(f"{BASE_URL}{ENDPOINT}", data=json_body, headers=headers, timeout=30)
    if resp.status_code == 200:
        return resp.json()
    return None


def fetch_all_trades_since_start():
    now = time.time()
    if now - _cache["last_fetch"] < _cache["ttl"] and _cache["trades"]:
        return _cache["trades"]

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=IST)
    start_ms = start_dt.timestamp() * 1000

    all_trades = []
    page = 1
    while True:
        result = sign_and_post({"page": page, "limit": 10})
        if not result or not isinstance(result, list) or len(result) == 0:
            break
        for t in result:
            if t.get("timestamp", 0) >= start_ms:
                all_trades.append(t)
        oldest = min(t.get("timestamp", 0) for t in result)
        if oldest < start_ms:
            break
        page += 1
        time.sleep(0.15)

    all_trades.sort(key=lambda t: t["timestamp"])
    _cache["trades"] = all_trades
    _cache["last_fetch"] = now
    return all_trades


def pair_trades(trades):
    orders = defaultdict(list)
    for t in trades:
        orders[t.get("order_id", "")].append(t)

    aggregated = []
    for oid, fills in orders.items():
        if not fills:
            continue
        side = fills[0]["side"]
        pair = fills[0]["pair"]
        total_qty = sum(float(f["quantity"]) for f in fills)
        total_value = sum(float(f["quantity"]) * float(f["price"]) for f in fills)
        total_fee = sum(float(f["fee_amount"]) for f in fills)
        avg_price = total_value / total_qty if total_qty > 0 else 0
        ts = fills[0]["timestamp"]
        aggregated.append({
            "order_id": oid, "pair": pair, "side": side,
            "qty": total_qty, "avg_price": avg_price,
            "fee_usdt": total_fee, "timestamp": ts,
        })

    aggregated.sort(key=lambda x: x["timestamp"])

    open_orders = {}
    completed = []

    for order in aggregated:
        pair = order["pair"]
        if pair not in open_orders:
            open_orders[pair] = order
        else:
            entry = open_orders.pop(pair)
            exit_o = order
            qty = entry["qty"]
            if entry["side"] == "buy":
                pnl_usdt = (exit_o["avg_price"] - entry["avg_price"]) * qty
            else:
                pnl_usdt = (entry["avg_price"] - exit_o["avg_price"]) * qty
            fees = entry["fee_usdt"] + exit_o["fee_usdt"]
            net_pnl = pnl_usdt - fees
            net_inr = net_pnl * USDT_INR
            margin_usdt = entry["avg_price"] * qty / 5
            roi = (net_pnl / margin_usdt) * 100 if margin_usdt > 0 else 0
            symbol = pair.replace("B-", "").replace("_USDT", "")

            entry_dt = datetime.fromtimestamp(entry["timestamp"] / 1000, IST)
            exit_dt = datetime.fromtimestamp(exit_o["timestamp"] / 1000, IST)

            completed.append({
                "symbol": symbol,
                "pair": pair,
                "side": entry["side"].upper(),
                "entry_price": round(entry["avg_price"], 8),
                "exit_price": round(exit_o["avg_price"], 8),
                "qty": round(qty, 4),
                "pnl_usdt": round(net_pnl, 4),
                "fees_usdt": round(fees, 4),
                "net_inr": round(net_inr, 2),
                "roi": round(roi, 2),
                "entry_time": entry_dt.strftime("%m/%d %H:%M"),
                "exit_time": exit_dt.strftime("%m/%d %H:%M"),
                "exit_date": exit_dt.strftime("%Y-%m-%d"),
                "status": "TP" if net_pnl > 0 else "SL",
            })

    return completed, open_orders


def fetch_current_prices():
    try:
        resp = requests.get(f"{BASE_URL}/exchange/ticker", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return {item["market"]: float(item["last_price"]) for item in data if item.get("last_price")}
    except:
        pass
    return {}


def build_stats(completed, open_orders):
    if not completed:
        return {"total": 0, "wins": 0, "losses": 0, "wr": 0, "net_inr": 0,
                "start": STARTING_CAPITAL, "end": STARTING_CAPITAL, "peak": STARTING_CAPITAL,
                "trough": STARTING_CAPITAL, "open_count": len(open_orders)}

    wins = [t for t in completed if t["net_inr"] > 0]
    losses = [t for t in completed if t["net_inr"] <= 0]
    total_net = sum(t["net_inr"] for t in completed)

    # Equity curve
    equity = [STARTING_CAPITAL]
    running = STARTING_CAPITAL
    for t in completed:
        running += t["net_inr"]
        equity.append(round(running, 2))

    # Daily breakdown
    daily = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in completed:
        d = t["exit_date"]
        daily[d]["trades"] += 1
        if t["net_inr"] > 0:
            daily[d]["wins"] += 1
        daily[d]["pnl"] += t["net_inr"]

    daily_list = []
    cum = STARTING_CAPITAL
    for d in sorted(daily.keys()):
        dd = daily[d]
        cum += dd["pnl"]
        daily_list.append({
            "date": d, "trades": dd["trades"], "wins": dd["wins"],
            "losses": dd["trades"] - dd["wins"],
            "wr": round(dd["wins"] / dd["trades"] * 100, 1) if dd["trades"] else 0,
            "pnl": round(dd["pnl"], 2), "cum": round(cum, 2),
        })

    # Per-symbol breakdown
    symbols = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in completed:
        symbols[t["symbol"]]["trades"] += 1
        if t["net_inr"] > 0:
            symbols[t["symbol"]]["wins"] += 1
        symbols[t["symbol"]]["pnl"] += t["net_inr"]

    symbol_list = sorted(
        [{"symbol": s, **d, "pnl": round(d["pnl"], 2),
          "wr": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0}
         for s, d in symbols.items()],
        key=lambda x: x["pnl"], reverse=True
    )

    # Open positions with current price
    prices = fetch_current_prices() if open_orders else {}
    open_list = []
    open_pnl = 0.0
    for pair, o in open_orders.items():
        sym = pair.replace("B-", "").replace("_USDT", "")
        mark = prices.get(pair, 0)
        entry = o["avg_price"]
        qty = o["qty"]
        if mark > 0:
            upnl = ((mark - entry) * qty if o["side"] == "buy" else (entry - mark) * qty)
            upnl_inr = (upnl - o["fee_usdt"]) * USDT_INR
        else:
            upnl_inr = 0
        open_pnl += upnl_inr
        open_list.append({
            "symbol": sym, "side": o["side"].upper(),
            "entry": round(entry, 8), "mark": round(mark, 8) if mark else 0,
            "upnl_inr": round(upnl_inr, 2),
        })

    longs = [t for t in completed if t["side"] == "BUY"]
    shorts = [t for t in completed if t["side"] == "SELL"]
    long_wins = [t for t in longs if t["net_inr"] > 0]
    short_wins = [t for t in shorts if t["net_inr"] > 0]

    avg_win = sum(t["net_inr"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["net_inr"] for t in losses) / len(losses) if losses else 0

    return {
        "total": len(completed),
        "wins": len(wins),
        "losses": len(losses),
        "wr": round(len(wins) / len(completed) * 100, 1),
        "long_count": len(longs),
        "short_count": len(shorts),
        "long_wr": round(len(long_wins) / len(longs) * 100, 1) if longs else 0,
        "short_wr": round(len(short_wins) / len(shorts) * 100, 1) if shorts else 0,
        "net_inr": round(total_net, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "start": STARTING_CAPITAL,
        "end": round(STARTING_CAPITAL + total_net, 2),
        "peak": max(equity),
        "trough": min(equity),
        "return_pct": round(total_net / STARTING_CAPITAL * 100, 2),
        "equity": equity,
        "daily": daily_list,
        "symbols": symbol_list,
        "open_positions": open_list,
        "open_count": len(open_orders),
        "open_pnl": round(open_pnl, 2),
        "total_fees_inr": round(sum(t["fees_usdt"] for t in completed) * USDT_INR, 2),
    }


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    trades = fetch_all_trades_since_start()
    completed, open_orders = pair_trades(trades)
    stats = build_stats(completed, open_orders)
    return jsonify({"trades": completed, "stats": stats})


@app.route("/api/refresh")
def api_refresh():
    _cache["last_fetch"] = 0
    return jsonify({"status": "cache cleared"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
