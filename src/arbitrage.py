"""Q5 — Cross-venue arbitrage: do the same events diverge across venues?

Match an event that trades on BOTH Polymarket and Kalshi, align the two price
paths on a common clock, and test whether a *locked* position was ever available
after fees.

The matching problem (the hard 80%)
-----------------------------------
There is no shared event ID across venues, so matches are curated by hand into
`REGISTRY` below: each entry pins the Polymarket YES token and the Kalshi
series/ticker for one real event, plus the polarity (does Polymarket-YES mean the
same thing as Kalshi-YES?). Two live constraints learned from the APIs:
  * Polymarket keeps deep history; Kalshi's public API prunes older settled
    markets, so matchable pairs are limited to recent events.
  * Polymarket's windowed price call is flaky for some tokens; we fall back to
    interval=all.

The arbitrage test
------------------
For one event with $1 notional on each side, holding YES on one venue and NO on
the other guarantees exactly $1 at resolution (one leg pays, the other doesn't).
So a locked position costs:  price_yes(venueA) + (1 - price_yes(venueB)).
If that total is below $1 after fees, the difference is risk-free profit. Net edge
at any instant is therefore  |pm_yes - kalshi_yes| - fees, buying YES on whichever
venue is cheaper. Because capital is tied up until resolution, the headline number
is the *annualized* return, not the raw edge.

Fees: Kalshi charges ceil(0.07 * price * (1-price)) per contract (its general
formula, symmetric in YES/NO); Polymarket CLOB has no trading fee (gas aside).
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

import pandas as pd

from . import polymarket, kalshi
from .http import RateLimitedSession


@dataclass
class MatchedEvent:
    label: str
    pm_yes_token: str
    kalshi_series: str
    kalshi_ticker: str
    pm_yes_is_kalshi_yes: bool = True   # polarity
    resolution_ts: int | None = None    # unix seconds, for annualizing


# Curated matches (grown by hand; see module docstring). Each is a confirmed
# same-event pair with overlapping price history on both venues.
REGISTRY: list[MatchedEvent] = [
    MatchedEvent(
        label="Fed hikes 25bps @ Sept 2026 FOMC (Polymarket vs Kalshi)",
        pm_yes_token="63842529068710005716169325380315470359047749786610778647370693404952498013178",
        kalshi_series="KXFEDDECISION",
        kalshi_ticker="KXFEDDECISION-26SEP-H25",
        pm_yes_is_kalshi_yes=True,  # PM "increase 25bps" YES == Kalshi "Hike 25bps" YES
        resolution_ts=1789581540,   # 2026-09-16T17:59:00Z
    ),
]


def kalshi_fee(price: float, contracts: int = 1) -> float:
    """Kalshi general trading fee: ceil(0.07 * C * P * (1-P)) dollars."""
    p = min(max(price, 0.0), 1.0)
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100


def _pm_history(session, token, start_ts, end_ts, fidelity=60):
    """Polymarket YES price path, with interval=all fallback.

    The windowed call rejects coarse fidelity (1440) and is flaky for some
    tokens, so any failure or empty result falls back to interval=all at 12h
    fidelity (reliable, fine enough once resampled).
    """
    try:
        h = polymarket.fetch_price_history(session, token, fidelity=fidelity,
                                           start_ts=start_ts, end_ts=end_ts)
    except Exception:
        h = []
    if not h:
        h = polymarket.fetch_price_history(session, token, fidelity=720)
    return h


def align_prices(session: RateLimitedSession, ev: MatchedEvent,
                 freq: str = "1h", fidelity_min: int = 60) -> pd.DataFrame:
    """Return a DataFrame indexed by time with pm_yes and kalshi_yes aligned."""
    meta = kalshi.fetch_market(session, ev.kalshi_ticker)
    k_start = kalshi._iso_to_ts(meta.get("open_time")) or 0
    k_end = kalshi._iso_to_ts(meta.get("close_time")) or 4_000_000_000
    k_raw = kalshi.fetch_price_history(session, ev.kalshi_series, ev.kalshi_ticker,
                                       start_ts=k_start, end_ts=k_end,
                                       period_interval=fidelity_min)
    if not k_raw:
        return pd.DataFrame()
    k_ts = [t for t, _ in k_raw]
    pm_raw = _pm_history(session, ev.pm_yes_token, min(k_ts), max(k_ts), fidelity_min)
    if not pm_raw:
        return pd.DataFrame()

    def to_series(raw, name):
        s = pd.Series({pd.to_datetime(t, unit="s", utc=True): float(p) for t, p in raw},
                      name=name).sort_index()
        return s.resample(freq).last().ffill()

    pm = to_series(pm_raw, "pm_yes")
    k = to_series(k_raw, "kalshi_yes")
    if not ev.pm_yes_is_kalshi_yes:
        k = 1.0 - k
    df = pd.concat([pm, k], axis=1).dropna()
    return df


def arb_scan(df: pd.DataFrame, ev: MatchedEvent) -> dict:
    """Per-timestamp net edge and the best locked opportunity."""
    if df.empty:
        return {"n": 0}
    d = df.copy()
    d["divergence"] = (d["pm_yes"] - d["kalshi_yes"]).abs()
    # Kalshi leg fee depends on the price transacted; use the cheaper-YES price
    # as the Kalshi transaction price (symmetric formula, so side doesn't matter).
    d["fee"] = d["kalshi_yes"].map(kalshi_fee)
    d["net_edge"] = d["divergence"] - d["fee"]
    d["cost"] = d[["pm_yes", "kalshi_yes"]].min(axis=1) + (1 - d[["pm_yes", "kalshi_yes"]].max(axis=1)) + d["fee"]

    arb = d[d["net_edge"] > 0]
    out = {
        "n": len(d),
        "overlap_start": str(d.index[0]), "overlap_end": str(d.index[-1]),
        "corr": float(d["pm_yes"].corr(d["kalshi_yes"])),
        "mean_divergence": float(d["divergence"].mean()),
        "max_divergence": float(d["divergence"].max()),
        "arb_fraction": len(arb) / len(d),
        "max_net_edge": float(d["net_edge"].max()),
    }
    if len(arb):
        best = arb.loc[arb["net_edge"].idxmax()]
        out["best_time"] = str(best.name)
        out["best_net_edge"] = float(best["net_edge"])
        out["best_cost"] = float(best["cost"])
        if ev.resolution_ts:
            days = max((ev.resolution_ts - best.name.timestamp()) / 86400, 0.5)
            ann = (best["net_edge"] / best["cost"]) * (365.0 / days)
            out["best_days_to_resolution"] = days
            out["best_annualized_return"] = float(ann)
    return out


def print_report(ev: MatchedEvent, res: dict) -> None:
    print(f"\n=== {ev.label} ===")
    if res.get("n", 0) == 0:
        print("  no overlapping price history (one side missing / pruned)")
        return
    print(f"  overlap: {res['overlap_start'][:16]} -> {res['overlap_end'][:16]} "
          f"({res['n']} aligned points)")
    print(f"  price correlation:  {res['corr']:+.3f}")
    print(f"  divergence (|PM-Kalshi|):  mean {res['mean_divergence']:.3f}  "
          f"max {res['max_divergence']:.3f}")
    print(f"  max net edge after fees:   {res['max_net_edge']:+.3f} / contract")
    print(f"  time with a locked arb:    {res['arb_fraction']*100:.1f}%")
    if "best_net_edge" in res:
        print(f"  best opportunity: {res['best_net_edge']:.3f}/contract at "
              f"{res['best_time'][:16]} (cost {res['best_cost']:.3f})")
        if "best_annualized_return" in res:
            print(f"    -> {res['best_days_to_resolution']:.0f} days to resolution, "
                  f"annualized {res['best_annualized_return']*100:.0f}%")
    else:
        print("  no risk-free arbitrage after fees (market efficient across venues)")


def run(events: list[MatchedEvent], session: RateLimitedSession | None = None) -> None:
    session = session or RateLimitedSession()
    for ev in events:
        df = align_prices(session, ev, freq="1D", fidelity_min=1440)
        print_report(ev, arb_scan(df, ev))


def main() -> None:
    ap = argparse.ArgumentParser(description="Q5 cross-venue arbitrage.")
    ap.add_argument("--list", action="store_true", help="list registry events")
    args = ap.parse_args()
    if args.list or not REGISTRY:
        print(f"{len(REGISTRY)} matched events in REGISTRY")
        for ev in REGISTRY:
            print(f"  - {ev.label}")
        if not REGISTRY:
            print("  (populate REGISTRY with confirmed cross-venue matches)")
        return
    run(REGISTRY)


if __name__ == "__main__":
    main()
