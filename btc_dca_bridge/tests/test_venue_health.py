from __future__ import annotations

import unittest

from btc_dca_bridge.venues.health import VenueCircuitBreaker, VenueHealthState


class VenueCircuitBreakerTests(unittest.TestCase):
    def test_failure_transitions_and_successful_probe_recovery(self):
        breaker = VenueCircuitBreaker(failure_threshold=3)
        self.assertEqual(breaker.current().state, VenueHealthState.HEALTHY)
        self.assertEqual(breaker.record_failure("timeout").state, VenueHealthState.DEGRADED)
        self.assertEqual(breaker.record_failure("reset").state, VenueHealthState.DEGRADED)
        self.assertEqual(breaker.record_failure("outage").state, VenueHealthState.UNAVAILABLE)
        self.assertEqual(breaker.record_success().state, VenueHealthState.HEALTHY)

    def test_invalid_threshold_is_rejected(self):
        with self.assertRaises(ValueError):
            VenueCircuitBreaker(failure_threshold=0)


if __name__ == "__main__": unittest.main()
