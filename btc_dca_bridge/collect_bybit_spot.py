#!/usr/bin/env python3
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASES = [
    "https://api.bybit.com",
    "https://api.bytick.com",
]
SYMBOL = "BTCUSDT"
CATEGORY = "spot"
WINDOW_HOURS = 168
OUT = Path(__file__).with_name("latest.json")

def get_json(base, path, params, timeout=20):
    url = base + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "btc-dca-bridge/1.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status} from {url}")
        return json.loads(r.read().decode("utf-8"))

def require_ok(payload, where):
    if payload.get("retCode") != 0:
        raise RuntimeError(f"{where}: retCode={payload.get('retCode')} retMsg={payload.get('retMsg')}")

def fetch_klines(base, interval, start_ms, end_ms, limit):
    p = get_json(base, "/v5/market/kline", {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "interval": str(interval),
        "start": start_ms,
        "end": end_ms,
        "limit": limit,
    })
    require_ok(p, f"kline-{interval}")
    result = p.get("result", {})
    if result.get("category") != CATEGORY:
        raise RuntimeError(f"kline-{interval} category mismatch")
    if result.get("symbol") != SYMBOL:
        raise RuntimeError(f"kline-{interval} symbol mismatch")
    rows = result.get("list") or []
    parsed = [(int(c[0]), float(c[2])) for c in rows]
    parsed.sort()
    return parsed

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

    # Interior window: hourly candles fully inside the rolling boundary.
    first_full_hour_ms = ((start_ms + 3600_000 - 1) // 3600_000) * 3600_000
    hourly = fetch_klines(
        base, 60,
        first_full_hour_ms,
        now_ms,
        200,
    )
    hourly_in_window = [(ts, h) for ts, h in hourly if first_full_hour_ms <= ts <= now_ms]
    expected_min_hourly = int((now_ms - first_full_hour_ms) // 3600_000)
    if len(hourly_in_window) < max(1, expected_min_hourly):
        raise RuntimeError(
            f"incomplete hourly coverage: {len(hourly_in_window)} candles, expected at least {expected_min_hourly}"
        )

    # Boundary fragment: use 1-minute Spot candles from exact rolling start to first full hour.
    boundary_end_ms = min(first_full_hour_ms - 1, now_ms)
    boundary = []
    if boundary_end_ms >= start_ms:
        boundary = fetch_klines(
            base, 1,
            start_ms,
            boundary_end_ms,
            120,
        )
        boundary = [(ts, h) for ts, h in boundary if start_ms <= ts <= boundary_end_ms]
        required_boundary_minutes = int((boundary_end_ms - start_ms) // 60_000) + 1
        if len(boundary) < max(1, required_boundary_minutes - 1):
            raise RuntimeError(
                f"incomplete 1m boundary coverage: {len(boundary)} candles, expected about {required_boundary_minutes}"
            )

    highs = [h for _, h in hourly_in_window] + [h for _, h in boundary]
    if not highs:
        raise RuntimeError("no valid candles in rolling window")
    seven_day_high = max(highs)

    server_ms = int(ticker.get("time") or now_ms)
    age_sec = max(0, (now_ms - server_ms) / 1000)
    if age_sec > 300:
        raise RuntimeError(f"stale ticker timestamp: {age_sec:.0f}s")

    return {
        "schema_version": 2,
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
        "hourly_candles_validated": len(hourly_in_window),
        "boundary_1m_candles_validated": len(boundary),
        "source_base": base,
        "validation": {
            "category_is_spot": True,
            "symbol_is_btcusdt": True,
            "full_168h_coverage": True,
            "exact_boundary_rebuilt_with_1m_candles": True,
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
            "schema_version": 2,
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
