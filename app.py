import os, json, time, hmac, hashlib, threading, logging, pickle
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, render_template, jsonify

# ---------- logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("dashboard")

app = Flask(__name__)

# ---------- config ----------
API_KEY = os.environ.get("COINDCX_API_KEY", "").strip()
API_SECRET = os.environ.get("COINDCX_API_SECRET", "").strip()
BASE_URL = "https://api.coindcx.com"
ENDPOINT = os.environ.get("COINDCX_TRADES_ENDPOINT", "/exchange/v1/derivatives/futures/trades")
USDT_INR = float(os.environ.get("USDT_INR_RATE", "98"))
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL_INR", "4490"))
START_DATE = os.environ.get("START_DATE", "2026-04-07")
BOT_URL = os.environ.get("BOT_URL", "")
PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "100"))    # was 10 — biggest single perf win
MAX_PAGES = int(os.environ.get("MAX_PAGES", "200"))      # safety guard against runaway pagination
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))       # seconds
CACHE_PATH = os.environ.get("CACHE_PATH", "/tmp/coindcx_dashboard_cache.pkl")
REQUEST_TIMEOUT = (10, 25)                                # (connect, read) seconds
IST = timezone(timedelta(hours=5, minutes=30))

# ---------- shared state ----------
_state = {
    "trades": [],
    "last_fetch_ok": 0,        # epoch of last successful fetch
    "last_fetch_attempt": 0,
    "last_error": None,
    "fetch_in_progress": False,
}
_state_lock = threading.Lock()
_fetch_lock = threading.Lock()  # serialize fetches

# ---------- requests session w/ retries ----------
_session = requests.Session()
_retry = Retry(
    total=3, backoff_factor=0.5,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset(["POST", "GET"]),
)
_session.mount("https://", HTTPAdapter(max_retries=_retry, pool_connections=4, pool_maxsize=4))


# ---------- disk cache (survives worker restarts within same container) ----------
def _load_disk_cache():
    try:
        with open(CACHE_PATH, "rb") as f:
            data = pickle.load(f)
            if isinstance(data, dict) and "trades" in data:
                with _state_lock:
                    _state["trades"] = data.get("trades", [])
                    _state["last_fetch_ok"] = data.get("last_fetch_ok", 0)
                log.info(f"loaded {len(_state['trades'])} trades from disk cache (age {int(time.time() - _state['last_fetch_ok'])}s)")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"could not load disk cache: {e}")


def _save_disk_cache():
    try:
        with open(CACHE_PATH, "wb") as f:
            pickle.dump({
                "trades": _state["trades"],
                "last_fetch_ok": _state["last_fetch_ok"],
            }, f)
    except Exception as e:
        log.warning(f"could not save disk cache: {e}")


_load_disk_cache()


# ---------- CoinDCX API ----------
def sign_and_post(body):
    """Returns (data, error_string). data is parsed JSON or None. error_string is None on success."""
    if not API_KEY or not API_SECRET:
        return None, "API keys not configured (COINDCX_API_KEY / COINDCX_API_SECRET)"
    body = dict(body)
    body["timestamp"] = int(round(time.time() * 1000))
    json_body = json.dumps(body, separators=(",", ":"))
    sig = hmac.new(API_SECRET.encode(), json_body.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": API_KEY,
        "X-AUTH-SIGNATURE": sig,
    }
    try:
        resp = _session.post(
            f"{BASE_URL}{ENDPOINT}",
            data=json_body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        return None, f"timeout after {REQUEST_TIMEOUT[1]}s contacting CoinDCX"
    except requests.exceptions.ConnectionError as e:
        return None, f"connection error: {type(e).__name__}"
    except requests.exceptions.RequestException as e:
        return None, f"request error: {type(e).__name__}: {e}"

    if resp.status_code == 200:
        try:
            return resp.json(), None
        except ValueError:
            return None, f"non-JSON response (status 200)"

    # Try to extract a useful message from the response body
    body_snippet = resp.text[:200] if resp.text else ""
    if resp.status_code == 401:
        return None, f"401 unauthorized — API keys rejected. {body_snippet}"
    if resp.status_code == 403:
        return None, f"403 forbidden — IP whitelist or permission issue. {body_snippet}"
    if resp.status_code == 429:
        return None, f"429 rate limited"
    return None, f"HTTP {resp.status_code}: {body_snippet}"


def _fetch_trades_blocking():
    """Actually paginates the API. Returns (trades, error_string).
    On partial failure returns whatever was collected plus error."""
    try:
        start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=IST)
    except ValueError:
        return [], f"invalid START_DATE: {START_DATE!r}, expected YYYY-MM-DD"
    start_ms = start_dt.timestamp() * 1000

    collected = []
    page = 1
    while page <= MAX_PAGES:
        result, err = sign_and_post({"page": page, "limit": PAGE_LIMIT})
        if err:
            return collected, err
        if not isinstance(result, list) or len(result) == 0:
            break

        for t in result:
            if t.get("timestamp", 0) >= start_ms:
                collected.append(t)
        oldest = min(t.get("timestamp", 0) for t in result)
        if oldest < start_ms:
            break
        page += 1
        time.sleep(0.1)
    else:
        log.warning(f"hit MAX_PAGES={MAX_PAGES} — there may be more trades")

    collected.sort(key=lambda t: t.get("timestamp", 0))
    return collected, None


