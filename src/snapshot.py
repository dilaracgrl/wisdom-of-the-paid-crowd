"""Horizon price-snapshot builder — the analysis dataset.

For each resolved binary market we record the YES price *as it stood* at a set
of fixed horizons before resolution (e.g. 1 week, 3 days, 1 day, 1 hour out).
"The price as it stood at time t" = the last observed price at or before t, i.e.
what a trader watching the market would have seen then. No look-ahead.

One row per market; one `price_h{N}` column per horizon (N = hours before close),
plus `price_last` (final pre-resolution price). This single table feeds:
  Q1 calibration  -> price_h24 vs outcome
  Q2 longshot     -> win rate by price bucket
  Q3 liquidity    -> error vs volume / liquidity
  Q4 timing       -> how price_h{N} -> outcome accuracy sharpens as N -> 0

Input is the normalized markets table written by `fetch_markets` (it carries the
`ref_*` columns each venue's history loader needs).
"""
from __future__ import annotations

import argparse
import os
from bisect import bisect_right

import pandas as pd

from . import polymarket, kalshi
from .http import RateLimitedSession
from .kalshi import _iso_to_ts

# Default horizons in hours before resolution: 1wk, 3d, 1d, 12h, 6h, 1h.
DEFAULT_HORIZONS = (168, 72, 24, 12, 6, 1)


def _price_as_of(history: list[tuple[int, float]], target_ts: int):
    """Last price at or before target_ts. None if no observation that early.

    `history` must be sorted ascending by timestamp.
    """
    ts = [h[0] for h in history]
    i = bisect_right(ts, target_ts)
    if i == 0:
        return None
    return history[i - 1][1]


def _fetch_history(
    session: RateLimitedSession, row: dict, fidelity_min: int,
    start_ts: int, end_ts: int,
):
    """Fetch the [start_ts, end_ts] price window; returns sorted [(ts, price)].

    Bounding the window to just what the horizons need keeps every request
    small enough for fine fidelity and under each venue's point cap.
    """
    venue = row["venue"]
    if venue == "polymarket":
        hist = polymarket.fetch_price_history(
            session, row.get("ref_yes_token"), fidelity=fidelity_min,
            start_ts=start_ts, end_ts=end_ts,
        )
    elif venue == "kalshi":
        open_ts = _iso_to_ts(row.get("open_time"))
        start = start_ts if open_ts is None else max(start_ts, open_ts)
        hist = kalshi.fetch_price_history(
            session, row["ref_series"], row["ref_ticker"], start, end_ts,
            period_interval=fidelity_min,
        )
    else:
        return []
    return sorted(hist, key=lambda p: p[0])


def snapshot_market(
    session: RateLimitedSession,
    row: dict,
    horizons=DEFAULT_HORIZONS,
    fidelity_min: int = 60,
) -> dict:
    """Return one snapshot row for a single market (price at each horizon)."""
    close_ts = _iso_to_ts(row.get("close_time"))
    out = {
        "venue": row["venue"],
        "market_id": row["market_id"],
        "question": row.get("question", ""),
        "category": row.get("category", ""),
        "outcome": int(row["outcome"]),
        "volume": float(row.get("volume") or 0.0),
        "liquidity": float(row.get("liquidity") or 0.0),
        "close_time": row.get("close_time"),
        "n_price_points": 0,
        "price_last": None,
    }
    for h in horizons:
        out[f"price_h{h}"] = None
    if close_ts is None:
        return out

    # Fetch only the window the horizons need: [close - max_horizon - pad, close].
    pad = fidelity_min * 60 * 3  # a few bars of lead so the earliest horizon has a prior point
    window_start = close_ts - max(horizons) * 3600 - pad
    history = _fetch_history(session, row, fidelity_min, window_start, close_ts)
    out["n_price_points"] = len(history)
    if not history:
        return out

    out["price_last"] = _price_as_of(history, close_ts)
    for h in horizons:
        out[f"price_h{h}"] = _price_as_of(history, close_ts - h * 3600)
    return out


def build_snapshots(
    markets: pd.DataFrame,
    horizons=DEFAULT_HORIZONS,
    fidelity_min: int = 60,
    session: RateLimitedSession | None = None,
    progress_every: int = 100,
) -> pd.DataFrame:
    """Build the snapshot table for every market in `markets`."""
    session = session or RateLimitedSession()
    rows = []
    total = len(markets)
    for i, (_, r) in enumerate(markets.iterrows(), 1):
        rows.append(snapshot_market(session, r.to_dict(), horizons, fidelity_min))
        if progress_every and i % progress_every == 0:
            print(f"  snapshots: {i}/{total}")
    return pd.DataFrame(rows)


def _read_table(path: str) -> pd.DataFrame:
    return pd.read_csv(path) if path.endswith(".csv") else pd.read_parquet(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build horizon price snapshots.")
    ap.add_argument("--markets", default="data/markets.parquet",
                    help="input markets table from fetch_markets")
    ap.add_argument("--out", default="data/snapshots.parquet")
    ap.add_argument("--limit", type=int, default=None, help="cap markets processed")
    ap.add_argument("--horizons", default=",".join(map(str, DEFAULT_HORIZONS)),
                    help="comma-separated hours-before-resolution")
    ap.add_argument("--fidelity-min", type=int, default=60, choices=[1, 60, 1440],
                    help="price granularity in minutes (Kalshi requires 1/60/1440)")
    ap.add_argument("--venue", choices=["polymarket", "kalshi", "both"], default="both")
    args = ap.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(","))
    df = _read_table(args.markets)
    if args.venue != "both":
        df = df[df["venue"] == args.venue]
    if args.limit:
        df = df.head(args.limit)
    print(f"Building snapshots for {len(df)} markets "
          f"(horizons={horizons}h, fidelity={args.fidelity_min}min)...")

    snaps = build_snapshots(df, horizons=horizons, fidelity_min=args.fidelity_min)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    if args.out.endswith(".csv"):
        snaps.to_csv(args.out, index=False)
    else:
        snaps.to_parquet(args.out, index=False)
    print(f"\nWrote {len(snaps)} snapshot rows -> {args.out}")

    # coverage report: how many markets have a usable price at each horizon
    covered = snaps[snaps["n_price_points"] > 0]
    print(f"with price history: {len(covered)}/{len(snaps)}")
    for h in horizons:
        col = f"price_h{h}"
        n = snaps[col].notna().sum()
        print(f"  h{h:>4}: {n:>5} markets have a snapshot")


if __name__ == "__main__":
    main()
