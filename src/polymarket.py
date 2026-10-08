"""Polymarket loaders (Gamma API for markets, CLOB API for price history).

Endpoints (public, no auth):
  markets   : https://gamma-api.polymarket.com/markets?closed=true
  history   : https://clob.polymarket.com/prices-history?market=<clobTokenId>

Notes verified against the live API (2026-10):
  - Resolved binary markets expose outcomePrices == ["1","0"] or ["0","1"].
    Both-zero / ambiguous prices mean voided/unresolved -> skipped.
  - Price history only exists for CLOB-era markets (~2022+). Older AMM-era
    markets return {"history": []}; they still carry a valid outcome.
"""
from __future__ import annotations

import json
from datetime import timedelta as _timedelta

from .http import RateLimitedSession
from .schema import Market

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"


def _parse_binary_outcome(m: dict) -> int | None:
    """Return 1/0 for a clean binary resolution, or None to skip."""
    try:
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices = json.loads(m.get("outcomePrices", "[]"))
    except (json.JSONDecodeError, TypeError):
        return None
    if [o.lower() for o in outcomes] != ["yes", "no"]:
        return None
    if len(prices) != 2:
        return None
    try:
        yes, no = float(prices[0]), float(prices[1])
    except (ValueError, TypeError):
        return None
    # resolved markets settle to exactly 1/0; anything else is unresolved/voided
    if {round(yes), round(no)} != {0, 1}:
        return None
    return int(round(yes))


def fetch_resolved_markets(
    session: RateLimitedSession,
    limit: int | None = None,
    page_size: int = 500,
    order: str | None = "volumeNum",
):
    """Yield normalized Market rows for resolved binary Polymarket markets.

    `limit` caps the total number of *accepted* rows (None = all).
    `order` sorts the feed (default "volumeNum" desc for convenience); pass
    None for the natural API order, which is what a *representative* pull wants
    so the sample isn't selected on volume.
    """
    offset = 0
    accepted = 0
    while True:
        params = {
            "closed": "true",
            "archived": "false",
            "limit": page_size,
            "offset": offset,
        }
        if order:
            params["order"] = order
            params["ascending"] = "false"
        batch = session.get_json(GAMMA, params=params)
        if not batch:
            return
        for m in batch:
            outcome = _parse_binary_outcome(m)
            if outcome is None:
                continue
            try:
                token_ids = json.loads(m.get("clobTokenIds") or "[]")
            except json.JSONDecodeError:
                token_ids = []
            yield Market(
                venue="polymarket",
                market_id=str(m.get("conditionId") or m.get("id")),
                question=m.get("question", ""),
                category=m.get("category") or "uncategorized",
                outcome=outcome,
                open_time=m.get("startDate"),
                close_time=m.get("closedTime") or m.get("endDate"),
                volume=float(m.get("volumeNum") or 0.0),
                liquidity=float(m.get("liquidityNum") or 0.0),
                price_ref={
                    "yes_token": token_ids[0] if token_ids else None,
                    "slug": m.get("slug"),
                },
            )
            accepted += 1
            if limit and accepted >= limit:
                return
        offset += page_size


def _pull_window(session, lo, hi, min_volume, page_size, max_offset, depth):
    """Collect resolved binary markets with endDate in [lo, hi).

    Gamma caps `offset` at ~2000 and `limit` at 100 once a date filter is set,
    so a window with more than ~2000 markets would be silently truncated. We
    detect that (last page still full at max offset) and split the window.
    """
    params_base = {
        "closed": "true",
        "end_date_min": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date_max": hi.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": page_size,
    }
    collected, seen, offset, hit_cap = [], set(), 0, False
    while offset <= max_offset:
        batch = session.get_json(GAMMA, params={**params_base, "offset": offset})
        if not batch:
            break
        for m in batch:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            collected.append(m)
        if len(batch) < page_size:
            break
        if offset == max_offset:
            hit_cap = True
        offset += page_size

    if hit_cap and depth < 6 and (hi - lo) > _timedelta(hours=12):
        mid = lo + (hi - lo) / 2
        yield from _pull_window(session, lo, mid, min_volume, page_size, max_offset, depth + 1)
        yield from _pull_window(session, mid, hi, min_volume, page_size, max_offset, depth + 1)
        return

    for m in collected:
        outcome = _parse_binary_outcome(m)
        if outcome is None:
            continue
        if float(m.get("volumeNum") or 0.0) < min_volume:
            continue
        try:
            token_ids = json.loads(m.get("clobTokenIds") or "[]")
        except json.JSONDecodeError:
            token_ids = []
        yield Market(
            venue="polymarket",
            market_id=str(m.get("conditionId") or m.get("id")),
            question=m.get("question", ""),
            category=m.get("category") or "uncategorized",
            outcome=outcome,
            open_time=m.get("startDate"),
            close_time=m.get("closedTime") or m.get("endDate"),
            volume=float(m.get("volumeNum") or 0.0),
            liquidity=float(m.get("liquidityNum") or 0.0),
            price_ref={"yes_token": token_ids[0] if token_ids else None, "slug": m.get("slug")},
        )


def fetch_resolved_markets_windowed(
    session: RateLimitedSession,
    start: str,
    end: str,
    window_days: int = 30,
    min_volume: float = 0.0,
    page_size: int = 100,
    max_offset: int = 2000,
):
    """Yield resolved binary markets with endDate in [start, end), by date window.

    This is the representative-pull path: it enumerates the whole population in
    a date range (not selected on volume), working around Gamma's offset/limit
    caps. `min_volume` applies an inclusion threshold (a market nobody traded
    has no crowd forecast). Dates are ISO8601 (e.g. "2022-01-01T00:00:00Z").
    """
    from datetime import datetime

    cur = datetime.fromisoformat(start.replace("Z", "+00:00"))
    stop = datetime.fromisoformat(end.replace("Z", "+00:00"))
    while cur < stop:
        w_end = min(cur + _timedelta(days=window_days), stop)
        yield from _pull_window(session, cur, w_end, min_volume, page_size, max_offset, 0)
        cur = w_end


def fetch_price_history(
    session: RateLimitedSession,
    yes_token: str,
    fidelity: int = 1440,
    start_ts: int | None = None,
    end_ts: int | None = None,
):
    """Return [(unix_ts, price), ...] for a market's YES token.

    fidelity is in minutes (1440 = daily, 60 = hourly). Empty list for
    AMM-era (pre-2022) markets.

    Pass start_ts + end_ts for a bounded window (unix seconds). This is the
    reliable way to get fine fidelity: `interval=all` silently returns [] when
    span / fidelity exceeds the server's point cap, whereas an explicit window
    honors the request (tested: ~20k points over 14 days at 1-min fidelity).
    """
    if not yes_token:
        return []
    params = {"market": yes_token, "fidelity": fidelity}
    if start_ts is not None and end_ts is not None:
        params["startTs"], params["endTs"] = start_ts, end_ts
    else:
        params["interval"] = "all"
    data = session.get_json(CLOB_HISTORY, params=params)
    return [(pt["t"], pt["p"]) for pt in data.get("history", [])]
