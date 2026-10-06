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
transport/API failures to `DATA_UNAVAILABLE`, and gaps or pagination exhaustion
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

## Reserved for Phase 3.4

This phase does not calculate the rolling 168-hour high, choose final
window-boundary inclusion semantics, combine ticker and candles into a
`MarketSnapshot`, or evaluate freshness. Phase 3.4 can prove same-source input by
comparing the explicit `source`, exchange, market, and symbol fields on `Ticker`
and `CandleHistory`.
