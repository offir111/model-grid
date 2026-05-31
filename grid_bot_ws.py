"""
MODEL GRID — KuCoin WebSocket Bot
====================================
Real-time version using KuCoin WebSocket stream.
Processes every closed 1-minute candle instantly.

Architecture:
  - KuCoin WebSocket pushes each closed kline → immediate fill check
  - Every 5 minutes → push data to GitHub (dashboard update)
  - Every 4 hours   → recalculate AI params (range + grids)
  - Auto-reconnect  on disconnect

Deploy: Railway (Procfile: worker: python grid_bot_ws.py)
"""

import json, os, time, subprocess, threading, hmac, hashlib, base64
from datetime import datetime
import requests
import websocket

# ── Import core logic from existing simulator ──────────────────────
from grid_simulator import (
    calc_ai_params, build_grid, simulate_fills,
    check_and_apply_dgt, check_support_distance,
    load_state, save_state,
    load_portfolio, init_portfolio, save_portfolio,
    DATA_DIR, DGT_ENABLED, AI_HOURS
)

# ─────────────────────────────────────────────────────────────────
# KUCOIN CREDENTIALS
# ─────────────────────────────────────────────────────────────────
KC_KEY        = os.environ.get("KUCOIN_API_KEY",        "6a14b60795d1820001a05521")
KC_SECRET     = os.environ.get("KUCOIN_API_SECRET",     "45c2df6d-076a-4b87-9666-926448d02fce")
KC_PASSPHRASE = os.environ.get("KUCOIN_PASSPHRASE",     "modelw1800")
KC_BASE       = "https://api.kucoin.com"

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
SYMBOL          = "BTC-USDT"
INTERVAL        = "1min"
GITHUB_PUSH_SEC = 300      # push to GitHub every 5 minutes
AI_RECALC_SEC   = 14400    # recalculate AI params every 4 hours
RECONNECT_SEC   = 5        # wait before reconnecting after disconnect

