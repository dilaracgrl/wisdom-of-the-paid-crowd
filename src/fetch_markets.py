"""CLI: pull resolved markets from both venues into one normalized table.

Usage:
    python -m src.fetch_markets --venue both --limit 500 --out data/markets.parquet
    python -m src.fetch_markets --venue kalshi --min-duration-hours 24

Writes parquet (default) or csv based on the output extension.
"""
from __future__ import annotations

import argparse
import os

import pandas as pd

from . import polymarket, kalshi
from .http import RateLimitedSession


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch resolved prediction markets.")
    ap.add_argument("--venue", choices=["polymarket", "kalshi", "both"], default="both")
    ap.add_argument("--limit", type=int, default=None, help="max rows per venue")
    ap.add_argument("--min-duration-hours", type=float, default=12.0,
                    help="Kalshi: drop markets shorter than this (filters micro-markets)")
    ap.add_argument("--representative", action="store_true",
                    help="Polymarket: pull the whole date-range population (not volume-sorted)")
    ap.add_argument("--start", default="2022-01-01T00:00:00Z", help="representative: window start")
    ap.add_argument("--end", default="2025-10-01T00:00:00Z", help="representative: window end")
    ap.add_argument("--min-volume", type=float, default=0.0,
                    help="inclusion threshold: drop markets below this traded volume")
    ap.add_argument("--sample-n", type=int, default=None,
                    help="randomly subsample this many rows per venue after pulling")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data/markets.parquet")
    args = ap.parse_args()

    session = RateLimitedSession(min_interval=0.2)
    rows = []

    if args.venue in ("polymarket", "both"):
        if args.representative:
            print(f"Fetching Polymarket population {args.start[:10]}..{args.end[:10]} "
                  f"(min_volume={args.min_volume})...")
            src = polymarket.fetch_resolved_markets_windowed(
                session, args.start, args.end, min_volume=args.min_volume)
        else:
            print("Fetching Polymarket resolved markets (volume-sorted)...")
            src = polymarket.fetch_resolved_markets(session, limit=args.limit)
        for i, m in enumerate(src, 1):
            rows.append(m.to_dict())
            if i % 1000 == 0:
                print(f"  polymarket: {i}")
        print(f"  polymarket total: {sum(r['venue'] == 'polymarket' for r in rows)}")

    if args.venue in ("kalshi", "both"):
        print("Fetching Kalshi settled markets...")
        n0 = len(rows)
        for i, m in enumerate(
            kalshi.fetch_resolved_markets(
                session, limit=args.limit, min_duration_hours=args.min_duration_hours
            ),
            1,
        ):
            rows.append(m.to_dict())
            if i % 500 == 0:
                print(f"  kalshi: {i}")
        print(f"  kalshi total: {len(rows) - n0}")

    df = pd.DataFrame(rows)

    if args.sample_n and len(df) > args.sample_n:
        # representative random subsample, per venue, reproducible via seed
        df = (df.groupby("venue", group_keys=False)[df.columns.tolist()]
                .apply(lambda g: g.sample(min(len(g), args.sample_n), random_state=args.seed))
                .reset_index(drop=True))
        print(f"randomly subsampled to {len(df)} rows (seed={args.seed})")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    if args.out.endswith(".csv"):
        df.to_csv(args.out, index=False)
    else:
        df.to_parquet(args.out, index=False)
    print(f"\nWrote {len(df)} rows -> {args.out}")
    if not df.empty:
        print(df.groupby("venue")["outcome"].agg(["count", "mean"]).rename(
            columns={"mean": "yes_rate"}))


if __name__ == "__main__":
    main()
