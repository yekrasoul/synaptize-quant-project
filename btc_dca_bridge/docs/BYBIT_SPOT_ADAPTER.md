# Bybit BTCUSDT Spot adapter

Phase 3.3 adds a read-only public adapter at
`btc_dca_bridge.market_data.bybit.BybitSpotAdapter`. It performs no allocation,
sentiment, notification, scheduling, credential, or order-execution work.

## Public endpoints

The adapter uses `https://api.bybit.com` and only these V5 endpoints:

| Purpose | Path | Parameters |
|---|---|---|
| Last traded Spot price | `GET /v5/market/tickers` | `category=spot`, `symbol=BTCUSDT` |
| Spot hourly candles | `GET /v5/market/kline` | `category=spot`, `symbol=BTCUSDT`, `interval=60`, explicit millisecond `start` and `end`, `limit=200` by default |

Both are public endpoints and require no API key. The adapter never calls a mark
price, index price, derivatives, order, account, or alternate-exchange endpoint.
It requires the response `result.category` to equal `spot` and the returned
symbol to equal `BTCUSDT`; a symbol alone is not accepted as proof of Spot
identity. Derivative mark/index fields in a purported Spot ticker are rejected.

## Transport and failures

The standard-library HTTPS transport uses a 3-second connect timeout and a
10-second read timeout. It makes at most three attempts with controlled
exponential delays of 0.2 and 0.4 seconds. Only connection failures, timeouts,
HTTP 429, and HTTP 5xx are retried. HTTP 403 is not retried because Bybit uses it
for forbidden/region cases and may use it for an IP frequency ban; HTTP 429 and
logical `retCode=10006` map to `RATE_LIMITED`.

Malformed JSON/envelopes/OHLC map to `INVALID_RESPONSE`, symbol conflicts to
`SOURCE_MISMATCH`, non-Spot identity to `INVALID_MARKET_IDENTITY`, exhausted
transport/API failures to `SOURCE_UNAVAILABLE`, and gaps or pagination exhaustion
to `INSUFFICIENT_HISTORY`. `DATA_STALE` exists in the domain taxonomy, but this
adapter does not invent a freshness threshold; Phase 3.4 owns that policy.

## Normalization and time semantics

Prices, OHLC, and volume use `Decimal`. Datetimes are timezone-aware UTC values.
`Ticker` records retrieval time, Bybit's envelope response time, and explicit
source/market identity. Its `observed_at_utc` is `None`: the ticker endpoint does
not expose a reliable last-trade observation timestamp, and the envelope time is
not misrepresented as one.

Each `Candle` contains open time, OHLC, volume, `source=bybit_api`,
`exchange=Bybit`, `market=spot`, and `symbol=BTCUSDT`. `CandleHistory` adds the
requested and covered bounds, retrieval time, hourly interval, request count,
duplicate count, and `complete=true` proof.

Bybit returns klines newest first. The adapter paginates backward with a bounded
page count, advances the `end` cursor below the oldest received timestamp,
deduplicates by candle open time, sorts oldest first, and verifies that every
hourly open overlapping the requested interval is present. Empty or gapped
history is never returned as complete.

## Exact 168-hour snapshot construction (Phase 3.4)

`build_market_snapshot` converts a normalized ticker and candle history into the
schema-valid V1 `MarketSnapshot`. Its market observation time is the ticker's
source-provided observation time when one exists, otherwise the local time at
which the latest price snapshot was retrieved (the current Bybit ticker has no
trade timestamp):

```text
window_end_utc   = ticker.observed_at_utc or ticker.retrieved_at_utc
window_start_utc = window_end_utc - 168 hours
```

The exact mathematical observation window is closed,
`[window_start_utc, window_end_utc]`. Bybit labels a kline by its start time;
every candle is modelled as a half-open aggregate interval
`[open_time_utc, open_time_utc + interval)`. The ticker is the point observation
at the inclusive end.

For an hour-aligned end, 168 fully-contained 60-minute Spot candles are used.
For a minute-aligned but non-hour-aligned end, the adapter uses the documented
one-minute (`interval=1`) Bybit Spot kline resolution for both partial hours:

1. minute candles from `window_start` up to the next hour;
2. complete 60-minute candles in the interior;
3. minute candles from the final hour up to (but excluding) `window_end`;
4. the ticker point at `window_end`.

Only candles wholly inside their assigned span contribute a high. A candle
ending at the start and a candle opening at the end never contributes, so no
known pre-window or post-window observation influences the result. Every span
must be complete, ordered, deduplicated, UTC-aligned, and gap-free.

### Resolution limitation and fail-closed behavior

The wall-clock window remains exactly 168 hours, but the finest public kline
observation used here is one minute. The emitted `market_data_metadata` records
`observation_resolution_seconds` (`60` when minute boundaries are used, `3600`
for a wholly hourly path) and `trade_level_exact: false`. It never claims
trade-level precision.

A non-zero second or microsecond boundary fails with `INSUFFICIENT_HISTORY`:
using a whole minute high would include unknown trades before the start or after
the end. Bybit's public recent-trades endpoint is limited to recent Spot trades
and cannot prove the historical start-boundary sub-minute span 168 hours back.
No boundary is rounded, interpolated, or approximated. Missing minute boundary
history, a boundary gap, wrong interval, or any identity mismatch also fails
closed.

Construction also fails closed for stale retrievals, gaps, duplicates,
out-of-order candles, false or insufficient coverage claims, contradictory OHLC,
non-UTC timestamps, and any source/exchange/market/symbol mismatch. The default
freshness limit is five minutes and may be tightened explicitly by the caller.
No summary high, percentage performance, derivative field, alternate exchange,
or calendar-day shortcut is accepted.

## Phase 3.5 source selection

Production source order is strictly:

```text
Bybit direct Spot -> TradingView exact BYBIT:BTCUSDT Spot -> unavailable
```

`FallbackMarketDataProvider` invokes complete snapshot sources, not individual
price/history methods. The direct path must retrieve and validate its own ticker,
hourly history, and any minute boundary history before it can return. Only after
that complete path raises an availability-category error is the complete
TradingView path called. Values retained in local variables from a failed path
are never passed to the next path, so a Bybit price cannot be combined with
TradingView history (or vice versa).

Fallback-triggering categories are exactly `SOURCE_UNAVAILABLE`, `RATE_LIMITED`,
`INVALID_RESPONSE`, and `INSUFFICIENT_HISTORY`. `INVALID_MARKET_IDENTITY`,
`SOURCE_MISMATCH`, `DATA_STALE`, `INVALID_WINDOW`, and
`CONTRADICTORY_PRICE_DATA` are hard failures and do not silently select another
source. When both availability paths fail, `AllSourcesUnavailableError` exposes
the primary source/category, confirms the fallback attempt, and retains the
fallback source/category without embedding upstream payloads.

See `TRADINGVIEW_FALLBACK.md` for exact symbol, resolution, protocol, freshness,
and operational details. This fallback changes no V1 allocation rule.
