# Crypto Fear & Greed adapter (Phase 3.6)

The read-only adapter uses Alternative.me's public [Crypto Fear & Greed API](https://alternative.me/crypto/fear-and-greed-index/#api), specifically `https://api.alternative.me/fng/?limit=1&format=json`. It normalizes exactly one current observation into the versioned `SentimentSnapshot` contract. The source publishes a 0–100 integer index and normally updates daily. Its human-readable classification (for example, `Fear`) is retained only as descriptive metadata; the numeric value is canonical.

`observed_at_utc` is the upstream Unix-seconds publication timestamp; `retrieved_at_utc` is the local UTC retrieval instant. This source does not offer a more precise observation time than that timestamp. The operational default freshness policy is 36 hours, configurable through `max_observation_age`; it is intentionally separate from the five-minute BTC market-data policy. A future timestamp, stale observation, malformed response, missing provenance, non-integer/out-of-range value, or multiple current readings fails closed.

The transport is HTTPS-only with explicit connect/read timeouts and at most three attempts (the existing public transport policy). HTTP 429 is `RATE_LIMITED`; connectivity failures are `SOURCE_UNAVAILABLE`; invalid payload/value/timestamp and stale data have distinct typed error codes. There is no fallback sentiment source in this phase.

The adapter returns only validated sentiment data. It does not calculate a multiplier, allocation, buy recommendation, BTC price, or rolling high. V1 multiplier bands remain exclusively in the active strategy config declared by `config/project_manifest.yaml`; the deterministic engine consumes the integer `SentimentSnapshot.value` at a later orchestration boundary.
