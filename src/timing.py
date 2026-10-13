"""Q4 — How early does the market know?

Track forecast accuracy as resolution approaches, using the per-horizon price
columns from `snapshot.py` (price_h168 ... price_h1, then price_last).

Two methodology choices that make the curve honest:

  * Balanced panel. Coverage falls off at long horizons (a market open only 3
    days before close has no price_h168), so a naive per-horizon Brier mixes
    different market subsets. We restrict to markets with a price at *every*
    horizon, so the curve follows one fixed cohort. This shrinks n (and tilts
    toward longer-lived markets) — reported explicitly.

  * Category by keyword. Gamma's `category` field is ~97% empty, so topic is
    derived from the question text (politics / crypto / sports / econ / other).
    Approximate by construction — a heuristic label, not ground truth.

The "how early" read: of the total Brier improvement from the earliest horizon to
final settlement, how much is already in the price at each horizon — and the
earliest horizon that has closed half the gap.
"""
from __future__ import annotations

import argparse

import pandas as pd

from .calibration import analyze as calib_analyze

DEFAULT_HORIZONS = [168, 72, 24, 12, 6, 1]

CATEGORY_KEYWORDS = {
    "politics": ["trump", "biden", "harris", "election", "president", "senate",
                 "congress", "governor", "democrat", "republican", "primary",
                 "vote", "poll ", "parliament", "prime minister", "cabinet"],
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol ",
               "dogecoin", "token", "altcoin", "binance", "coinbase"],
    "sports": ["nba", "nfl", "nhl", "mlb", "win the", "super bowl", "world cup",
               "champion", " vs ", "playoff", "finals", "ufc", "premier league",
               "medal", "olympic"],
    "econ": ["cpi", "inflation", "fed ", "interest rate", "gdp", "jobs",
             "unemployment", "recession", "payroll", "rate cut", "rate hike"],
}


def classify(question: str) -> str:
    q = f" {str(question).lower()} "
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in q for k in kws):
            return cat
    return "other"


def balanced_panel(df: pd.DataFrame, horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    cols = [f"price_h{h}" for h in horizons] + ["price_last"]
    cols = [c for c in cols if c in df.columns]
    return df.dropna(subset=cols)


def timing_curve(df: pd.DataFrame, horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    """Brier / skill / ECE at each horizon for the given (already-panel) set."""
    rows = []
    steps = [(h, f"price_h{h}") for h in horizons] + [(0, "price_last")]
    for h, col in steps:
        if col not in df.columns:
            continue
        r = calib_analyze(df, col)
        if r.get("n", 0) == 0:
            continue
        rows.append({"hours_out": h, "n": r["n"], "brier": r["brier"],
                     "bss": r["brier_skill_score"], "ece": r["ece"]})
    return pd.DataFrame(rows)


def how_early(curve: pd.DataFrame) -> dict:
    """Of the earliest->final Brier gain, what fraction is in by each horizon."""
    if len(curve) < 2:
        return {}
    c = curve.sort_values("hours_out", ascending=False).reset_index(drop=True)
    b0, bf = c.iloc[0]["brier"], c.iloc[-1]["brier"]
    span = b0 - bf
    c = c.copy()
    c["frac_closed"] = (b0 - c["brier"]) / span if span > 0 else float("nan")
    # earliest horizon (largest hours_out) that has closed >= 50% of the gap
    half = c[(c["frac_closed"] >= 0.5) & (c["hours_out"] > 0)]
    half_h = int(half.iloc[0]["hours_out"]) if len(half) else None
    return {"earliest_hours": int(b0 and c.iloc[0]["hours_out"]),
            "brier_earliest": b0, "brier_final": bf,
            "half_gap_horizon_h": half_h, "frac_by_curve": c}


def print_curve(title: str, curve: pd.DataFrame) -> None:
    print(f"\n{title}")
    print(f"  {'hours_out':>9} {'n':>5} {'brier':>7} {'bss':>7} {'ece':>6}")
    for _, r in curve.iterrows():
        tag = "  (final)" if r["hours_out"] == 0 else ""
        print(f"  {int(r['hours_out']):>9} {int(r['n']):>5} {r['brier']:>7.4f} "
              f"{r['bss']:>+7.3f} {r['ece']:>6.3f}{tag}")


def _read_table(path: str) -> pd.DataFrame:
    return pd.read_csv(path) if path.endswith(".csv") else pd.read_parquet(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Q4 timing: how early does the market know.")
    ap.add_argument("--snapshots", default="data/rep_snap.parquet")
    ap.add_argument("--horizons", default=",".join(map(str, DEFAULT_HORIZONS)))
    ap.add_argument("--by-category", action="store_true",
                    help="also split the curve by keyword-derived category")
    ap.add_argument("--out", default=None, help="optional CSV for the overall curve")
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",")]
    df = _read_table(args.snapshots)
    panel = balanced_panel(df, horizons)
    print(f"Balanced panel: {len(panel)}/{len(df)} markets have a price at all "
          f"{len(horizons)} horizons + final.")

    curve = timing_curve(panel, horizons)
    print_curve("Overall timing curve (fixed cohort):", curve)
    he = how_early(curve)
    if he:
        print(f"\n  Brier {he['brier_earliest']:.4f} (@{he['earliest_hours']}h) -> "
              f"{he['brier_final']:.4f} (final).")
        if he["half_gap_horizon_h"]:
            print(f"  Half the earliest->final accuracy gain is already priced "
                  f"{he['half_gap_horizon_h']}h before resolution.")

    if args.by_category:
        panel = panel.copy()
        panel["cat"] = panel["question"].map(classify)
        print("\n--- by keyword-derived category (each on its own balanced panel) ---")
        print(f"  category breakdown: {panel['cat'].value_counts().to_dict()}")
        for cat, g in panel.groupby("cat"):
            if len(g) < 20:
                continue
            c = timing_curve(g, horizons)
            e = c[c["hours_out"] == max(horizons)]
            l = c[c["hours_out"] == 0]
            if len(e) and len(l):
                print(f"  {cat:9s} n={len(g):>4}  Brier {e.iloc[0]['brier']:.4f} "
                      f"(@{max(horizons)}h) -> {l.iloc[0]['brier']:.4f} (final)  "
                      f"bss@24h={c[c['hours_out']==24]['bss'].iloc[0]:+.3f}"
                      if len(c[c['hours_out'] == 24]) else "")

    if args.out and len(curve):
        curve.to_csv(args.out, index=False)
        print(f"\nWrote timing curve -> {args.out}")


if __name__ == "__main__":
    main()
