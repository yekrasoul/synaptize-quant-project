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
