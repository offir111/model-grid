"""
MODEL GRID — Pionex AI Grid Bot 2.0 Simulator
==============================================
Strategy : Grid Trading with DGT auto-reset + ATR-14 spacing
           BTC/USDT — virtual fills simulated from real Binance data
Data     : Binance API (free, no key needed)
Run      : Every 15 minutes via GitHub Actions
           Uses recent 1-minute klines to simulate fills between runs
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
import json
import os
import math
from datetime import datetime

BINANCE_BASE = "https://api.binance.com/api/v3"

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
INVESTMENT       = 1000.0   # $ starting investment
FEE_PCT          = 0.05     # % per fill (Pionex standard fee)
DGT_ENABLED      = True     # auto-reset when price exits range by >0.5%
REINVEST_ENABLED = True     # compound realized profits on DGT reset
AI_HOURS         = 720      # klines for AI param calculation (30d × 24h)

DATA_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATE_FILE     = os.path.join(DATA_DIR, "grid_state.json")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")

# ─────────────────────────────────────────────────────────────────
# BINANCE API
# ─────────────────────────────────────────────────────────────────

def get_klines(interval="1h", limit=720):
    try:
        r = requests.get(f"{BINANCE_BASE}/klines", params={
            "symbol": "BTCUSDT", "interval": interval, "limit": limit
        }, timeout=15)
        data = r.json()
        if not isinstance(data, list):
            return []
        candles = []
        for c in data:
            candles.append({
                "time":  c[0],
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
            })
        return candles
    except Exception as e:
        print(f"  ⚠️ Error fetching klines [{interval}]: {e}")
        return []


def get_current_price():
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=5)
        return float(r.json()["price"])
    except:
        return None

# ─────────────────────────────────────────────────────────────────
# AI PARAM CALCULATION (Pionex AI Grid Bot 2.0 method)
# ─────────────────────────────────────────────────────────────────

def calc_ai_params(candles):
    """
    Pionex AI method:
    - Range: 3rd–97th percentile of closes + 4% padding
    - ATR-14: 14-period average true range
    - Grids: range / (0.5 × ATR), clamped 10–80
    """
    if len(candles) < 30:
        return None

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]

    # Percentile range
    sorted_cl = sorted(closes)
    n = len(sorted_cl)
    lo_idx = max(0, int(0.03 * n))
    hi_idx = min(n - 1, int(0.97 * n))
    low_pct  = sorted_cl[lo_idx]
    high_pct = sorted_cl[hi_idx]
    padding  = (high_pct - low_pct) * 0.04
    lower    = round(low_pct  - padding, 2)
    upper    = round(high_pct + padding, 2)

    # ATR-14
    atr_vals = []
    for i in range(1, min(15, len(candles))):
        tr = max(
            highs[-i] - lows[-i],
            abs(highs[-i] - closes[-i-1]),
            abs(lows[-i]  - closes[-i-1]),
        )
        atr_vals.append(tr)
    atr14 = sum(atr_vals) / len(atr_vals) if atr_vals else (upper - lower) / 20

    # Grid count
    rng   = upper - lower
    step  = 0.5 * atr14
    grids = int(rng / step) if step > 0 else 20
    grids = max(10, min(80, grids))

    return {"lower": lower, "upper": upper, "grids": grids, "atr14": round(atr14, 2)}

# ─────────────────────────────────────────────────────────────────
# GRID BUILDER
# ─────────────────────────────────────────────────────────────────

def build_grid(lower, upper, grids, investment):
    """
    Build grid levels. Each level has a BUY price and a SELL price
    (one step higher). When price crosses DOWN through buy → fill.
    When price crosses UP through sell (filled) → profit realized.
    """
    if grids < 2:
        grids = 2
    step = (upper - lower) / grids
    per_grid = investment / grids

    levels = []
    for i in range(grids):
        buy_px  = round(lower + i * step, 2)
        sell_px = round(buy_px + step, 2)
        # Gross profit per cycle (before fees)
        gross_pct   = step / buy_px if buy_px > 0 else 0
        fee_cost    = per_grid * (FEE_PCT / 100) * 2   # buy fee + sell fee
        grid_profit = round(per_grid * gross_pct - fee_cost, 4)
        levels.append({
            "buy":    buy_px,
            "sell":   sell_px,
            "profit": grid_profit,
            "filled": False,
        })
    return levels

# ─────────────────────────────────────────────────────────────────
# STATE & PORTFOLIO I/O
# ─────────────────────────────────────────────────────────────────

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except:
        return None


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_portfolio():
    if not os.path.exists(PORTFOLIO_FILE):
        return None
    try:
        with open(PORTFOLIO_FILE, encoding="utf-8") as f:
            return json.load(f)
    except:
        return None


def init_portfolio():
    os.makedirs(DATA_DIR, exist_ok=True)
    p = {
        "starting_investment": INVESTMENT,
        "investment":          INVESTMENT,
        "realized_pnl":        0.0,
        "reinvested_total":    0.0,
        "total_cycles":        0,
        "dgt_count":           0,
        "max_drawdown":        0.0,
        "peak_value":          INVESTMENT,
        "apr":                 0.0,
        "start_time":          datetime.now().isoformat(),
        "last_updated":        datetime.now().isoformat(),
        "trade_log":           [],
    }
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)
    print(f"  ✅ Grid Portfolio created — Investment: ${INVESTMENT:,.2f}")
    return p


def save_portfolio(p):
    p["last_updated"] = datetime.now().isoformat()
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)

# ─────────────────────────────────────────────────────────────────
# DGT CHECK
# ─────────────────────────────────────────────────────────────────

def check_and_apply_dgt(state, current_price, portfolio, hourly_candles):
    """
    DGT: if price exits range by >0.5% → recalculate AI params,
    reinvest uncompounded profits, rebuild grid.
    Returns True if DGT was triggered.
    """
    upper = state["upper"]
    lower = state["lower"]

    if not (current_price > upper * 1.005 or current_price < lower * 0.995):
        return False

    direction = "above" if current_price > upper else "below"
    print(f"  🔄 DGT triggered! BTC=${current_price:,.2f} is {direction} range "
          f"[${lower:,.2f}–${upper:,.2f}]")

    # Reinvest uncompounded profits
    reinvested_total = portfolio.get("reinvested_total", 0.0)
    uncompounded     = portfolio["realized_pnl"] - reinvested_total
    new_investment   = portfolio["investment"]

    if REINVEST_ENABLED and uncompounded > 0:
        new_investment = round(portfolio["investment"] + uncompounded, 2)
        portfolio["reinvested_total"] = round(reinvested_total + uncompounded, 2)
        print(f"  💰 Reinvest: +${uncompounded:.4f}  →  New investment: ${new_investment:.2f}")

    portfolio["investment"] = new_investment
    portfolio["dgt_count"]  = portfolio.get("dgt_count", 0) + 1

    # Recalculate AI params
    ai = calc_ai_params(hourly_candles)
    if ai is None:
        print("  ⚠️ Not enough data for AI recalc — using current range shifted")
        rng = state["upper"] - state["lower"]
        mid = current_price
        ai  = {
            "lower": round(mid - rng / 2, 2),
            "upper": round(mid + rng / 2, 2),
            "grids": state["grids"],
            "atr14": state.get("atr14", rng / state["grids"]),
        }

    grid = build_grid(ai["lower"], ai["upper"], ai["grids"], new_investment)

    state.update({
        "lower":    ai["lower"],
        "upper":    ai["upper"],
        "grids":    ai["grids"],
        "atr14":    ai["atr14"],
        "investment": new_investment,
        "grid":     grid,
        "last_dgt": datetime.now().isoformat(),
    })

    print(f"  📐 New range: ${ai['lower']:,.2f} — ${ai['upper']:,.2f}  "
          f"Grids: {ai['grids']}  ATR: ${ai['atr14']:,.2f}")
    return True

# ─────────────────────────────────────────────────────────────────
# GRID SIMULATION (tick through recent candles)
# ─────────────────────────────────────────────────────────────────

def simulate_fills(state, recent_candles, portfolio):
    """
    For each 1-minute candle since last run:
    - Candle LOW crosses below a buy level → virtual buy (mark filled)
    - Candle HIGH crosses above a sell level of a filled level → virtual sell
    Returns (realized_profit, cycle_count, new_log_entries)
    """
    grid          = state["grid"]
    realized_pnl  = 0.0
    cycles        = 0
    log_entries   = []

    last_processed = state.get("last_processed_candle", 0)

    for candle in recent_candles:
        t = candle["time"]
        if t <= last_processed:
            continue  # already processed this candle

        h, l = candle["high"], candle["low"]

        for lvl in grid:
            buy_px  = lvl["buy"]
            sell_px = lvl["sell"]

            # Price crossed DOWN through buy price → virtual buy
            if not lvl["filled"] and l <= buy_px <= h:
                lvl["filled"] = True
                log_entries.append({
                    "type":   "BUY",
                    "price":  buy_px,
                    "profit": 0,
                    "time":   datetime.fromtimestamp(t / 1000).isoformat(),
                })

            # Price crossed UP through sell price of a filled level → virtual sell
            elif lvl["filled"] and l <= sell_px <= h:
                lvl["filled"] = False
                profit = lvl["profit"]
                realized_pnl += profit
                cycles += 1
                log_entries.append({
                    "type":   "SELL",
                    "price":  sell_px,
                    "profit": round(profit, 4),
                    "time":   datetime.fromtimestamp(t / 1000).isoformat(),
                })

        state["last_processed_candle"] = t

    # Update portfolio
    portfolio["realized_pnl"] = round(portfolio["realized_pnl"] + realized_pnl, 4)
    portfolio["total_cycles"] += cycles

    # APR
    start_dt    = datetime.fromisoformat(portfolio["start_time"])
    days_run    = max(0.001, (datetime.now() - start_dt).total_seconds() / 86400)
    investment  = portfolio["investment"]
    portfolio["apr"] = round(
        (portfolio["realized_pnl"] / investment) / days_run * 365 * 100, 2
    ) if investment > 0 else 0.0

    # Max Drawdown
    cur_val = investment + portfolio["realized_pnl"]
    if cur_val > portfolio.get("peak_value", investment):
        portfolio["peak_value"] = cur_val
    peak     = portfolio.get("peak_value", investment)
    drawdown = (peak - cur_val) / peak * 100 if peak > 0 else 0
    if drawdown > portfolio.get("max_drawdown", 0):
        portfolio["max_drawdown"] = round(drawdown, 2)

    # Trade log (keep last 200)
    all_log = portfolio.get("trade_log", []) + log_entries
    portfolio["trade_log"] = all_log[-200:]

    state["grid"] = grid
    return realized_pnl, cycles

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  MODEL GRID — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Pionex AI Grid Bot 2.0 | BTC/USDT | DGT + ATR-14")
    print(f"{'='*60}\n")

    # Portfolio
    portfolio = load_portfolio()
    if portfolio is None:
        portfolio = init_portfolio()

    inv         = portfolio["investment"]
    pnl         = portfolio["realized_pnl"]
    total_val   = round(inv + pnl, 2)
    start_dt    = datetime.fromisoformat(portfolio["start_time"])
    days_run    = max(0.001, (datetime.now() - start_dt).total_seconds() / 86400)

    print(f"  💼 Investment: ${inv:,.2f}  |  P&L: ${pnl:+.4f}  |  Total: ${total_val:,.2f}")
    print(f"  📊 APR: {portfolio.get('apr', 0):.1f}%  |  "
          f"Cycles: {portfolio['total_cycles']}  |  "
          f"Running: {days_run:.1f}d  |  "
          f"DGT resets: {portfolio.get('dgt_count', 0)}")
    print(f"  📉 MDD: {portfolio.get('max_drawdown', 0):.2f}%\n")

    # Current price
    current_price = get_current_price()
    if current_price is None:
        print("  ⚠️ Cannot fetch BTC price — aborting")
        return
    print(f"  💰 BTC Price: ${current_price:,.2f}\n")

    # Hourly klines (for AI params + DGT)
    print("  📡 Fetching 30d hourly klines for AI params...")
    hourly = get_klines("1h", AI_HOURS)
    if not hourly:
        print("  ⚠️ No kline data — aborting")
        return

    # Load or init grid state
    state = load_state()

    if state is None:
        # ── First run: calculate AI params and build grid ──────────
        print("  🤖 First run — calculating AI parameters...")
        ai = calc_ai_params(hourly)
        if ai is None:
            print("  ⚠️ Not enough data for AI params — aborting")
            return

        grid = build_grid(ai["lower"], ai["upper"], ai["grids"], portfolio["investment"])
        state = {
            "lower":                 ai["lower"],
            "upper":                 ai["upper"],
            "grids":                 ai["grids"],
            "atr14":                 ai["atr14"],
            "investment":            portfolio["investment"],
            "grid":                  grid,
            "prev_price":            current_price,
            "last_processed_candle": 0,
            "last_dgt":              None,
            "created":               datetime.now().isoformat(),
        }

        print(f"  📐 AI Range: ${ai['lower']:,.2f} — ${ai['upper']:,.2f}")
        print(f"  📏 ATR-14: ${ai['atr14']:,.2f}  |  Grids: {ai['grids']}")
        print(f"  💎 Profit/grid: ~${grid[0]['profit']:.4f}\n")

    else:
        # ── Subsequent runs ────────────────────────────────────────
        print(f"  📐 Current range: ${state['lower']:,.2f} — ${state['upper']:,.2f}  "
              f"Grids: {state['grids']}")

        # Check DGT
        if DGT_ENABLED:
            check_and_apply_dgt(state, current_price, portfolio, hourly)

    # ── Simulate fills from recent 1-minute candles ────────────────
    print("\n  📡 Fetching recent 1m candles for fill simulation...")
    recent = get_klines("1m", 20)   # last ~20 minutes (one extra for safety)

    if recent:
        profit, cycles = simulate_fills(state, recent, portfolio)
        if cycles > 0:
            print(f"  ✅ {cycles} cycle(s) filled  |  Profit this run: ${profit:+.4f}")
        else:
            print("  ⏳ No fills this interval (price range within grid, no full grid cross)")
    else:
        print("  ⚠️ No recent candles for simulation")

    # Grid position summary
    filled_count = sum(1 for lvl in state["grid"] if lvl["filled"])
    unfilled     = state["grids"] - filled_count
    print(f"\n  📊 Grid: {filled_count}/{state['grids']} levels filled "
          f"(active positions: {filled_count}  |  waiting to buy: {unfilled})")

    in_pct = (current_price - state["lower"]) / (state["upper"] - state["lower"]) * 100
    in_pct = max(0, min(100, in_pct))
    bar_w  = 30
    bar_p  = int(in_pct / 100 * bar_w)
    bar    = "█" * bar_p + "░" * (bar_w - bar_p)
    print(f"  Price position: [{bar}] {in_pct:.0f}% in range")
    print(f"  Range: ${state['lower']:,.2f} ←→ ${state['upper']:,.2f}")

    # Save everything
    state["prev_price"] = current_price
    save_state(state)
    save_portfolio(portfolio)

    # Final P&L
    total_val = round(portfolio["investment"] + portfolio["realized_pnl"], 2)
    print(f"\n  💼 Total: ${total_val:,.2f}  |  "
          f"APR: {portfolio.get('apr', 0):.1f}%  |  "
          f"Cycles: {portfolio['total_cycles']}")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
