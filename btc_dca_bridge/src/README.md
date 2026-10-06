# Application modules

This directory contains the offline Phase 2 core:

- `engine.py`: pure deterministic V1 calculation
- `ledger.py`: read-only canonical execution-history access
- `portfolio.py`: derived portfolio state
- `cli.py`: offline command adapter
- `config.py` and `schemas.py`: boundary validation

Market retrieval, notification, orchestration, and order execution do not belong in this package.
