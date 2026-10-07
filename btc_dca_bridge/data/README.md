# Runtime data

This directory is reserved for generated immutable runtime artifacts. `market/`,
`sentiment/`, `decisions/`, and completed `runs/` manifests (plus their SHA-256 sidecars) are
runtime-generated and intentionally ignored by Git; this README and the
contracts are committed. See `../docs/IMMUTABLE_ARTIFACTS.md` for the layout,
atomic publication policy, and read-back rules. Runtime artifacts are not the
canonical reconciled execution ledger.

Do not place reconciled executions here; use `../ledger/executions.jsonl`.
