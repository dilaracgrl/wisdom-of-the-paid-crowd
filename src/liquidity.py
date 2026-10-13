"""Q3 — Liquidity vs accuracy: are thin markets worse forecasters?

Slice markets by traded volume (or open interest / liquidity) and score each
stratum with the Q1 engine: Brier, skill score, calibration error, resolution.

The confound this module is built to handle
------------------------------------------
Thin markets are disproportionately *longshots* (prices near 0 that resolve No).
Those are easy, so a thin bucket can post a LOW raw Brier for the wrong reason —
not because the crowd is sharp but because the questions were obvious. Raw Brier
by volume is therefore misleading on its own. Two guards:

  * ECE / reliability   — isolates *miscalibration* from difficulty.
  * controlled view     — restrict to a mid-price band (default 0.20-0.80), the
                          genuinely contested markets, then compare Brier across
                          volume. This removes the easy-longshot tailwind.

A real "thin markets forecast worse" result should show up as worse calibration
and worse controlled-Brier in low-volume strata, not just higher raw Brier.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from .calibration import analyze as calib_analyze

# Log-spaced volume strata (USD); volume spans several orders of magnitude.
DEFAULT_VOL_EDGES = [0, 100, 1_000, 10_000, 100_000, 1e15]


def _fmt_edge(x: float) -> str:
    if x >= 1e15:
        return "inf"
    if x >= 1000:
        return f"{x/1000:.0f}k"
    return f"{x:.0f}"


def by_bucket(
    df: pd.DataFrame, col: str, edges, prob_col: str,
    price_band: tuple[float, float] | None = None,
) -> pd.DataFrame:
    """Per volume/liquidity bucket, run the Q1 calibration analysis."""
    base = df
    if price_band is not None:
        lo, hi = price_band
        base = df[(df[prob_col] >= lo) & (df[prob_col] <= hi)]
    rows = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        g = base[(base[col] >= lo) & (base[col] < hi)]
        r = calib_analyze(g, prob_col)
        if r.get("n", 0) == 0:
            continue
        rows.append({
            "bucket": f"[{_fmt_edge(lo)},{_fmt_edge(hi)})",
            "n": r["n"],
            "median": float(g[col].median()),
            "base_rate": r["base_rate"],
            "brier": r["brier"],
            "bss": r["brier_skill_score"],
            "ece": r["ece"],
            "resolution": r["resolution"],
        })
    return pd.DataFrame(rows)


def _print_table(title: str, t: pd.DataFrame, unit: str) -> None:
    print(f"\n{title}")
    print(f"  {'bucket':14s} {'n':>5} {'median':>9} {'base':>5} "
          f"{'brier':>7} {'bss':>7} {'ece':>6} {'resol':>6}")
    for _, r in t.iterrows():
        med = _fmt_edge(r["median"]) if unit == "$" else f"{r['median']:.0f}"
        print(f"  {r['bucket']:14s} {r['n']:>5} {unit}{med:>8} {r['base_rate']:>5.2f} "
              f"{r['brier']:>7.4f} {r['bss']:>+7.3f} {r['ece']:>6.3f} {r['resolution']:>6.3f}")


def analyze(df: pd.DataFrame, col: str = "volume", prob_col: str = "price_h24",
            edges=DEFAULT_VOL_EDGES, band=(0.20, 0.80)) -> dict:
    full = by_bucket(df, col, edges, prob_col)
    controlled = by_bucket(df, col, edges, prob_col, price_band=band)
    return {"col": col, "band": band, "full": full, "controlled": controlled}


def print_report(res: dict, prob_col: str) -> None:
    col, band = res["col"], res["band"]
    unit = "$" if col in ("volume", "liquidity") else ""
    print(f"\n=== Liquidity vs accuracy — by {col}, forecast = {prob_col} ===")
    _print_table(f"All markets:", res["full"], unit)
    _print_table(f"Controlled (price in [{band[0]:.2f},{band[1]:.2f}] — contested only):",
                 res["controlled"], unit)

    # trend read: does calibration improve with volume? (ECE down, controlled brier down)
    c = res["controlled"]
    if len(c) >= 2:
        lo_ece, hi_ece = c.iloc[0]["ece"], c.iloc[-1]["ece"]
        lo_br, hi_br = c.iloc[0]["brier"], c.iloc[-1]["brier"]
        print(f"\n  thinnest->thickest (contested): ECE {lo_ece:.3f}->{hi_ece:.3f}, "
              f"Brier {lo_br:.4f}->{hi_br:.4f}")
        # crude reliability threshold: first bucket whose controlled ECE <= 0.05
        reliable = c[c["ece"] <= 0.05]
        if len(reliable):
            print(f"  first bucket with contested ECE <= 0.05: {reliable.iloc[0]['bucket']} "
                  f"(median {unit}{_fmt_edge(reliable.iloc[0]['median'])})")
        else:
            print("  no volume bucket reaches contested ECE <= 0.05")
    print("  (bss>0 beats base rate; ece lower=better calibrated; resolution higher=sharper)")


def _read_table(path: str) -> pd.DataFrame:
    return pd.read_csv(path) if path.endswith(".csv") else pd.read_parquet(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Q3 liquidity vs accuracy.")
    ap.add_argument("--snapshots", default="data/rep_snap.parquet")
    ap.add_argument("--prob-col", default="price_h24")
    ap.add_argument("--by", default="volume", help="column to stratify by (volume/liquidity)")
    ap.add_argument("--band", default="0.20,0.80", help="contested price band 'lo,hi'")
    ap.add_argument("--out", default=None, help="optional CSV path for the full table")
    args = ap.parse_args()

    df = _read_table(args.snapshots)
    lo, hi = (float(x) for x in args.band.split(","))
    res = analyze(df, args.by, args.prob_col, band=(lo, hi))
    print_report(res, args.prob_col)
    if args.out:
        res["full"].to_csv(args.out, index=False)
        print(f"\nWrote full table -> {args.out}")


if __name__ == "__main__":
    main()
