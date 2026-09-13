import unittest

from serve_summary import build_summary


class SummaryHandlerTests(unittest.TestCase):
    def test_summary_is_explicitly_order_incapable_and_unsettled(self):
        result = build_summary([{"kind": "baseline", "observed_at_ms": 1000}], 2000)
        self.assertFalse(result["order_capability"])
        self.assertEqual(result["profitability_status"], "unknown_no_settled_outcomes")
        self.assertEqual(result["last_observed_at_ms"], 1000)

    def test_summary_reports_order_incapable_mempool_detections(self):
        result = build_summary(
            [{"kind": "baseline", "observed_at_ms": 1000}],
            2000,
            [{"kind": "mempool_detection", "observed_at_ms": 1500, "source": "publicnode"}],
        )
        self.assertEqual(result["mempool"]["detection_count"], 1)
        self.assertEqual(result["mempool"]["last_observed_at_ms"], 1500)
        self.assertFalse(result["mempool"]["order_capability"])


if __name__ == "__main__":
    unittest.main()
