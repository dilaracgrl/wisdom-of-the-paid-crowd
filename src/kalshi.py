"""Kalshi loaders (public trade-api v2, no auth for read endpoints).

Endpoints:
  markets      : /trade-api/v2/markets?status=settled
  candlesticks : /trade-api/v2/series/{series}/markets/{ticker}/candlesticks

Notes verified against the live API (2026-10):
  - `result` is "yes"/"no" for settled binary markets -> clean ground truth.
  - The settled feed is dominated by 15-minute crypto micro-markets
    (KXBTC15M, KXMVECROSSCATEGORY, ...). We filter by a minimum lifespan so
    those don't swamp the dataset; tune `min_duration_hours` as needed.
  - Candlesticks cap at 5000 points per request, so the history loader chunks
    the time range. period_interval is in minutes and must be 1, 60, or 1440.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .http import RateLimitedSession
from .schema import Market

BASE = "https://api.elections.kalshi.com/trade-api/v2"
MARKETS = f"{BASE}/markets"
MAX_CANDLES = 5000


def _iso_to_ts(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _duration_hours(m: dict) -> float:
    o, c = _iso_to_ts(m.get("open_time")), _iso_to_ts(m.get("close_time"))
    if o is None or c is None:
        return 0.0
    return (c - o) / 3600.0


def fetch_resolved_markets(
    session: RateLimitedSession,
    limit: int | None = None,
    page_size: int = 1000,
    min_duration_hours: float = 12.0,
):
    """Yield normalized Market rows for settled binary Kalshi markets.

    Markets shorter than `min_duration_hours` are skipped (filters out the
    high-frequency crypto micro-markets). `limit` caps accepted rows.
    """
    cursor = None
    accepted = 0
    while True:
        params = {"status": "settled", "limit": page_size}
        if cursor:
            params["cursor"] = cursor
        data = session.get_json(MARKETS, params=params)
        markets = data.get("markets", [])
        if not markets:
            return
        for m in markets:
            result = m.get("result")
            if result not in ("yes", "no"):
                continue
            if m.get("market_type") != "binary":
                continue
            if _duration_hours(m) < min_duration_hours:
                continue
            ticker = m["ticker"]
            series = ticker.split("-")[0]
            yield Market(
                venue="kalshi",
                market_id=ticker,
                question=m.get("title", ""),
                category=series,  # series prefix is the natural category key
                outcome=1 if result == "yes" else 0,
                open_time=m.get("open_time"),
                close_time=m.get("close_time"),
                volume=float(m.get("volume_fp") or 0.0),
                liquidity=float(m.get("open_interest_fp") or 0.0),
                price_ref={"series": series, "ticker": ticker},
            )
            accepted += 1
            if limit and accepted >= limit:
                return
        cursor = data.get("cursor")
        if not cursor:
            return


def fetch_price_history(
    session: RateLimitedSession,
    series: str,
    ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 1440,
):
    """Return [(end_period_ts, close_price), ...], chunked under the 5000 cap.

    period_interval is in minutes (1, 60, or 1440). close_price is the YES
    mid/close in dollars (0..1).
    """
    if period_interval not in (1, 60, 1440):
        raise ValueError("period_interval must be 1, 60, or 1440 minutes")
    url = f"{BASE}/series/{series}/markets/{ticker}/candlesticks"
    span = period_interval * 60 * MAX_CANDLES  # max seconds per request
    out: list[tuple[int, float]] = []
    lo = start_ts
    while lo < end_ts:
        hi = min(lo + span, end_ts)
        data = session.get_json(
            url,
            params={"start_ts": lo, "end_ts": hi, "period_interval": period_interval},
        )
        for c in data.get("candlesticks", []):
            close = (c.get("price") or {}).get("close_dollars")
            if close is not None:
                out.append((c["end_period_ts"], float(close)))
        lo = hi
    return out
