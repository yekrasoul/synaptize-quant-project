# Immutable pipeline artifacts (Phase 3.7)

`ArtifactStore` persists validated market snapshots, sentiment snapshots, and already-produced decisions below an injectable artifact root. Repository runtime payloads are ignored by Git; only `data/README.md`, source, schemas, and documentation are committed. The reconciled execution ledger remains `ledger/executions.jsonl`: a persisted Decision is not an Execution and never changes the ledger.

## Layout and run identity

The UTC date comes from `captured_at_utc` (market), `retrieved_at_utc` (sentiment), or `created_at_utc` (decision):

```text
data/
  market/2026/10/07/run_20261007T120000Z_a1b2c3d4e5f6.json
  market/2026/10/07/run_20261007T120000Z_a1b2c3d4e5f6.json.sha256
  sentiment/2026/10/07/run_20261007T120000Z_a1b2c3d4e5f6.json
  decisions/2026/10/07/run_20261007T120000Z_a1b2c3d4e5f6.json
```

One logical orchestration run supplies the same `run_id` to all three writes. A run ID is `run_YYYYMMDDTHHMMSSZ_<suffix>`; the suffix is 8–64 filesystem-safe ASCII letters, digits, `_`, or `-`, and must start alphanumeric. Callers inject the timestamp and stable suffix, so tests and future orchestration can be deterministic. IDs containing separators, traversal, nulls, or ambiguous forms are rejected.

## Validation, serialization, and integrity

The store validates each artifact against its canonical existing schema before creating a final path. It serializes UTF-8 JSON with lexicographically sorted keys, compact separators, no NaN, and exactly one trailing newline. No Decimal-to-float conversion occurs in persistence. A `.sha256` sidecar contains the SHA-256 of those exact bytes. Read-back requires the sidecar, checks the digest, parses UTF-8 JSON, re-validates the expected schema, confirms canonical serialization, and verifies its UTC directory matches the artifact timestamp.

## Atomic immutability

Temporary files are written and fsynced in the final directory. On supported macOS and Linux filesystems, `os.link(temp, final)` atomically creates a new directory entry and fails with `EEXIST` if either concurrent writer already created the target. This deliberately avoids `os.replace`, which can overwrite. The sidecar is published before the JSON payload; a crash can leave only an ignored/orphan sidecar, never a partial canonical `.json` artifact. Temporary files are cleaned on handled failure. A filesystem without same-directory hard-link or directory-fsync support fails closed with `PERSISTENCE_IO_ERROR` rather than weakening no-clobber semantics.

Failures distinguish invalid paths, already-existing artifacts, missing artifacts, schema-validation failure, corrupt stored content/digests, and I/O. There is no database, cloud storage, scheduling, market retrieval, allocation calculation, execution, or ledger mutation in this layer.