def fetch_all_trades_since_start(force=False):
    """Returns the cached trade list, refreshing if stale. Never raises.
    Updates _state with last fetch status."""
    now = time.time()
    with _state_lock:
        cached = list(_state["trades"])
        last_ok = _state["last_fetch_ok"]
    if not force and cached and (now - last_ok) < CACHE_TTL:
        return cached

    # Try to acquire fetch lock; if someone else is fetching, return cached immediately
    if not _fetch_lock.acquire(blocking=False):
        log.info("fetch already in progress, returning cached")
        return cached

    try:
        with _state_lock:
            _state["fetch_in_progress"] = True
            _state["last_fetch_attempt"] = now
        t0 = time.time()
        trades, err = _fetch_trades_blocking()
        elapsed = time.time() - t0

        with _state_lock:
            _state["fetch_in_progress"] = False
            if trades and not err:
                # Fully successful
                _state["trades"] = trades
                _state["last_fetch_ok"] = time.time()
                _state["last_error"] = None
                log.info(f"fetched {len(trades)} trades in {elapsed:.1f}s")
                _save_disk_cache()
                return trades
            elif trades and err:
                # Partial — got some data before erroring out
                _state["trades"] = trades
                _state["last_fetch_ok"] = time.time()
                _state["last_error"] = f"partial fetch: {err}"
                log.warning(f"partial fetch: got {len(trades)} trades, then: {err}")
                _save_disk_cache()
                return trades
            else:
                # Total failure — keep old cache
                _state["last_error"] = err or "unknown error"
                log.error(f"fetch failed in {elapsed:.1f}s: {err}")
                return cached
    finally:
        _fetch_lock.release()


# ---------- pairing & stats (preserved from original) ----------
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


def fetch_bot_positions():
    if not BOT_URL:
        return {}
    try:
        resp = requests.get(f"{BOT_URL}/status", timeout=5)
        if resp.status_code == 200:
            return resp.json().get("active_trades", {})
    except Exception:
        pass
    return {}


def _empty_stats(open_count=0):
    return {"total": 0, "wins": 0, "losses": 0, "wr": 0, "net_inr": 0,
            "start": STARTING_CAPITAL, "end": STARTING_CAPITAL, "peak": STARTING_CAPITAL,
            "trough": STARTING_CAPITAL, "open_count": open_count, "open_positions": [],
            "open_pnl": 0, "equity": [STARTING_CAPITAL], "daily": [], "symbols": [],
            "total_fees_inr": 0, "return_pct": 0, "avg_win": 0, "avg_loss": 0,
            "long_count": 0, "short_count": 0, "long_wr": 0, "short_wr": 0,
            "long_pnl": 0, "short_pnl": 0, "sum_roi": 0, "sum_tp_roi": 0, "sum_sl_roi": 0}


