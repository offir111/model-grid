"""
MODEL GRID — Pionex AI Grid Bot 2.0 Simulator
==============================================
v2.0 — 4 fixes for Pionex accuracy:
  1. Dynamic candle window  — fetches ALL 1m candles since last run (not just 20)
  2. Direction-aware fills  — SELL processed before BUY per candle
  3. No same-candle buy+sell — just_bought flag blocks same-candle exit
  4. Real BTC qty tracking  — profit = qty * (sell - buy) - fees (not flat %)
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
import json
import os
from datetime import datetime

BINANCE_BASE = "https://api.binance.com/api/v3"

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
INVESTMENT       = 1000.0   # $ starting investment
FEE_PCT          = 0.05     # % per side — Pionex taker fee
DGT_ENABLED      = True     # auto-reset when price exits range by >0.5%
REINVEST_ENABLED = True     # compound realized profits on DGT reset
AI_HOURS         = 720      # 30d × 24h hourly klines for AI param calculation

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
        return [{
            "time":  c[0],
            "open":  float(c[1]),
            "high":  float(c[2]),
            "low":   float(c[3]),
            "close": float(c[4]),
        } for c in data]
    except Exception as e:
        print(f"  Warning: Error fetching klines [{interval}]: {e}")
        return []


def get_current_price():
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=5)
        return float(r.json()["price"])
    except:
        return None

# ─────────────────────────────────────────────────────────────────
# AI PARAM CALCULATION — Pionex AI Grid Bot 2.0 method
# ─────────────────────────────────────────────────────────────────

def calc_ai_params(candles):
    """
    Exact Pionex AI method:
      Range  : 3rd-97th percentile of 30d closes + 4% padding
      ATR-14 : 14-period average true range (hourly)
      Grids  : range / (0.5 * ATR-14), clamped 10-80
    """
    if len(candles) < 30:
        return None

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]

    sorted_cl = sorted(closes)
    n      = len(sorted_cl)
    lo_idx = max(0, int(0.03 * n))
    hi_idx = min(n - 1, int(0.97 * n))
    low_p  = sorted_cl[lo_idx]
    high_p = sorted_cl[hi_idx]
    pad    = (high_p - low_p) * 0.04
    lower  = round(low_p  - pad, 2)
    upper  = round(high_p + pad, 2)

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

    rng   = upper - lower
    step  = 0.5 * atr14
    grids = int(rng / step) if step > 0 else 20
    grids = max(10, min(80, grids))

    return {"lower": lower, "upper": upper, "grids": grids, "atr14": round(atr14, 2)}

# ─────────────────────────────────────────────────────────────────
# GRID BUILDER — with real BTC quantity per level
# ─────────────────────────────────────────────────────────────────

def build_grid(lower, upper, grids, investment):
    """
    FIX 4 — Real BTC quantity tracking (Pionex method):
      per_grid = investment / grids          (USDT per level)
      qty      = per_grid / buy_px           (BTC bought)
      sell_val = qty * sell_px               (USDT received on sell)
      profit   = sell_val*(1-fee) - per_grid*(1+fee)
    """
    if grids < 2:
        grids = 2
    step     = (upper - lower) / grids
    per_grid = investment / grids
    fee      = FEE_PCT / 100

    levels = []
    for i in range(grids):
        buy_px  = round(lower + i * step, 2)
        sell_px = round(buy_px + step, 2)
        qty     = per_grid / buy_px if buy_px > 0 else 0
        sell_val = qty * sell_px
        profit   = round(sell_val * (1 - fee) - per_grid * (1 + fee), 6)

        levels.append({
            "buy":         buy_px,
            "sell":        sell_px,
            "qty":         round(qty, 8),   # BTC per grid
            "profit":      profit,
            "filled":      False,
            "just_bought": False,           # FIX 3 — same-candle guard
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
    print(f"  Grid Portfolio created — Investment: ${INVESTMENT:,.2f}")
    return p


def save_portfolio(p):
    p["last_updated"] = datetime.now().isoformat()
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)

# ─────────────────────────────────────────────────────────────────
# DGT — Dynamic Grid Trading auto-reset
# ─────────────────────────────────────────────────────────────────

def check_support_distance(current_price, hourly_candles, lookback=24, min_pct=3.0):
    """
    Volsight-inspired: בודק שהמחיר לא קרוב מדי לתמיכה חזקה.
    מחזיר (True, pct) אם בטוח לפתוח גריד, (False, pct) אם קרוב מדי.
    lookback  = כמה נרות שעתיים לבדוק (ברירת מחדל: 24 = יום אחד)
    min_pct   = מרחק מינימלי מתמיכה באחוזים (ברירת מחדל: 3%)
    """
    if len(hourly_candles) < lookback:
        return True, 100.0  # אין מספיק נתונים — מאפשר
    recent = hourly_candles[-lookback:]
    support = min(c["low"] for c in recent)
    distance_pct = (current_price - support) / current_price * 100
    is_safe = distance_pct >= min_pct
    return is_safe, round(distance_pct, 2)


def check_and_apply_dgt(state, current_price, portfolio, hourly_candles):
    upper = state["upper"]
    lower = state["lower"]
    if not (current_price > upper * 1.005 or current_price < lower * 0.995):
        return False

    direction = "above" if current_price > upper else "below"
    print(f"  DGT triggered! BTC=${current_price:,.2f} is {direction} range "
          f"[${lower:,.2f} -- ${upper:,.2f}]")

    # ── Volsight-inspired: Support Distance Check ──────────────────
    is_safe, support_dist = check_support_distance(current_price, hourly_candles)
    if not is_safe:
        print(f"  ⚠️  Support Distance: {support_dist:.1f}% — קרוב מדי לתמיכה! "
              f"DGT נדחה (מינימום 3%)")
        return False
    print(f"  ✅ Support Distance: {support_dist:.1f}% — בטוח לאפס גריד")

    reinvested_total = portfolio.get("reinvested_total", 0.0)
    uncompounded     = portfolio["realized_pnl"] - reinvested_total
    new_investment   = portfolio["investment"]

    if REINVEST_ENABLED and uncompounded > 0:
        new_investment = round(portfolio["investment"] + uncompounded, 2)
        portfolio["reinvested_total"] = round(reinvested_total + uncompounded, 2)
        print(f"  Reinvest: +${uncompounded:.4f}  ->  New investment: ${new_investment:.2f}")

    portfolio["investment"] = new_investment
    portfolio["dgt_count"]  = portfolio.get("dgt_count", 0) + 1

    ai = calc_ai_params(hourly_candles)
    if ai is None:
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
        "lower":      ai["lower"],
        "upper":      ai["upper"],
        "grids":      ai["grids"],
        "atr14":      ai["atr14"],
        "investment": new_investment,
        "grid":       grid,
        "last_dgt":   datetime.now().isoformat(),
    })
    print(f"  New range: ${ai['lower']:,.2f} -- ${ai['upper']:,.2f}  "
          f"Grids: {ai['grids']}  ATR: ${ai['atr14']:,.2f}")
    return True

# ─────────────────────────────────────────────────────────────────
# FILL SIMULATION — Pionex-accurate (all 4 fixes applied)
# ─────────────────────────────────────────────────────────────────

def simulate_fills(state, recent_candles, portfolio):
    """
    FIX 1 — Only processes candles newer than last_processed_candle
             (caller supplies the right number of candles)

    FIX 2 — Per candle: SELL pass first, then BUY pass
             (mirrors real grid bot order book behavior)

    FIX 3 — just_bought flag: a level bought this candle
             cannot be sold in the same candle

    FIX 4 — Profit uses pre-calculated real BTC qty per level
    """
    grid         = state["grid"]
    realized_pnl = 0.0
    cycles       = 0
    log_entries  = []
    last_proc    = state.get("last_processed_candle", 0)

    new_candles = [c for c in recent_candles if c["time"] > last_proc]
    if not new_candles:
        return 0.0, 0

    for candle in new_candles:
        t = candle["time"]
        h = candle["high"]
        l = candle["low"]

        # ── PASS 1: SELLS ──────────────────────────────────────────
        # Price crossed UP through sell_px of a filled level
        for lvl in grid:
            if not lvl["filled"] or lvl.get("just_bought", False):
                continue
            sell_px = lvl["sell"]
            if l <= sell_px <= h:
                lvl["filled"] = False
                profit = lvl["profit"]
                realized_pnl += profit
                cycles += 1
                log_entries.append({
                    "type":   "SELL",
                    "price":  sell_px,
                    "qty":    lvl["qty"],
                    "profit": round(profit, 6),
                    "time":   datetime.fromtimestamp(t / 1000).isoformat(),
                })

        # ── PASS 2: BUYS ───────────────────────────────────────────
        # Price crossed DOWN through buy_px of an unfilled level
        for lvl in grid:
            if lvl["filled"]:
                continue
            buy_px = lvl["buy"]
            if l <= buy_px <= h:
                lvl["filled"]      = True
                lvl["just_bought"] = True    # guard: cannot sell until next candle
                log_entries.append({
                    "type":   "BUY",
                    "price":  buy_px,
                    "qty":    lvl["qty"],
                    "profit": 0,
                    "time":   datetime.fromtimestamp(t / 1000).isoformat(),
                })

        # Clear just_bought after each candle
        for lvl in grid:
            lvl["just_bought"] = False

        state["last_processed_candle"] = t

    # ── Update portfolio ───────────────────────────────────────────
    portfolio["realized_pnl"] = round(portfolio["realized_pnl"] + realized_pnl, 6)
    portfolio["total_cycles"] += cycles

    start_dt   = datetime.fromisoformat(portfolio["start_time"])
    days_run   = max(0.001, (datetime.now() - start_dt).total_seconds() / 86400)
    investment = portfolio["investment"]
    portfolio["apr"] = round(
        (portfolio["realized_pnl"] / investment) / days_run * 365 * 100, 2
    ) if investment > 0 else 0.0

    cur_val = investment + portfolio["realized_pnl"]
    if cur_val > portfolio.get("peak_value", investment):
        portfolio["peak_value"] = cur_val
    peak     = portfolio.get("peak_value", investment)
    drawdown = (peak - cur_val) / peak * 100 if peak > 0 else 0
    if drawdown > portfolio.get("max_drawdown", 0):
        portfolio["max_drawdown"] = round(drawdown, 2)

    portfolio["trade_log"] = (portfolio.get("trade_log", []) + log_entries)[-200:]
    state["grid"] = grid
    return realized_pnl, cycles

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  MODEL GRID v2.0 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Pionex AI Grid Bot 2.0 | BTC/USDT | DGT + ATR-14")
    print(f"{'='*60}\n")

    portfolio = load_portfolio()
    if portfolio is None:
        portfolio = init_portfolio()

    inv       = portfolio["investment"]
    pnl       = portfolio["realized_pnl"]
    total_val = round(inv + pnl, 2)
    start_dt  = datetime.fromisoformat(portfolio["start_time"])
    days_run  = max(0.001, (datetime.now() - start_dt).total_seconds() / 86400)

    print(f"  Investment : ${inv:,.2f}  |  P&L: ${pnl:+.6f}  |  Total: ${total_val:,.2f}")
    print(f"  APR        : {portfolio.get('apr', 0):.1f}%  |  "
          f"Cycles: {portfolio['total_cycles']}  |  "
          f"Running: {days_run:.1f}d  |  "
          f"DGT: {portfolio.get('dgt_count', 0)}")
    print(f"  MDD        : {portfolio.get('max_drawdown', 0):.2f}%\n")

    current_price = get_current_price()
    if current_price is None:
        print("  Cannot fetch BTC price — aborting")
        return
    print(f"  BTC Price  : ${current_price:,.2f}\n")

    print("  Fetching 30d hourly klines for AI params...")
    hourly = get_klines("1h", AI_HOURS)
    if not hourly:
        print("  No kline data — aborting")
        return

    state = load_state()

    # ── Backward-compat: rebuild grid if qty field missing ─────────
    if state and state.get("grid") and "qty" not in state["grid"][0]:
        print("  Upgrading grid to v2.0 (adding qty per level)...")
        state["grid"] = build_grid(
            state["lower"], state["upper"],
            state["grids"], state.get("investment", portfolio["investment"])
        )

    if state is None:
        # ── First run ──────────────────────────────────────────────
        print("  First run — calculating AI parameters...")
        ai = calc_ai_params(hourly)
        if ai is None:
            print("  Not enough data for AI params — aborting")
            return

        grid = build_grid(ai["lower"], ai["upper"], ai["grids"], portfolio["investment"])
        per_grid = portfolio["investment"] / ai["grids"]
        state = {
            "lower":                 ai["lower"],
            "upper":                 ai["upper"],
            "grids":                 ai["grids"],
            "atr14":                 ai["atr14"],
            "investment":            portfolio["investment"],
            "grid":                  grid,
            "prev_price":            current_price,
            "last_processed_candle": 0,
            "last_run_time":         datetime.now().isoformat(),
            "last_dgt":              None,
            "created":               datetime.now().isoformat(),
        }
        print(f"  AI Range   : ${ai['lower']:,.2f} -- ${ai['upper']:,.2f}")
        print(f"  ATR-14     : ${ai['atr14']:,.2f}  |  Grids: {ai['grids']}")
        print(f"  Per grid   : ${per_grid:.2f}  |  "
              f"Profit/cycle (mid): ~${grid[ai['grids']//2]['profit']:.4f}\n")

    else:
        # ── Subsequent run ─────────────────────────────────────────
        print(f"  Range      : ${state['lower']:,.2f} -- ${state['upper']:,.2f}  "
              f"Grids: {state['grids']}")
        if DGT_ENABLED:
            check_and_apply_dgt(state, current_price, portfolio, hourly)

    # ── FIX 1: Dynamic candle window based on elapsed time ─────────
    last_run_str = state.get("last_run_time")
    if last_run_str:
        elapsed_min  = (datetime.now() - datetime.fromisoformat(last_run_str)).total_seconds() / 60
        candle_limit = max(20, int(elapsed_min) + 10)   # +10 buffer
        candle_limit = min(candle_limit, 1000)           # Binance API max
    else:
        candle_limit = 30

    print(f"\n  Fetching {candle_limit} recent 1m candles...")
    recent = get_klines("1m", candle_limit)

    if recent:
        profit, cycles = simulate_fills(state, recent, portfolio)
        if cycles > 0:
            print(f"  {cycles} cycle(s) closed  |  Profit this run: ${profit:+.6f}")
        else:
            print("  No fills this interval (price range within grid, no full cross)")
    else:
        print("  No recent candles available")

    state["last_run_time"] = datetime.now().isoformat()

    # ── Grid status bar ────────────────────────────────────────────
    filled_count = sum(1 for lvl in state["grid"] if lvl["filled"])
    unfilled     = state["grids"] - filled_count
    rng          = state["upper"] - state["lower"]
    in_pct       = (current_price - state["lower"]) / rng * 100 if rng > 0 else 50
    in_pct       = max(0, min(100, in_pct))
    bar_p        = int(in_pct / 100 * 30)
    bar          = "=" * bar_p + "." * (30 - bar_p)

    print(f"\n  Grid       : {filled_count} filled / {unfilled} waiting "
          f"(total {state['grids']})")
    print(f"  Position   : [{bar}] {in_pct:.0f}% in range")
    print(f"  Range      : ${state['lower']:,.2f} <-> ${state['upper']:,.2f}")

    state["prev_price"] = current_price
    save_state(state)
    save_portfolio(portfolio)

    total_val = round(portfolio["investment"] + portfolio["realized_pnl"], 2)
    print(f"\n  Total: ${total_val:,.2f}  |  "
          f"APR: {portfolio.get('apr', 0):.1f}%  |  "
          f"Cycles: {portfolio['total_cycles']}")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
