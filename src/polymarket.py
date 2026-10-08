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
):
    """Yield normalized Market rows for resolved binary Polymarket markets.

    `limit` caps the total number of *accepted* rows (None = all).
    """
    offset = 0
    accepted = 0
    while True:
        batch = session.get_json(
            GAMMA,
            params={
                "closed": "true",
                "archived": "false",
                "limit": page_size,
                "offset": offset,
                "order": "volumeNum",
                "ascending": "false",
            },
        )
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


def fetch_price_history(session: RateLimitedSession, yes_token: str, fidelity: int = 1440):
    """Return [(unix_ts, price), ...] for a market's YES token.

    fidelity is in minutes (1440 = daily). Empty list for AMM-era markets.
    """
    if not yes_token:
        return []
    data = session.get_json(
        CLOB_HISTORY,
        params={"market": yes_token, "interval": "all", "fidelity": fidelity},
    )
    return [(pt["t"], pt["p"]) for pt in data.get("history", [])]