def build_stats(completed, open_orders):
    if not completed:
        return _empty_stats(open_count=len(fetch_bot_positions()))

    wins = [t for t in completed if t["net_inr"] > 0]
    losses = [t for t in completed if t["net_inr"] <= 0]
    total_net = sum(t["net_inr"] for t in completed)

    equity = [STARTING_CAPITAL]
    running = STARTING_CAPITAL
    for t in completed:
        running += t["net_inr"]
        equity.append(round(running, 2))

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

    longs = [t for t in completed if t["side"] == "BUY"]
    shorts = [t for t in completed if t["side"] == "SELL"]
    long_wins = [t for t in longs if t["net_inr"] > 0]
    short_wins = [t for t in shorts if t["net_inr"] > 0]

    avg_win = sum(t["net_inr"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["net_inr"] for t in losses) / len(losses) if losses else 0
    long_pnl = sum(t["net_inr"] for t in longs)
    short_pnl = sum(t["net_inr"] for t in shorts)
    sum_tp_roi = sum(t["roi"] for t in completed if t["roi"] > 0)
    sum_sl_roi = sum(t["roi"] for t in completed if t["roi"] <= 0)
    sum_roi = sum(t["roi"] for t in completed)

    return {
        "total": len(completed),
        "wins": len(wins),
        "losses": len(losses),
        "wr": round(len(wins) / len(completed) * 100, 1),
        "long_count": len(longs),
        "short_count": len(shorts),
        "long_wr": round(len(long_wins) / len(longs) * 100, 1) if longs else 0,
        "short_wr": round(len(short_wins) / len(shorts) * 100, 1) if shorts else 0,
        "long_pnl": round(long_pnl, 2),
        "short_pnl": round(short_pnl, 2),
        "net_inr": round(total_net, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "sum_roi": round(sum_roi, 2),
        "sum_tp_roi": round(sum_tp_roi, 2),
        "sum_sl_roi": round(sum_sl_roi, 2),
        "start": STARTING_CAPITAL,
        "end": round(STARTING_CAPITAL + total_net, 2),
        "peak": max(equity),
        "trough": min(equity),
        "return_pct": round(total_net / STARTING_CAPITAL * 100, 2),
        "equity": equity,
        "daily": daily_list,
        "symbols": symbol_list,
        "total_fees_inr": round(sum(t["fees_usdt"] for t in completed) * USDT_INR, 2),
        "open_count": len(open_orders),
    }


# ---------- routes ----------
@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    try:
        trades = fetch_all_trades_since_start()
        completed, open_orders = pair_trades(trades)
        stats = build_stats(completed, open_orders)
        with _state_lock:
            err = _state["last_error"]
            last_ok = _state["last_fetch_ok"]
        return jsonify({
            "trades": completed,
            "stats": stats,
            "error": err,
            "last_fetch_ok": last_ok,
            "stale": bool(trades) and (time.time() - last_ok) > CACHE_TTL * 2,
        })
    except Exception as e:
        log.exception("api_data failed")
        return jsonify({
            "trades": [], "stats": _empty_stats(),
            "error": f"server error: {type(e).__name__}: {e}",
            "last_fetch_ok": 0, "stale": True,
        }), 200  # 200 so frontend can read body


@app.route("/api/refresh")
def api_refresh():
    # force a refresh on next /api/data call by zeroing the cache age
    with _state_lock:
        _state["last_fetch_ok"] = 0
    return jsonify({"status": "cache cleared"})


@app.route("/api/health")
def api_health():
    with _state_lock:
        return jsonify({
            "ok": True,
            "api_key_set": bool(API_KEY),
            "api_secret_set": bool(API_SECRET),
            "cached_trades": len(_state["trades"]),
            "last_fetch_ok": _state["last_fetch_ok"],
            "last_fetch_ok_age_s": int(time.time() - _state["last_fetch_ok"]) if _state["last_fetch_ok"] else None,
            "last_error": _state["last_error"],
            "fetch_in_progress": _state["fetch_in_progress"],
            "endpoint": ENDPOINT,
            "page_limit": PAGE_LIMIT,
            "cache_ttl_s": CACHE_TTL,
            "start_date": START_DATE,
        })


@app.route("/api/diagnose")
def api_diagnose():
    """One-shot probe to surface exactly what's wrong with the API call."""
    if not API_KEY or not API_SECRET:
        return jsonify({"ok": False, "stage": "config",
                        "error": "API keys not set as environment variables"}), 200
    t0 = time.time()
    result, err = sign_and_post({"page": 1, "limit": 1})
    elapsed = round(time.time() - t0, 2)
    if err:
        return jsonify({"ok": False, "stage": "api_call", "elapsed_s": elapsed, "error": err}), 200
    return jsonify({
        "ok": True,
        "elapsed_s": elapsed,
        "got_trades": isinstance(result, list),
        "sample_count": len(result) if isinstance(result, list) else 0,
        "sample_keys": list(result[0].keys()) if isinstance(result, list) and result else [],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
