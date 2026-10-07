# Application modules

This directory contains the deterministic core and read-only production-shadow
adapters:

- `engine.py`: pure deterministic V1 calculation
- `ledger.py`: read-only canonical execution-history access
- `portfolio.py`: derived portfolio state
- `market_data/` and `sentiment/`: validated public read-only acquisition
- `artifacts/`: immutable canonical persistence
- `shadow.py`: deterministic Phase 3 composition
- `production.py`: Phase 4 scheduling context and structured workflow handoff
- `notifications/`: independent Telegram delivery from structured outcomes
- `cli.py`: offline and production-shadow command adapter
- `config.py` and `schemas.py`: boundary validation

Live order execution, authenticated exchange access, ledger mutation from a
recommendation, and strategy-rule duplication do not belong in this package.