# ─────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────
bot_state    = None
portfolio    = None
hourly_cache = []
last_push    = 0
last_ai_calc = 0

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ─────────────────────────────────────────────────────────────────
# KUCOIN REST — fetch candles + WebSocket token
# ─────────────────────────────────────────────────────────────────
def kc_sign(endpoint, method="GET", body=""):
    timestamp = str(int(time.time() * 1000))
    msg = f"{timestamp}{method}{endpoint}{body}"
    sig = base64.b64encode(
        hmac.new(KC_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    pp = base64.b64encode(
        hmac.new(KC_SECRET.encode(), KC_PASSPHRASE.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "KC-API-KEY":         KC_KEY,
        "KC-API-SIGN":        sig,
        "KC-API-TIMESTAMP":   timestamp,
        "KC-API-PASSPHRASE":  pp,
        "KC-API-KEY-VERSION": "2",
        "Content-Type":       "application/json",
    }

def get_kucoin_price():
    try:
        r = requests.get(f"{KC_BASE}/api/v1/market/orderbook/level1",
                         params={"symbol": SYMBOL}, timeout=5)
        return float(r.json()["data"]["price"])
    except:
        return None

def get_kucoin_klines(interval="1hour", limit=720):
    """Fetch historical klines from KuCoin for AI param calculation."""
    try:
        # KuCoin uses Unix timestamps
        end_ts   = int(time.time())
        # 1hour = 3600s, fetch limit candles back
        interval_sec = {"1min":60,"5min":300,"15min":900,"1hour":3600,"4hour":14400}
        sec = interval_sec.get(interval, 3600)
        start_ts = end_ts - sec * limit

        r = requests.get(f"{KC_BASE}/api/v1/market/candles", params={
            "symbol":      SYMBOL,
            "type":        interval,
            "startAt":     start_ts,
            "endAt":       end_ts,
        }, timeout=15)
        data = r.json().get("data", [])
        if not data:
            return []
        # KuCoin returns: [time, open, close, high, low, volume, turnover]
        candles = [{
            "time":  int(c[0]) * 1000,  # convert to ms
            "open":  float(c[1]),
            "high":  float(c[3]),
            "low":   float(c[4]),
            "close": float(c[2]),
        } for c in reversed(data)]  # KuCoin returns newest first
        return candles
    except Exception as e:
        log(f"⚠️  KuCoin klines error: {e}")
        return []

def get_ws_token():
    """Get KuCoin WebSocket token (public endpoint)."""
    try:
        r = requests.post(f"{KC_BASE}/api/v1/bullet-public", timeout=10)
        data = r.json()["data"]
        token    = data["token"]
        endpoint = data["instanceServers"][0]["endpoint"]
        ping_int = data["instanceServers"][0]["pingInterval"]
        return token, endpoint, ping_int
    except Exception as e:
        log(f"⚠️  Could not get WS token: {e}")
        return None, None, None

# ─────────────────────────────────────────────────────────────────
# GITHUB PUSH
# ─────────────────────────────────────────────────────────────────
def push_to_github():
    global last_push
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        subprocess.run(["git", "-C", base, "add", "data/"],
                       check=True, capture_output=True)
        result = subprocess.run(
            ["git", "-C", base, "commit", "-m", f"bot: update {now_str}"],
            capture_output=True, text=True
        )
        if "nothing to commit" not in result.stdout:
            subprocess.run(["git", "-C", base, "push"],
                           check=True, capture_output=True)
            log("✅ GitHub push OK")
        last_push = time.time()
    except Exception as e:
        log(f"⚠️  GitHub push failed: {e}")

# ─────────────────────────────────────────────────────────────────
# AI PARAMS REFRESH
# ─────────────────────────────────────────────────────────────────
def refresh_ai_params():
    global hourly_cache, last_ai_calc
    log("🔄 Refreshing AI params...")
    candles = get_kucoin_klines("1hour", AI_HOURS)
    if candles:
        hourly_cache = candles
        log(f"  Fetched {len(candles)} hourly candles ✅")
    last_ai_calc = time.time()

# ─────────────────────────────────────────────────────────────────
# PROCESS CLOSED CANDLE
# ─────────────────────────────────────────────────────────────────
def process_candle(candle):
    global bot_state, portfolio, last_push, last_ai_calc

    if bot_state is None or portfolio is None:
        return

    # Recalc AI params if due
    if time.time() - last_ai_calc > AI_RECALC_SEC:
        threading.Thread(target=refresh_ai_params, daemon=True).start()

    # DGT check
    if DGT_ENABLED and hourly_cache:
        dgt = check_and_apply_dgt(bot_state, candle["close"], portfolio, hourly_cache)
        if dgt:
            log(f"🔁 DGT Reset → ${bot_state['lower']:,.0f}–${bot_state['upper']:,.0f}")

    # Fill simulation
    profit, cycles = simulate_fills(bot_state, [candle], portfolio)

    if cycles > 0:
        log(f"💰 {cycles} SELL | profit: +${profit:.4f} | "
            f"P&L: ${portfolio['realized_pnl']:+.4f} | "
            f"APR: {portfolio.get('apr',0):.1f}%")
    else:
        filled = sum(1 for l in bot_state["grid"] if l["filled"])
        _, sup_dist = check_support_distance(candle["close"], hourly_cache)
        safety = "✅" if sup_dist >= 3.0 else "⚠️"
        log(f"  ${candle['low']:,.0f}–${candle['high']:,.0f} | "
            f"filled: {filled}/{bot_state['grids']} | "
            f"P&L: ${portfolio['realized_pnl']:+.4f} | "
            f"Support: {safety}{sup_dist:.1f}%")

    bot_state["last_run_time"] = datetime.now().isoformat()
    save_state(bot_state)
    save_portfolio(portfolio)

    if time.time() - last_push > GITHUB_PUSH_SEC:
        threading.Thread(target=push_to_github, daemon=True).start()

# ─────────────────────────────────────────────────────────────────
# WEBSOCKET HANDLERS
# ─────────────────────────────────────────────────────────────────
ping_timer = None

def on_open(ws):
    log("✅ KuCoin WebSocket connected")
    # Subscribe to 1min kline stream
    sub_msg = {
        "id":             str(int(time.time() * 1000)),
        "type":           "subscribe",
        "topic":          f"/market/candles:{SYMBOL}_{INTERVAL}",
        "privateChannel": False,
        "response":       True,
    }
    ws.send(json.dumps(sub_msg))
    log(f"  Subscribed to {SYMBOL} {INTERVAL} klines")

def on_message(ws, message):
    try:
        data = json.loads(message)

        # Handle ping from server
        if data.get("type") == "ping":
            ws.send(json.dumps({"id": data.get("id","1"), "type":"pong"}))
            return

        if data.get("type") != "message":
            return

        kline_data = data.get("data", {})
        candles    = kline_data.get("candles", [])
        if not candles:
            return

        # KuCoin kline: [time, open, close, high, low, volume, turnover]
        # Only process when candle is closed (new candle starts)
        # KuCoin sends on every tick — we use time change to detect new candle
        candle_time = int(candles[0]) * 1000

        # Build candle dict
        candle = {
            "time":  candle_time,
            "open":  float(candles[1]),
            "high":  float(candles[3]),
            "low":   float(candles[4]),
            "close": float(candles[2]),
        }

        last_proc = bot_state.get("last_processed_candle", 0) if bot_state else 0

        # Only process each candle once (when a new candle time arrives)
        if candle_time > last_proc:
            process_candle(candle)

    except Exception as e:
        log(f"⚠️  on_message error: {e}")

def on_error(ws, error):
    log(f"⚠️  WebSocket error: {error}")

def on_close(ws, code, msg):
    log(f"🔌 WebSocket closed (code={code}). Reconnecting in {RECONNECT_SEC}s...")

# ─────────────────────────────────────────────────────────────────
# INIT
# ─────────────────────────────────────────────────────────────────
def init_bot():
    global bot_state, portfolio, hourly_cache, last_push, last_ai_calc

    log("=" * 55)
    log("  MODEL GRID — KuCoin WebSocket Mode")
    log(f"  {SYMBOL} | {INTERVAL} klines | Railway 24/7")
    log("=" * 55)

    portfolio = load_portfolio()
    if portfolio is None:
        portfolio = init_portfolio()

    log(f"  Portfolio: ${portfolio['investment']:,.2f} | "
        f"P&L: ${portfolio['realized_pnl']:+.4f} | "
        f"Cycles: {portfolio['total_cycles']}")

    log("  Fetching hourly candles (KuCoin)...")
    hourly_cache = get_kucoin_klines("1hour", AI_HOURS)
    if not hourly_cache:
        log("  ⚠️  No hourly candles — trying Binance fallback...")
        from grid_simulator import get_klines
        hourly_cache = get_klines("1h", AI_HOURS)

    log(f"  Fetched {len(hourly_cache)} candles ✅")
    last_ai_calc = time.time()

    bot_state = load_state()
    if bot_state is None:
        log("  First run — calculating AI grid params...")
        ai = calc_ai_params(hourly_cache)
        if ai is None:
            log("  ❌ Not enough data.")
            return False
        grid = build_grid(ai["lower"], ai["upper"], ai["grids"], portfolio["investment"])
        bot_state = {
            "lower":                 ai["lower"],
            "upper":                 ai["upper"],
            "grids":                 ai["grids"],
            "atr14":                 ai["atr14"],
            "investment":            portfolio["investment"],
            "grid":                  grid,
            "last_processed_candle": 0,
            "last_run_time":         datetime.now().isoformat(),
            "last_dgt":              None,
            "created":               datetime.now().isoformat(),
        }
        log(f"  Range: ${ai['lower']:,.0f}–${ai['upper']:,.0f} | "
            f"Grids: {ai['grids']} | ATR: ${ai['atr14']:,.0f}")
        save_state(bot_state)
    else:
        filled = sum(1 for l in bot_state["grid"] if l["filled"])
        log(f"  Range: ${bot_state['lower']:,.0f}–${bot_state['upper']:,.0f} | "
            f"Grids: {bot_state['grids']} | Filled: {filled}")

    last_push = time.time()
    return True

# ─────────────────────────────────────────────────────────────────
# MAIN LOOP — auto-reconnect
# ─────────────────────────────────────────────────────────────────
def main():
    if not init_bot():
        return

    while True:
        try:
            token, endpoint, ping_ms = get_ws_token()
            if not token:
                log("  Could not get WS token — retrying in 10s...")
                time.sleep(10)
                continue

            ws_url = f"{endpoint}?token={token}&connectId={int(time.time()*1000)}"
            log(f"🔗 Connecting to KuCoin WebSocket...")

            ws = websocket.WebSocketApp(
                ws_url,
                on_open    = on_open,
                on_message = on_message,
                on_error   = on_error,
                on_close   = on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)

        except Exception as e:
            log(f"❌ Fatal: {e}")

        log(f"  Reconnecting in {RECONNECT_SEC}s...")
        time.sleep(RECONNECT_SEC)

if __name__ == "__main__":
    main()
