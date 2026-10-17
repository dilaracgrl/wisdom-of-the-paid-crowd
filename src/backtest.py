"""Q6 — Strategy backtest: is the favourite-longshot bias worth money?

Q2 showed longshots (low YES price) are overpriced and favourites underpriced.
This turns that into a trading rule and asks what it would have returned after
costs. No look-ahead: enter at a fixed horizon before resolution (default the
price 24h out) and hold to settlement.

Strategies
----------
fade-longshots : bet NO on every market whose YES price <= p_max (the overpriced
                 side). Profit per $1-notional contract = (p - y) - costs, where
                 y is the outcome — exactly the `ev_no` from Q2.
back-favourites: bet YES on every market whose YES price >= p_min.

Costs & realism
---------------
  * spread   — you transact at the ask, not the mid snapshot; charged per contract.
  * fee      — per-contract fee (Polymarket CLOB ~0; set for conservatism).
  * min_volume — a liquidity floor: thin books rarely fill at the quoted longshot
                 price, so we only bet where there was real volume (ties to Q3).

Reported: number of bets, capital staked, PnL, ROI, hit rate, and a t-stat on the
per-bet edge (is ROI distinguishable from zero, or just noise?). Holding period is
the entry horizon, so ROI is a per-bet return over ~that window, not annualized.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd


def run_bets(p: np.ndarray, y: np.ndarray, side: str,
             spread: float = 0.0, fee: float = 0.0) -> dict:
    """Flat 1-contract bets. side='no' buys NO at (1-p); 'yes' buys YES at p."""
    if len(p) == 0:
        return {"n": 0, "staked": 0.0, "pnl": 0.0, "roi": float("nan"),
                "hit_rate": float("nan"), "t_stat": float("nan")}
    if side == "no":
        cost = (1 - p) + spread + fee
        payoff = (1 - y)
    else:
        cost = p + spread + fee
        payoff = y
    profit = payoff - cost
    staked = cost.sum()
    roi = profit.sum() / staked if staked > 0 else float("nan")
    sd = profit.std(ddof=1) if len(profit) > 1 else 0.0
    t = (profit.mean() / (sd / np.sqrt(len(profit)))) if sd > 0 else float("nan")
    return {"n": int(len(p)), "staked": float(staked), "pnl": float(profit.sum()),
            "roi": float(roi), "hit_rate": float((payoff > 0).mean()),
            "t_stat": float(t), "mean_pnl": float(profit.mean())}


def _subset(df, prob_col, min_volume):
    d = df[df[prob_col].notna()]
    if min_volume:
        d = d[d["volume"] >= min_volume]
    return d


def fade_longshots(df, prob_col, p_max, spread=0.0, fee=0.0, min_volume=0.0) -> dict:
    d = _subset(df, prob_col, min_volume)
    d = d[d[prob_col] <= p_max]
    return run_bets(d[prob_col].to_numpy(float), d["outcome"].to_numpy(float),
                    "no", spread, fee)


def back_favourites(df, prob_col, p_min, spread=0.0, fee=0.0, min_volume=0.0) -> dict:
    d = _subset(df, prob_col, min_volume)
    d = d[d[prob_col] >= p_min]
    return run_bets(d[prob_col].to_numpy(float), d["outcome"].to_numpy(float),
                    "yes", spread, fee)


def _print_sweep(title, rows, thresh_label):
    print(f"\n{title}")
    print(f"  {thresh_label:>7} {'n':>5} {'staked':>8} {'pnl':>8} {'roi':>7} "
          f"{'hit%':>6} {'t':>6}")
    for th, r in rows:
        if r["n"] == 0:
            print(f"  {th:>7} {0:>5}")
            continue
        print(f"  {th:>7} {r['n']:>5} {r['staked']:>8.1f} {r['pnl']:>+8.1f} "
              f"{r['roi']*100:>+6.1f}% {r['hit_rate']*100:>5.1f}% {r['t_stat']:>+6.2f}")


def _read_table(path):
    return pd.read_csv(path) if path.endswith(".csv") else pd.read_parquet(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Q6 favourite-longshot strategy backtest.")
    ap.add_argument("--snapshots", default="data/rep_snap.parquet")
    ap.add_argument("--prob-col", default="price_h24")
    ap.add_argument("--spread", type=float, default=0.01, help="per-contract spread cost")
    ap.add_argument("--fee", type=float, default=0.0, help="per-contract fee")
    ap.add_argument("--min-volume", type=float, default=1000.0, help="liquidity floor")
    args = ap.parse_args()

    df = _read_table(args.snapshots)
    print(f"Backtest on {args.snapshots} — forecast={args.prob_col}, "
          f"spread={args.spread}, fee={args.fee}, min_volume=${args.min_volume:,.0f}")

    # 1) fade-longshots threshold sweep
    fade_rows = [(f"<={t:.2f}", fade_longshots(df, args.prob_col, t, args.spread,
                                               args.fee, args.min_volume))
                 for t in (0.05, 0.10, 0.20, 0.30, 0.50)]
    _print_sweep("Fade longshots (bet NO when YES price <= p_max):", fade_rows, "p_max")

    # 2) back-favourites threshold sweep
    fav_rows = [(f">={t:.2f}", back_favourites(df, args.prob_col, t, args.spread,
                                              args.fee, args.min_volume))
                for t in (0.95, 0.90, 0.80, 0.70, 0.50)]
    _print_sweep("Back favourites (bet YES when YES price >= p_min):", fav_rows, "p_min")

    # 3) spread sensitivity on the headline fade rule (<=0.50)
    print("\nSpread sensitivity — fade longshots <=0.50:")
    print(f"  {'spread':>7} {'roi':>7} {'pnl':>8}")
    for sp in (0.0, 0.01, 0.02, 0.03):
        r = fade_longshots(df, args.prob_col, 0.50, sp, args.fee, args.min_volume)
        print(f"  {sp:>7.2f} {r['roi']*100:>+6.1f}% {r['pnl']:>+8.1f}")

    # 4) sanity check: the opposite rule (back longshots) should lose
    anti = run_bets(*[_subset(df, args.prob_col, args.min_volume)
                      .query(f"{args.prob_col} <= 0.50")[c].to_numpy(float)
                      for c in (args.prob_col, "outcome")], "yes", args.spread, args.fee)
    print(f"\nSanity (BUY longshots <=0.50 instead): roi {anti['roi']*100:+.1f}%, "
          f"t {anti['t_stat']:+.2f}  (should be negative if the bias is real)")
    print("\n  ROI is per-bet over the entry horizon's holding period; not annualized.")


if __name__ == "__main__":
    main()
