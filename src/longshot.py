"""Q2 — Favourite-longshot bias: do longshots win less often than priced?

Classic result from racetrack betting: longshots are systematically overbet
(win less than their odds imply) and favourites underbet. A prediction market
price is an implied probability, so the same test applies directly: within each
price bucket, compare the mean price (implied prob) to the realized win rate.

    bias = win_rate - price
      < 0  -> overpriced (wins less than it costs)  -> classic longshot
      > 0  -> underpriced (wins more than it costs)  -> classic favourite

Buckets are fine in the tails, where the bias concentrates. Each win rate gets
a Wilson 95% interval (good for small n and extreme proportions), and a z-test
flags buckets whose realized rate differs significantly from the priced rate.

`ev_yes` is the expected profit per $1 paid to buy YES at the bucket's mean price
(= win_rate - price, fees aside); `ev_no` is the same for buying NO (betting
*against*). It previews the Q6 backtest: a positive `ev_no` on overpriced
longshots is the harvestable edge.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

# Finer in the tails, where favourite-longshot bias lives.
DEFAULT_EDGES = [0, .01, .02, .03, .05, .10, .20, .30, .40, .50,
                 .60, .70, .80, .90, .95, .97, .98, .99, 1.0]


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a binomial proportion (k successes of n)."""
    if n == 0:
        return (float("nan"), float("nan"))
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z / denom) * np.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def _ztest_vs_price(k: int, n: int, p0: float) -> float:
    """Two-sided z (obs rate vs priced rate p0). |z| > 1.96 ~ significant at 5%."""
    if n == 0 or p0 <= 0 or p0 >= 1:
        return float("nan")
    se = np.sqrt(p0 * (1 - p0) / n)
    return (k / n - p0) / se if se > 0 else float("nan")


def bias_table(p: np.ndarray, y: np.ndarray, edges=DEFAULT_EDGES) -> pd.DataFrame:
    """Per price-bucket win rate, Wilson CI, bias, EV, and significance."""
    edges = np.asarray(edges, dtype=float)
    idx = np.clip(np.digitize(p, edges[1:-1], right=False), 0, len(edges) - 2)
    rows = []
    for b in range(len(edges) - 1):
        mask = idx == b
        n = int(mask.sum())
        if n == 0:
            continue
        k = int(y[mask].sum())
        price = float(p[mask].mean())
        win = k / n
        lo, hi = wilson_interval(k, n)
        z = _ztest_vs_price(k, n, price)
        rows.append({
            "bucket": f"[{edges[b]:.2f},{edges[b + 1]:.2f})",
            "n": n,
            "price": price,          # implied prob (mean within bucket)
            "win_rate": win,         # realized
            "ci_lo": lo, "ci_hi": hi,
            "bias": win - price,     # <0 overpriced, >0 underpriced
            "ev_yes": win - price,   # profit/$1 buying YES at price
            "ev_no": price - win,    # profit/$1 buying NO at (1-price)
            "sig": bool(abs(z) > 1.96) if z == z else False,
        })
    return pd.DataFrame(rows)


def lpm_signature(p: np.ndarray, y: np.ndarray) -> dict:
    """Linear-probability fit outcome ~ a + b*price.

    Perfect calibration => a=0, b=1. Favourite-longshot bias compresses prices
    toward the center, so the realized relationship is *steeper*: b>1, a<0.
    """
    if len(p) < 3:
        return {"slope": float("nan"), "intercept": float("nan")}
    b, a = np.polyfit(p, y, 1)
    return {"slope": float(b), "intercept": float(a)}


def tail_summary(p: np.ndarray, y: np.ndarray, thresh: float = 0.10) -> dict:
    """Aggregate the longshot (price<thresh) and favourite (price>1-thresh) tails."""
    out = {}
    for name, mask in (("longshots", p < thresh), ("favourites", p > 1 - thresh)):
        n = int(mask.sum())
        if n == 0:
            out[name] = {"n": 0}
            continue
        k = int(y[mask].sum())
        lo, hi = wilson_interval(k, n)
        out[name] = {"n": n, "price": float(p[mask].mean()), "win_rate": k / n,
                     "ci_lo": lo, "ci_hi": hi, "bias": k / n - float(p[mask].mean())}
    return out


def analyze(df: pd.DataFrame, prob_col: str = "price_h24",
            edges=DEFAULT_EDGES, label: str = "all") -> dict:
    sub = df[[prob_col, "outcome"]].dropna()
    p = np.clip(sub[prob_col].to_numpy(dtype=float), 0.0, 1.0)
    y = sub["outcome"].to_numpy(dtype=float)
    if len(p) == 0:
        return {"label": label, "n": 0}
    return {
        "label": label, "n": len(p),
        "table": bias_table(p, y, edges),
        "lpm": lpm_signature(p, y),
        "tails": tail_summary(p, y),
    }


def print_report(res: dict, prob_col: str) -> None:
    if res.get("n", 0) == 0:
        print(f"[{res['label']}] no usable rows for {prob_col}")
        return
    t = res["table"]
    print(f"\n=== Favourite-longshot bias [{res['label']}] — forecast = {prob_col} ===")
    print(f"  markets: {res['n']}")
    print(f"  {'bucket':14s} {'n':>5} {'price':>6} {'win':>6} "
          f"{'95% CI':>15} {'bias':>7} {'ev_no':>7}  sig")
    for _, r in t.iterrows():
        ci = f"[{r['ci_lo']:.2f},{r['ci_hi']:.2f}]"
        star = "*" if r["sig"] else " "
        print(f"  {r['bucket']:14s} {r['n']:>5} {r['price']:>6.3f} {r['win_rate']:>6.3f} "
              f"{ci:>15} {r['bias']:>+7.3f} {r['ev_no']:>+7.3f}  {star}")
    lpm = res["lpm"]
    print(f"\n  LPM fit  outcome = {lpm['intercept']:+.3f} + {lpm['slope']:.3f}*price "
          f"(calibrated=0,1; longshot bias => slope>1, intercept<0)")
    for name, d in res["tails"].items():
        if d.get("n", 0) == 0:
            continue
        print(f"  {name:10s} n={d['n']:>4}  priced {d['price']:.3f}  "
              f"won {d['win_rate']:.3f}  [{d['ci_lo']:.3f},{d['ci_hi']:.3f}]  "
              f"bias {d['bias']:+.3f}")
    print("  (bias<0 = overpriced; ev_no = profit/$1 betting NO; * = differs from price at 5%)")


def _read_table(path: str) -> pd.DataFrame:
    return pd.read_csv(path) if path.endswith(".csv") else pd.read_parquet(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Q2 favourite-longshot bias analysis.")
    ap.add_argument("--snapshots", default="data/rep_snap.parquet")
    ap.add_argument("--prob-col", default="price_h24")
    ap.add_argument("--group-by", default=None, help="split by a column (e.g. venue)")
    ap.add_argument("--out", default=None, help="optional CSV path for the bias table")
    args = ap.parse_args()

    df = _read_table(args.snapshots)
    overall = analyze(df, args.prob_col, label="all")
    print_report(overall, args.prob_col)

    if args.group_by and args.group_by in df.columns:
        for key, g in df.groupby(args.group_by):
            print_report(analyze(g, args.prob_col, label=str(key)), args.prob_col)

    if args.out and overall.get("n", 0):
        overall["table"].to_csv(args.out, index=False)
        print(f"\nWrote bias table -> {args.out}")


if __name__ == "__main__":
    main()
