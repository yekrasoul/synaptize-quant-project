# Venue Architecture

`VenueAdapter` is the platform port: account identity, balance, native intent
validation, one submission, and reconciliation. `CcxtVenueAdapter` is the CEX
implementation. CCXT types never cross into Core or Strategy.

`venues/bybit/mapper.py` is the only current-generation Bybit order mapping;
it maps a canonical BTC/USDT Spot market buy to the required CCXT/Bybit
parameters. It has not yet replaced legacy transports in production. That
replacement requires request/response parity, fill normalization, durable
pre-submit evidence, and fault-injection sign-off.

If acknowledgement lacks a venue order ID, the adapter queries Bybit/CCXT by
the canonical client identity (`orderLinkId`). Zero or conflicting matches stay
ambiguous. Offline mapping and normalization tests are present; exact live
request-byte parity remains **NOT VERIFIED** and is an activation blocker.

CCXT `InvalidOrder` and `InsufficientFunds` are normalized at the adapter
boundary as deterministic rejection. Other submission exceptions remain
ambiguous, because a network write may have occurred. The adapter validates
venue minimum cost and, where CCXT exposes it, quote-cost precision before the
durable claim/network boundary.

See [`BYBIT_CCXT_CUTOVER.md`](BYBIT_CCXT_CUTOVER.md) for the fixture-backed
contract, unverified wire evidence, and legacy removal criteria.
