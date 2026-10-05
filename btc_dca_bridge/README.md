# BTC DCA Bybit Spot bridge

This collector retrieves only Bybit BTC/USDT Spot data using explicit `category=spot`.

It validates:
- exact symbol `BTCUSDT`
- spot product category
- fresh ticker timestamp
- at least 168 hourly candles spanning the rolling seven-day window

It writes `latest.json` with the current Spot price and computed rolling 7-day high.

The GitHub Actions workflow runs hourly at minute 07 and can also be dispatched manually.
