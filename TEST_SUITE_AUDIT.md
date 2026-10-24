# Test Suite Audit

V2 adds contract tests for: venue-neutral mapping boundary, $500 cap and kill
switch, account identity, unresolved execution, exactly-once claim, ambiguous
timeout prohibition, reconciliation-before-accounting, runtime policy drift,
and dependency direction.

Legacy tests remain while legacy production code remains reachable. No test was
deleted merely to increase the apparent simplification rate. Tests that freeze
old workflow or guardian structure were already removed in PR #76.
