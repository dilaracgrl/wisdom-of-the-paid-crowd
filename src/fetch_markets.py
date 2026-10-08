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
    ap.add_argument("--out", default="data/markets.parquet")
    args = ap.parse_args()

    session = RateLimitedSession(min_interval=0.2)
    rows = []

    if args.venue in ("polymarket", "both"):
        print("Fetching Polymarket resolved markets...")
        for i, m in enumerate(polymarket.fetch_resolved_markets(session, limit=args.limit), 1):
            rows.append(m.to_dict())
            if i % 500 == 0:
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
