#!/usr/bin/env python3
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASES = [
    "https://api.bybit.com",
    "https://api.bytick.com",
]
SYMBOL = "BTCUSDT"
CATEGORY = "spot"
INTERVAL = "60"
WINDOW_HOURS = 168
OUT = Path(__file__).with_name("latest.json")

def get_json(base, path, params, timeout=20):
    url = base + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "btc-dca-bridge/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status} from {url}")
        return json.loads(r.read().decode("utf-8"))

def require_ok(payload, where):
    if payload.get("retCode") != 0:
        raise RuntimeError(f"{where}: retCode={payload.get('retCode')} retMsg={payload.get('retMsg')}")

def fetch_from(base):
    ticker = get_json(base, "/v5/market/tickers", {"category": CATEGORY, "symbol": SYMBOL})
    require_ok(ticker, "ticker")
    if ticker.get("result", {}).get("category") != CATEGORY:
        raise RuntimeError("ticker category mismatch")
    rows = ticker.get("result", {}).get("list") or []
    if len(rows) != 1 or rows[0].get("symbol") != SYMBOL:
        raise RuntimeError("ticker symbol mismatch")
    price = float(rows[0]["lastPrice"])

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - WINDOW_HOURS * 3600 * 1000
    # Request 170 hourly candles to safely cover the rolling boundary.
    kl = get_json(base, "/v5/market/kline", {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "start": start_ms - 2 * 3600 * 1000,
        "end": now_ms,
        "limit": 200,
    })
    require_ok(kl, "kline")
    if kl.get("result", {}).get("category") != CATEGORY:
        raise RuntimeError("kline category mismatch")
    if kl.get("result", {}).get("symbol") != SYMBOL:
        raise RuntimeError("kline symbol mismatch")
    candles = kl.get("result", {}).get("list") or []
    parsed = []
    for c in candles:
        ts = int(c[0])
        high = float(c[2])
        parsed.append((ts, high))
    parsed.sort()

    in_window = [(ts, h) for ts, h in parsed if ts >= start_ms - 3600 * 1000 and ts <= now_ms]
    if len(in_window) < 168:
        raise RuntimeError(f"incomplete hourly coverage: only {len(in_window)} candles")

    # Require the first selected candle to start no more than one hour after the rolling boundary.
    first_ts = in_window[0][0]
    if first_ts > start_ms + 3600 * 1000:
        raise RuntimeError("rolling-window boundary not covered")

    seven_day_high = max(h for _, h in in_window)
    server_ms = int(ticker.get("time") or now_ms)
    age_sec = max(0, (now_ms - server_ms) / 1000)
    if age_sec > 300:
        raise RuntimeError(f"stale ticker timestamp: {age_sec:.0f}s")

    return {
        "schema_version": 1,
        "exchange": "Bybit",
        "market": "spot",
        "category": CATEGORY,
        "symbol": SYMBOL,
        "price_usdt": price,
        "rolling_7d_high_usdt": seven_day_high,
        "window_hours": WINDOW_HOURS,
        "window_start_utc": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
        "window_end_utc": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(),
        "source_server_time_utc": datetime.fromtimestamp(server_ms / 1000, tz=timezone.utc).isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candle_interval_minutes": 60,
        "candles_validated": len(in_window),
        "source_base": base,
        "validation": {
            "category_is_spot": True,
            "symbol_is_btcusdt": True,
            "full_168h_coverage": True,
            "fresh_ticker": True,
        },
    }

def main():
    errors = []
    data = None
    for base in BASES:
        try:
            data = fetch_from(base)
            break
        except Exception as e:
            errors.append(f"{base}: {e}")
    if data is None:
        payload = {
            "schema_version": 1,
            "exchange": "Bybit",
            "market": "spot",
            "category": CATEGORY,
            "symbol": SYMBOL,
            "status": "error",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "errors": errors,
        }
        OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        raise SystemExit(" | ".join(errors))
    data["status"] = "ok"
    OUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, indent=2))

if __name__ == "__main__":
    main()
