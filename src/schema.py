"""The shared, venue-neutral market schema.

One row == one resolved binary market. Both Polymarket and Kalshi markets are
normalized into this shape so every downstream analysis (calibration,
favourite-longshot, liquidity, timing, backtest) reads from one table.

`outcome` is the ground truth from the YES side's perspective:
    1 -> YES resolved true,  0 -> NO resolved true.

`price_ref` carries whatever the venue's price-history loader needs to fetch
the price path for this market later (kept opaque on purpose).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any


@dataclass
class Market:
    venue: str                 # "polymarket" | "kalshi"
    market_id: str             # stable id within the venue
    question: str              # human-readable question / title
    category: str              # normalized-ish category (venue-provided)
    outcome: int               # 1 if YES resolved true, else 0
    open_time: str | None      # ISO8601, when trading opened (may be None on PM)
    close_time: str | None     # ISO8601, resolution / close time
    volume: float              # total traded volume (USD-ish, venue units)
    liquidity: float           # PM: liquidityNum; Kalshi: open interest
    price_ref: dict[str, Any] = field(default_factory=dict)  # for history loader

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # flatten price_ref into dotted columns for tabular output
        ref = d.pop("price_ref") or {}
        for k, v in ref.items():
            d[f"ref_{k}"] = v
        return d


COLUMNS = [
    "venue", "market_id", "question", "category", "outcome",
    "open_time", "close_time", "volume", "liquidity",
]
