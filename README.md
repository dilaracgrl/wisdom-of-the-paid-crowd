# How Smart Is the Crowd? Testing Efficiency in Prediction Markets

*Working title — a.k.a. "Wisdom of the Paid Crowd."*

A prediction market's price is a forecast: a contract trading at 70¢ is the crowd
saying "70% likely." A good forecaster is **calibrated** — on all the days they said
70%, it happened ~70% of the time. This project tests whether the paid crowd is a good
forecaster, where it is biased, when you can trust it, and whether the biases are worth
money after fees.

Data comes from the **public** APIs of two venues — [Polymarket](https://polymarket.com)
and [Kalshi](https://kalshi.com) — with no scraping, no auth, and no paid tier.

## Research questions

| # | Question | Metric | Feasibility |
|---|----------|--------|-------------|
| 1 | **Calibration** (core): do prices match outcomes? | Calibration curve + Brier score | 🟢 Go |
| 2 | **Favourite–longshot bias**: do 5¢ contracts win <5%? | Win rate by price bucket | 🟢 Go |
| 3 | **Liquidity vs accuracy**: are thin markets worse? | Brier vs volume / open interest / spread | 🟢 Go |
| 4 | **How early does it know?**: accuracy vs time-to-resolution | Brier at horizon, by category | 🟡 Post-2022 only |
| 5 | **Cross-venue arbitrage**: do matched events diverge? | Price gap net of fees | 🟠 Curated set only |
| 6 | **Strategy backtest**: is betting against longshots profitable? | PnL after fees + spread + fills | 🟢 Go |

## Feasibility findings (verified against the live APIs, Oct 2026)

Everything needed for questions 1–4 and 6 is obtainable from one normalized dataset.
Cross-venue matching (Q5) has no shared event ID and is only realistic for a **curated**
set of matched events (elections, macro prints, major crypto levels).

**What's available, no auth:**

| Piece | Polymarket | Kalshi |
|-------|-----------|--------|
| Resolved markets | Gamma `/markets?closed=true` | `/markets?status=settled` |
| Ground-truth outcome | `outcomePrices` → `["1","0"]` | `result` → `"yes"/"no"` |
| Price history | CLOB `/prices-history` per `clobTokenId` | `/candlesticks` per series+ticker |
| Bid/ask (spread, fills) | live order book | in candlesticks (`yes_bid`/`yes_ask`) |
| Volume / liquidity | `volumeNum`, `liquidityNum` | `volume_fp`, `open_interest_fp` |

**Constraints that shape the design:**

1. **Polymarket price history starts ~2022 (CLOB era).** Pre-2022 AMM markets return
   `{"history": []}` — they keep a valid *outcome* but have no price path. Q4 ("how
   early") is therefore scoped to post-2022 on Polymarket; Kalshi is unaffected.
2. **Kalshi candlesticks cap at 5,000 points/request.** The loader chunks the time
   range. `period_interval` is in minutes and must be `1`, `60`, or `1440`.
3. **Kalshi's settled feed is dominated by 15-minute crypto micro-markets**
   (`KXBTC15M`, `KXMVECROSSCATEGORY`, …). Pulling "all settled" naively swamps every
   statistic, so the loader filters by a minimum market lifespan
   (`--min-duration-hours`, default 12). This is an explicit **selection decision** —
   document whatever threshold the final analysis uses.
4. **Rate limits are not published.** The shared HTTP client self-throttles (default
   0.2s between calls) and backs off on 429/5xx. Tune `min_interval` for bulk pulls.

### Recommended scope

Post-2022, binary markets, filtered to substantive series on both venues, with the
forecast **price snapshot at a fixed horizon before resolution** (e.g. 24h out). That
one dataset powers Q1–Q4 and Q6; Q5 runs off a hand-matched event list on top.

## Methodology notes (decisions to pin down before analysis)

- **Which price is the "forecast"?** Calibration depends on *when* you snapshot. Default
  plan: price at a fixed horizon before resolution (feeds Q4 directly). Alternatives —
  last trade, closing price, time-weighted — give different curves; pick one and stay
  consistent.
- **Selection / survivorship.** Use the API, not the UI (Polymarket hides old markets).
  Decide inclusion rules up front: binary only, exclude day-1 resolutions, minimum trade
  count / volume.
- **Cross-venue matching = the hard 80%.** Matching is semantic (same event, same
  resolution date, compatible resolution criteria). "Risk-free arb" is really a
  *convergence trade* — capital is locked until resolution, so returns are annualized
  **after** Polymarket gas + Kalshi fees.
- **Backtest realism.** Don't assume fills at the quoted longshot price; thin books
  rarely allow it. Model spread, fees, and fill feasibility from the bid/ask data.

## Layout

```
src/
  http.py           # rate-limited session with retry/backoff
  schema.py         # the shared venue-neutral Market schema
  polymarket.py     # Gamma markets + CLOB price history
  kalshi.py         # settled markets + candlestick price history
  fetch_markets.py  # CLI -> one normalized markets table
data/               # outputs (gitignored)
```

### The shared schema

One row per resolved binary market, both venues normalized into the same shape.
`outcome` is ground truth from the YES side (`1` = YES resolved true). `price_ref`
columns carry what each venue's history loader needs.

| Column | Meaning |
|--------|---------|
| `venue` | `polymarket` / `kalshi` |
| `market_id` | stable id (PM `conditionId`, Kalshi `ticker`) |
| `question` | market question / title |
| `category` | PM category, or Kalshi series prefix |
| `outcome` | `1` if YES resolved true, else `0` |
| `open_time`, `close_time` | ISO8601 |
| `volume`, `liquidity` | traded volume; PM liquidity / Kalshi open interest |

## Quickstart

```bash
pip install -r requirements.txt

# Pull a small sample from both venues (top markets by volume)
python -m src.fetch_markets --venue both --limit 500 --out data/markets.parquet

# Kalshi only, longer-lived markets, to CSV
python -m src.fetch_markets --venue kalshi --min-duration-hours 24 --out data/kalshi.csv
```

Price history, per market, is fetched on demand:

```python
from src.http import RateLimitedSession
from src import polymarket, kalshi

s = RateLimitedSession()
pm_path = polymarket.fetch_price_history(s, yes_token="<clobTokenId>", fidelity=1440)
k_path  = kalshi.fetch_price_history(s, "KXFEDDECISION", "KXFEDDECISION-26SEP-H25",
                                     start_ts=..., end_ts=..., period_interval=1440)
```

## Status

- [x] End-to-end feasibility verified against both live APIs
- [x] Shared schema + resolved-market loaders (both venues)
- [x] Price-history loaders (both venues)
- [ ] Horizon price-snapshot builder (the analysis dataset)
- [ ] Q1 calibration + Brier
- [ ] Q2–Q4 bias / liquidity / timing
- [ ] Q5 curated cross-venue matching
- [ ] Q6 backtest with realistic fills

*Data is for research only; this is not financial advice.*
