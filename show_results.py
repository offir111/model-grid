"""
MODEL GRID — Show Simulation Results
Run: python show_results.py
"""
import json, os
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def load(fname):
    path = os.path.join(DATA_DIR, fname)
    if not os.path.exists(path): return None
    with open(path, encoding="utf-8") as f: return json.load(f)

portfolio = load("portfolio.json")
state     = load("grid_state.json")

if not portfolio:
    print("No data yet. Run grid_simulator.py first.")
    exit()

p        = portfolio
inv      = p["investment"]
pnl      = p["realized_pnl"]
total    = round(inv + pnl, 2)
start_dt = datetime.fromisoformat(p["start_time"])
days_run = max(0.001, (datetime.now() - start_dt).total_seconds() / 86400)
ann_rate = p.get("apr", 0)

print(f"\n{'='*60}")
print(f"  MODEL GRID — Pionex AI Grid Bot 2.0 | BTC/USDT")
print(f"  Sim started: {p['start_time'][:10]}  |  Running: {days_run:.1f} days")
print(f"{'='*60}")
print(f"\n  💼 Investment Summary")
print(f"  Investment : ${inv:,.2f}  (started: ${p['starting_investment']:,.2f})")
print(f"  Realized   : ${pnl:+.4f}")
print(f"  Total Value: ${total:,.2f}")
print(f"  Reinvested : ${p.get('reinvested_total', 0):.4f}")

print(f"\n  📊 Performance")
print(f"  APR        : {ann_rate:.1f}%")
print(f"  Cycles     : {p['total_cycles']}")
print(f"  DGT resets : {p.get('dgt_count', 0)}")
print(f"  MDD        : {p.get('max_drawdown', 0):.2f}%")
print(f"  Peak value : ${p.get('peak_value', inv):,.4f}")

if state:
    filled = sum(1 for lvl in state["grid"] if lvl["filled"])
    total_grids = state["grids"]
    print(f"\n  📐 Grid State")
    print(f"  Range      : ${state['lower']:,.2f} — ${state['upper']:,.2f}")
    print(f"  ATR-14     : ${state.get('atr14', 0):,.2f}")
    print(f"  Grids      : {total_grids}  ({filled} filled / {total_grids - filled} waiting)")
    if state.get("last_dgt"):
        print(f"  Last DGT   : {state['last_dgt'][:16]}")

# Last 10 trades
log = p.get("trade_log", [])
if log:
    sells = [t for t in log if t["type"] == "SELL"]
    if sells:
        print(f"\n  📋 Last {min(10, len(sells))} fills:")
        for t in sells[-10:]:
            print(f"  ✅ SELL @ ${t['price']:,.2f}  profit=${t['profit']:+.4f}  {t['time'][:16]}")
else:
    print("\n  📋 No fills yet — bot running, waiting for first grid cycle")

print(f"\n{'='*60}\n")
