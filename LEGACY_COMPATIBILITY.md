# Legacy Compatibility

Historical schemas and artifacts remain read-only compatibility inputs. New V2
Core, Risk, and Venue modules do not generate or require Approval,
Authorization, Activation, ProductionEnablement, or CanaryManifest artifacts.

Legacy Phase 5.x command/service code remains temporarily because the existing
durable artifact and reconciliation behavior has not yet been parity-proven
against the V2 persistence path. It must be removed only with fixture-backed
equivalence for pre-submit persistence, ambiguous outcomes, fill parsing, and
ledger ordering.
