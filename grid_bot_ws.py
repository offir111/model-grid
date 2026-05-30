"""
MODEL GRID — Binance WebSocket Bot
====================================
Real-time version using Binance WebSocket stream.
Processes every closed 1-minute candle instantly.

Architecture:
  - Binance WebSocket pushes each closed kline → immediate fill check
  - Every 5 minutes → push data to GitHub (dashboard update)
  - Every 4 hours   → recalculate AI params (range + grids)
  - Auto-reconnect  on disconnect

Deploy: Railway (Procfile: worker: python grid_bot_ws.py)
"""

import json, os, time, subprocess, threading
from datetime import datetime
import requests
import websocket

# ── Import core logic from existing simulator ──────────────────────
from grid_simulator import (
    calc_ai_params, build_grid, simulate_fills,
    check_and_apply_dgt,
    load_state, save_state,
    load_portfolio, init_portfolio, save_portfolio,
    get_klines,
    DATA_DIR, DGT_ENABLED, AI_HOURS
)

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
WS_URL          = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"
GITHUB_PUSH_SEC = 300      # push to GitHub every 5 minutes
AI_RECALC_SEC   = 14400    # recalculate AI params every 4 hours
RECONNECT_SEC   = 5        # wait before reconnecting after disconnect

# ─────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────
bot_state     = None
portfolio     = None
hourly_cache  = []
last_push     = 0
last_ai_calc  = 0
candles_since_push = 0

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ─────────────────────────────────────────────────────────────────
# GITHUB PUSH
# ─────────────────────────────────────────────────────────────────
def push_to_github():
    global last_push
    try:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(__file__)),
                        "add", "data/"], check=True, capture_output=True)
        result = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.abspath(__file__)),
             "commit", "-m", f"bot: update {now_str}"],
            capture_output=True, text=True
        )
        if "nothing to commit" not in result.stdout:
            subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(__file__)),
                            "push"], check=True, capture_output=True)
            log("✅ GitHub push OK")
        else:
            log("  GitHub: nothing new to push")
        last_push = time.time()
    except Exception as e:
        log(f"⚠️  GitHub push failed: {e}")

# ─────────────────────────────────────────────────────────────────
# AI PARAMS REFRESH
# ─────────────────────────────────────────────────────────────────
def refresh_ai_params():
    global bot_state, portfolio, hourly_cache, last_ai_calc
    log("🔄 Refreshing AI params (hourly candles)...")
    candles = get_klines("1h", AI_HOURS)
    if candles:
        hourly_cache = candles
        log(f"  Fetched {len(candles)} hourly candles")
    last_ai_calc = time.time()

# ─────────────────────────────────────────────────────────────────
# PROCESS CLOSED CANDLE
# ─────────────────────────────────────────────────────────────────
def process_candle(candle):
    global bot_state, portfolio, last_push, last_ai_calc, candles_since_push

    if bot_state is None or portfolio is None:
        return

    # Recalc AI params if due
    if time.time() - last_ai_calc > AI_RECALC_SEC:
        threading.Thread(target=refresh_ai_params, daemon=True).start()

    # DGT check
    if DGT_ENABLED and hourly_cache:
        current_price = candle["close"]
        dgt_fired = check_and_apply_dgt(bot_state, current_price, portfolio, hourly_cache)
        if dgt_fired:
            log(f"🔁 DGT Reset — new range: ${bot_state['lower']:,.2f} – ${bot_state['upper']:,.2f}")

    # Fill simulation on this single closed candle
    profit, cycles = simulate_fills(bot_state, [candle], portfolio)

    if cycles > 0:
        log(f"💰 {cycles} SELL(s) — profit: +${profit:.4f} | "
            f"total P&L: ${portfolio['realized_pnl']:+.4f} | "
            f"APR: {portfolio.get('apr', 0):.1f}%")
    else:
        filled = sum(1 for l in bot_state["grid"] if l["filled"])
        log(f"  Candle ${candle['low']:,.0f}–${candle['high']:,.0f} | "
            f"filled: {filled}/{bot_state['grids']} | "
            f"P&L: ${portfolio['realized_pnl']:+.4f}")

    bot_state["last_run_time"] = datetime.now().isoformat()
    save_state(bot_state)
    save_portfolio(portfolio)
    candles_since_push += 1

    # Push to GitHub every GITHUB_PUSH_SEC seconds
    if time.time() - last_push > GITHUB_PUSH_SEC:
        threading.Thread(target=push_to_github, daemon=True).start()

# ─────────────────────────────────────────────────────────────────
# WEBSOCKET HANDLERS
# ─────────────────────────────────────────────────────────────────
def on_message(ws, message):
    try:
        data   = json.loads(message)
        kline  = data.get("k", {})
        closed = kline.get("x", False)   # True = candle just closed

        if not closed:
            return  # skip in-progress candles

        candle = {
            "time":  kline["t"],
            "open":  float(kline["o"]),
            "high":  float(kline["h"]),
            "low":   float(kline["l"]),
            "close": float(kline["c"]),
        }
        process_candle(candle)

    except Exception as e:
        log(f"⚠️  on_message error: {e}")

def on_error(ws, error):
    log(f"⚠️  WebSocket error: {error}")

def on_close(ws, code, msg):
    log(f"🔌 WebSocket closed (code={code}). Reconnecting in {RECONNECT_SEC}s...")

def on_open(ws):
    log("✅ Binance WebSocket connected — listening to BTC/USDT 1m klines")

# ─────────────────────────────────────────────────────────────────
# INIT
# ─────────────────────────────────────────────────────────────────
def init_bot():
    global bot_state, portfolio, hourly_cache, last_push, last_ai_calc

    log("=" * 55)
    log("  MODEL GRID — Binance WebSocket Mode")
    log("  BTC/USDT | 1m klines | Railway 24/7")
    log("=" * 55)

    portfolio = load_portfolio()
    if portfolio is None:
        portfolio = init_portfolio()

    log(f"  Portfolio: ${portfolio['investment']:,.2f} | "
        f"P&L: ${portfolio['realized_pnl']:+.4f} | "
        f"Cycles: {portfolio['total_cycles']}")

    # Load AI params
    log("  Fetching hourly candles for AI params...")
    hourly_cache = get_klines("1h", AI_HOURS)
    if not hourly_cache:
        log("  ⚠️  Could not fetch hourly candles!")
    else:
        log(f"  Fetched {len(hourly_cache)} hourly candles ✅")

    last_ai_calc = time.time()

    bot_state = load_state()

    if bot_state is None:
        log("  First run — calculating AI grid parameters...")
        ai = calc_ai_params(hourly_cache)
        if ai is None:
            log("  ❌ Not enough data. Aborting.")
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
        log(f"  AI Range: ${ai['lower']:,.2f} – ${ai['upper']:,.2f} | "
            f"Grids: {ai['grids']} | ATR: ${ai['atr14']:,.2f}")
        save_state(bot_state)
    else:
        log(f"  Range: ${bot_state['lower']:,.2f} – ${bot_state['upper']:,.2f} | "
            f"Grids: {bot_state['grids']}")
        filled = sum(1 for l in bot_state["grid"] if l["filled"])
        log(f"  Grid: {filled} filled / {bot_state['grids'] - filled} waiting")

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
            log(f"🔗 Connecting to Binance WebSocket...")
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open    = on_open,
                on_message = on_message,
                on_error   = on_error,
                on_close   = on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            log(f"❌ Fatal error: {e}")

        log(f"  Reconnecting in {RECONNECT_SEC} seconds...")
        time.sleep(RECONNECT_SEC)

if __name__ == "__main__":
    main()
