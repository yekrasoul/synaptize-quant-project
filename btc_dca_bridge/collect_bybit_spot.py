#!/usr/bin/env python3
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WINDOW_HOURS = 168
OUT = Path(__file__).with_name("latest.json")

def http_json(url, params=None, timeout=20):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "btc-dca-market-bridge/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status} from {url}")
        return json.loads(r.read().decode("utf-8"))

def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

def validate_window(highs, price):
    if not highs:
        raise RuntimeError("no valid candles in rolling window")
    high = max(highs)
    if price <= 0 or high <= 0:
        raise RuntimeError("non-positive price/high")
    return high

def collect_bybit():
    base = "https://api.bybit.com"
    ticker = http_json(base + "/v5/market/tickers", {"category":"spot","symbol":"BTCUSDT"})
    if ticker.get("retCode") != 0:
        raise RuntimeError(ticker.get("retMsg"))
    if ticker.get("result",{}).get("category") != "spot":
        raise RuntimeError("category mismatch")
    rows = ticker["result"].get("list") or []
    if len(rows) != 1 or rows[0].get("symbol") != "BTCUSDT":
        raise RuntimeError("symbol mismatch")
    price = float(rows[0]["lastPrice"])

    now_ms = int(time.time()*1000)
    start_ms = now_ms - WINDOW_HOURS*3600_000
    first_full_hour = ((start_ms + 3600_000 - 1)//3600_000)*3600_000

    def klines(interval, start, end, limit):
        p = http_json(base + "/v5/market/kline", {
            "category":"spot","symbol":"BTCUSDT","interval":str(interval),
            "start":start,"end":end,"limit":limit
        })
        if p.get("retCode") != 0:
            raise RuntimeError(p.get("retMsg"))
        r = p.get("result",{})
        if r.get("category") != "spot" or r.get("symbol") != "BTCUSDT":
            raise RuntimeError("kline identity mismatch")
        return sorted((int(c[0]), float(c[2])) for c in (r.get("list") or []))

    hourly = [(ts,h) for ts,h in klines(60, first_full_hour, now_ms, 200) if first_full_hour <= ts <= now_ms]
    boundary_end = min(first_full_hour-1, now_ms)
    boundary = []
    if boundary_end >= start_ms:
        boundary = [(ts,h) for ts,h in klines(1, start_ms, boundary_end, 120) if start_ms <= ts <= boundary_end]

    if len(hourly) < 166:
        raise RuntimeError(f"incomplete hourly coverage: {len(hourly)}")
    high = validate_window([h for _,h in hourly] + [h for _,h in boundary], price)

    return {
        "source_exchange":"Bybit","source_market":"spot","source_symbol":"BTCUSDT","quote":"USDT",
        "price_usdt":price,"rolling_7d_high_usdt":high,
        "window_start_utc":iso(start_ms),"window_end_utc":iso(now_ms),
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "validation":{"same_source_price_and_high":True,"full_168h_coverage":True,"spot_market":True,"fresh_ticker":True}
    }

def collect_binance():
    base = "https://api.binance.com"
    ticker = http_json(base + "/api/v3/ticker/price", {"symbol":"BTCUSDT"})
    if ticker.get("symbol") != "BTCUSDT":
        raise RuntimeError("symbol mismatch")
    price = float(ticker["price"])

    now_ms = int(time.time()*1000)
    start_ms = now_ms - WINDOW_HOURS*3600_000
    first_full_hour = ((start_ms + 3600_000 - 1)//3600_000)*3600_000

    def klines(interval, start, end, limit):
        rows = http_json(base + "/api/v3/klines", {
            "symbol":"BTCUSDT","interval":interval,"startTime":start,"endTime":end,"limit":limit
        })
        return sorted((int(c[0]), float(c[2])) for c in rows)

    hourly = [(ts,h) for ts,h in klines("1h", first_full_hour, now_ms, 1000) if first_full_hour <= ts <= now_ms]
    boundary_end = min(first_full_hour-1, now_ms)
    boundary = []
    if boundary_end >= start_ms:
        boundary = [(ts,h) for ts,h in klines("1m", start_ms, boundary_end, 1000) if start_ms <= ts <= boundary_end]

    if len(hourly) < 166:
        raise RuntimeError(f"incomplete hourly coverage: {len(hourly)}")
    high = validate_window([h for _,h in hourly] + [h for _,h in boundary], price)

    return {
        "source_exchange":"Binance","source_market":"spot","source_symbol":"BTCUSDT","quote":"USDT",
        "price_usdt":price,"rolling_7d_high_usdt":high,
        "window_start_utc":iso(start_ms),"window_end_utc":iso(now_ms),
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "validation":{"same_source_price_and_high":True,"full_168h_coverage":True,"spot_market":True,"fresh_ticker":True}
    }

def collect_kucoin():
    base = "https://api.kucoin.com"
    ticker = http_json(base + "/api/v1/market/orderbook/level1", {"symbol":"BTC-USDT"})
    data = ticker.get("data") or {}
    if ticker.get("code") != "200000" or data.get("symbol") != "BTC-USDT":
        raise RuntimeError("ticker identity mismatch")
    price = float(data["price"])

    now_ms = int(time.time()*1000)
    start_ms = now_ms - WINDOW_HOURS*3600_000
    first_full_hour = ((start_ms + 3600_000 - 1)//3600_000)*3600_000

    def candles(kind, start_ms_, end_ms_):
        p = http_json(base + "/api/v1/market/candles", {
            "type":kind,"symbol":"BTC-USDT",
            "startAt":start_ms_//1000,"endAt":end_ms_//1000
        })
        if p.get("code") != "200000":
            raise RuntimeError("candle API error")
        rows = p.get("data") or []
        # [time, open, close, high, low, volume, turnover]
        return sorted((int(c[0])*1000, float(c[3])) for c in rows)

    hourly = [(ts,h) for ts,h in candles("1hour", first_full_hour, now_ms) if first_full_hour <= ts <= now_ms]
    boundary_end = min(first_full_hour-1, now_ms)
    boundary = []
    if boundary_end >= start_ms:
        boundary = [(ts,h) for ts,h in candles("1min", start_ms, boundary_end) if start_ms <= ts <= boundary_end]

    if len(hourly) < 166:
        raise RuntimeError(f"incomplete hourly coverage: {len(hourly)}")
    high = validate_window([h for _,h in hourly] + [h for _,h in boundary], price)

    return {
        "source_exchange":"KuCoin","source_market":"spot","source_symbol":"BTC-USDT","quote":"USDT",
        "price_usdt":price,"rolling_7d_high_usdt":high,
        "window_start_utc":iso(start_ms),"window_end_utc":iso(now_ms),
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "validation":{"same_source_price_and_high":True,"full_168h_coverage":True,"spot_market":True,"fresh_ticker":True}
    }

def main():
    collectors = [collect_bybit, collect_binance, collect_kucoin]
    errors = []
    for fn in collectors:
        try:
            d = fn()
            d.update({
                "schema_version":3,
                "status":"ok",
                "asset":"BTC",
                "strategy_market_note":"Reliable spot source; same source used for current price and rolling 7D high."
            })
            OUT.write_text(json.dumps(d, indent=2)+"\n", encoding="utf-8")
            print(json.dumps(d, indent=2))
            return
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")

    payload = {
        "schema_version":3,
        "status":"error",
        "asset":"BTC",
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "errors":errors
    }
    OUT.write_text(json.dumps(payload, indent=2)+"\n", encoding="utf-8")
    raise SystemExit(" | ".join(errors))

if __name__ == "__main__":
    main()
