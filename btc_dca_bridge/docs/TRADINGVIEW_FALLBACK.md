# TradingView exact Bybit Spot fallback

Phase 3.5 adds one secondary, read-only market-data path. The source order is:

```text
Bybit direct Spot -> TradingView exact BYBIT:BTCUSDT Spot -> unavailable
```

There is no Binance, KuCoin, generic BTC price, derivative, mark/index price,
sentiment, notification, scheduling, credential, order, or execution path.

## Exact identity

The TradingView adapter sends only `BYBIT:BTCUSDT` and requires resolved metadata
with all of the following:

- `pro_name=BYBIT:BTCUSDT`
- `exchange=Bybit`
- `listed_exchange=BYBIT`
- `type=spot`
- no perpetual, future, or swap marker in the resolved identity metadata

`BYBIT:BTCUSDT.P`, bare `BTCUSDT`, another exchange, another pair, and metadata
with a derivative identity fail closed. A derivative name is never stripped or
normalized into Spot.

## Transport and completeness

`WebSocketTradingViewTransport` uses the TradingView chart WebSocket at
`wss://data.tradingview.com/socket.io/websocket`. It isolates the length-framed
chart protocol (`resolve_symbol`, `create_series`, `timescale_update`, and
`series_completed`) from normalization. The connection has a 15-second timeout,
requests 180 hourly observations (bounded to 169–500), uses an anonymous
read-only token, and closes after the series completes. It uses
`websocket-client`, locked by `uv`; it does not use a browser or scrape HTML.

The adapter discards an in-progress hourly bar, requires at least 168 completed,
gap-free, hour-aligned bars, and selects the newest 168. The most recent completed
bar close is the recent price point at that bar's exclusive close time. The same
168 bars independently supply the rolling high. Thus both values come from one
resolved exact-symbol response, and a price-only or history-only response cannot
produce a snapshot.

## Exact rolling window and resolution

The adapter does not implement rolling-window mathematics. It normalizes to the
same `Ticker`, `Candle`, and `CandleHistory` values consumed by Phase 3.4's
`build_market_snapshot`:

```text
window_end   = latest completed hourly bar close
window_start = window_end - 168 hours
```

All 168 half-open hourly bars exactly cover `[window_start, window_end)`, and the
bar close supplies the inclusive end point. The snapshot reports
`observation_resolution_seconds=3600`, `trade_level_exact=false`, complete
coverage, and same-source validation. It never claims minute or trade-level
precision. A gap, non-hour boundary, duplicate ambiguity, contradictory OHLC,
or unprovable coverage fails closed.

Fresh transport retrieval must be no more than five minutes old. Because the
price is deliberately the newest completed hourly close, its observation may be
up to 65 minutes old; anything older is `DATA_STALE`. This avoids treating an
in-progress aggregate as exact while making the coarser resolution explicit.

## Fallback policy and diagnostics

Fallback is attempted only after a complete Bybit source path raises
`SOURCE_UNAVAILABLE`, `RATE_LIMITED`, `INVALID_RESPONSE`, or
`INSUFFICIENT_HISTORY`. Identity contradictions, source mismatch, stale data,
invalid windows, contradictory prices, schema failures, and code invariants are
hard failures.

Successful snapshot metadata includes source, exact external symbol, resolution,
primary source, primary failure category, and whether fallback was attempted.
When both paths are unavailable, the typed aggregate error retains both source
names and failure categories. No raw TradingView response or credentials are
logged. This source selection is outside the V1 engine and changes no allocation
rule.
