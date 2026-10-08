"""Q1 — Calibration: do prices match outcomes?

A market price is a probability forecast. This module scores how good that
forecast is, using the snapshot table from `snapshot.py`.

Metrics
-------
Brier score          mean((p - y)^2); 0 is perfect, 0.25 is a coin flip.
Brier skill score    1 - Brier / Brier_baseline, vs always predicting the base
                     rate; >0 means the market beats the base rate.
Calibration table    forecasts binned; mean predicted vs observed frequency per
                     bin (this is the data behind the calibration curve).
ECE / MCE            expected / max calibration error (count-weighted / worst bin).
Murphy decomposition Brier = reliability - resolution + uncertainty
                       reliability  (lower=better): how far bins stray from their
                                    own realized frequency — pure miscalibration.
                       resolution   (higher=better): how much outcomes vary across
                                    bins — the forecast's discriminating power.
                       uncertainty  = base_rate*(1-base_rate), the irreducible floor.

The default forecast column is `price_h24` (price 24h before resolution).
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd


def brier_score(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def calibration_table(
    p: np.ndarray, y: np.ndarray, n_bins: int = 10, strategy: str = "uniform"
) -> pd.DataFrame:
    """Bin forecasts and report mean predicted vs observed frequency per bin."""
    if strategy == "quantile":
        edges = np.unique(np.quantile(p, np.linspace(0, 1, n_bins + 1)))
    else:
        edges = np.linspace(0.0, 1.0, n_bins + 1)
    # assign each forecast to a bin; clip the right edge into the last bin
    idx = np.clip(np.digitize(p, edges[1:-1], right=False), 0, len(edges) - 2)
    rows = []
    for b in range(len(edges) - 1):
        mask = idx == b
        n = int(mask.sum())
        if n == 0:
            continue
        rows.append({
            "bin": f"[{edges[b]:.2f},{edges[b + 1]:.2f})",
            "n": n,
            "pred_mean": float(p[mask].mean()),
            "obs_freq": float(y[mask].mean()),
            "gap": float(p[mask].mean() - y[mask].mean()),
        })
    return pd.DataFrame(rows)


def murphy_decomposition(table: pd.DataFrame, y: np.ndarray) -> dict:
    """Reliability / resolution / uncertainty from a calibration table."""
    n_total = len(y)
    base = float(y.mean())
    rel = float((table["n"] * (table["pred_mean"] - table["obs_freq"]) ** 2).sum() / n_total)
    res = float((table["n"] * (table["obs_freq"] - base) ** 2).sum() / n_total)
    unc = base * (1.0 - base)
    return {"reliability": rel, "resolution": res, "uncertainty": unc,
            "brier_from_decomp": rel - res + unc}


def calibration_errors(table: pd.DataFrame) -> dict:
    n_total = table["n"].sum()
    ece = float((table["n"] * table["gap"].abs()).sum() / n_total)
    mce = float(table["gap"].abs().max())
    return {"ece": ece, "mce": mce}


def _ascii_curve(table: pd.DataFrame, width: int = 40) -> str:
    """A tiny terminal calibration plot: 'P' predicted, 'O' observed per bin."""
    lines = ["  bin            pred  obs    0" + " " * (width - 5) + "1"]
    for _, r in table.iterrows():
        row = [" "] * (width + 1)
        pp = int(round(r["pred_mean"] * width))
        oo = int(round(r["obs_freq"] * width))
        row[pp] = "P"
        row[oo] = "O" if oo != pp else "X"  # X = perfectly calibrated here
        lines.append(f"  {r['bin']:14s} {r['pred_mean']:.2f}  {r['obs_freq']:.2f}  |{''.join(row)}|")
    lines.append("  (P=predicted  O=observed  X=aligned)")
    return "\n".join(lines)


def analyze(
    df: pd.DataFrame, prob_col: str = "price_h24", n_bins: int = 10,
    strategy: str = "uniform", label: str = "all",
) -> dict:
    """Run the full Q1 analysis on one (sub)set and return a results dict."""
    sub = df[[prob_col, "outcome"]].dropna()
    p = np.clip(sub[prob_col].to_numpy(dtype=float), 0.0, 1.0)
    y = sub["outcome"].to_numpy(dtype=float)
    if len(p) == 0:
        return {"label": label, "n": 0}

    base = float(y.mean())
    bs = brier_score(p, y)
    bs_base = brier_score(np.full_like(y, base), y)  # climatology baseline
    table = calibration_table(p, y, n_bins, strategy)
    decomp = murphy_decomposition(table, y)
    errs = calibration_errors(table)
    return {
        "label": label, "n": len(p), "base_rate": base,
        "brier": bs, "brier_baseline": bs_base,
        "brier_skill_score": (1 - bs / bs_base) if bs_base > 0 else float("nan"),
        **errs, **decomp, "table": table,
    }


def print_report(res: dict, prob_col: str) -> None:
    if res.get("n", 0) == 0:
        print(f"[{res['label']}] no usable rows for {prob_col}")
        return
    print(f"\n=== Calibration [{res['label']}] — forecast = {prob_col} ===")
    print(f"  markets:            {res['n']}")
    print(f"  base rate (YES):    {res['base_rate']:.3f}")
    print(f"  Brier score:        {res['brier']:.4f}   (baseline {res['brier_baseline']:.4f})")
    print(f"  Brier skill score:  {res['brier_skill_score']:+.3f}   (>0 beats base rate)")
    print(f"  ECE / MCE:          {res['ece']:.4f} / {res['mce']:.4f}")
    print(f"  reliability (lo=good): {res['reliability']:.4f}")
    print(f"  resolution  (hi=good): {res['resolution']:.4f}")
    print(f"  uncertainty (floor):   {res['uncertainty']:.4f}")
    print(_ascii_curve(res["table"]))


def _read_table(path: str) -> pd.DataFrame:
    return pd.read_csv(path) if path.endswith(".csv") else pd.read_parquet(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Q1 calibration + Brier analysis.")
    ap.add_argument("--snapshots", default="data/snapshots.parquet")
    ap.add_argument("--prob-col", default="price_h24")
    ap.add_argument("--bins", type=int, default=10)
    ap.add_argument("--strategy", choices=["uniform", "quantile"], default="uniform")
    ap.add_argument("--group-by", default=None,
                    help="column to split by (e.g. venue, category)")
    ap.add_argument("--out", default=None, help="optional CSV path for the calibration table")
    args = ap.parse_args()

    df = _read_table(args.snapshots)
    overall = analyze(df, args.prob_col, args.bins, args.strategy, label="all")
    print_report(overall, args.prob_col)

    if args.group_by and args.group_by in df.columns:
        print(f"\n--- by {args.group_by} ---")
        summary = []
        for key, g in df.groupby(args.group_by):
            r = analyze(g, args.prob_col, args.bins, args.strategy, label=str(key))
            if r.get("n", 0) > 0:
                summary.append({args.group_by: key, "n": r["n"],
                                "brier": round(r["brier"], 4),
                                "bss": round(r["brier_skill_score"], 3),
                                "ece": round(r["ece"], 4)})
        if summary:
            print(pd.DataFrame(summary).sort_values("n", ascending=False).to_string(index=False))

    if args.out:
        overall["table"].to_csv(args.out, index=False)
        print(f"\nWrote calibration table -> {args.out}")


if __name__ == "__main__":
    main()
