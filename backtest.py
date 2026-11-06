"""Usage: python backtest.py history.csv   (columns: t,o,h,l,c  - 5-minute XAUUSD, t = unix seconds UTC)
Without a CSV it runs on SYNTHETIC data, so the numbers are meaningless. News filter is NOT backtested."""
import sys, csv, json
from engine import *

COST = dict(spread=0.30, slippage=0.10, commission_per_lot=7.0, contract_size=100)  # edit to match your broker


def load(path):
    return [dict(t=int(r["t"]), o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"])) for r in csv.DictReader(open(path))]


def simulate(m5, start, step=12, cfg=CFG, cost=COST):
    trades = []; i = start
    while i < len(m5) - 2:
        env = dict(now=m5[i]["t"] + 300, spread=cost["spread"], news_ok=True, blackout=False)
        x, b, s, j = cycle(m5[:i + 1], env, cfg)  # data up to bar i only: no look-ahead
        if j["decision"] == "WAIT" or not j["plan"]: i += step; continue
        d = 1 if j["decision"] == "BUY" else -1; p = j["plan"]; tgt = p["tp2"] or p["tp1"]
        ent = m5[i + 1]["o"] + d * (cost["spread"] / 2 + cost["slippage"])  # next open + costs
        risk = (ent - p["sl"]) * d
        if risk <= 0 or not tgt or (tgt - ent) * d / risk < cfg["min_rr"]: i += step; continue
        exit_, k = m5[min(i + 300, len(m5) - 1)]["c"], i + 1
        for k in range(i + 1, min(i + 300, len(m5))):
            b_ = m5[k]
            if (b_["l"] <= p["sl"]) if d == 1 else (b_["h"] >= p["sl"]): exit_ = p["sl"] - d * cost["slippage"]; break  # SL first if both hit
            if (b_["h"] >= tgt) if d == 1 else (b_["l"] <= tgt): exit_ = tgt; break
        pnl = (exit_ - ent) * d - cost["commission_per_lot"] / cost["contract_size"]
        trades.append(dict(t=m5[i]["t"], R=pnl / risk)); i = k + 1
    return trades


def metrics(tr):
    if not tr: return dict(trades=0)
    R = [t["R"] for t in tr]; w = [r for r in R if r > 0]; l = [-r for r in R if r < 0]
    eq = peak = dd = 0
    for r in R: eq += r; peak = max(peak, eq); dd = max(dd, peak - eq)
    return dict(trades=len(R), win_rate=round(len(w) / len(R), 3), expectancy_R=round(sum(R) / len(R), 3),
                profit_factor=round(sum(w) / sum(l), 2) if l else None, max_drawdown_R=round(dd, 2))


if __name__ == "__main__":
    synth = len(sys.argv) < 2
    m5 = sample_m5(6000) if synth else load(sys.argv[1])
    if synth: print("WARNING: synthetic data - results are NOT evidence of any edge.")
    cut = int(len(m5) * .7)  # rules are fixed, not fitted; last 30% is the held-out period
    warm = 2500
    print(json.dumps(dict(in_sample=metrics(simulate(m5[:cut], warm)), out_of_sample=metrics(simulate(m5, cut))), indent=2))
