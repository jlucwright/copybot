import unittest

from refresh_candidates import activity_summary, protocol_digest


class RefreshCandidateTests(unittest.TestCase):
    def test_activity_summary_is_bounded_and_descriptive(self):
        now = 1_000_000
        rows = [
            {"timestamp": now - 1, "conditionId": "a", "side": "BUY"},
            {"timestamp": now - 2, "conditionId": "b", "side": "SELL"},
            {"timestamp": now - 8 * 86400, "conditionId": "c", "side": "BUY"},
        ]
        result = activity_summary(rows, now)
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["trades_in_sample_from_last_7d"], 2)
        self.assertEqual(result["distinct_markets_in_sample_from_last_7d"], 2)
        self.assertEqual(result["buy_count_in_sample_from_last_7d"], 1)
        self.assertEqual(result["sell_count_in_sample_from_last_7d"], 1)

    def test_protocol_digest_ignores_mapping_order(self):
        self.assertEqual(protocol_digest({"a": 1, "b": 2}), protocol_digest({"b": 2, "a": 1}))


if __name__ == "__main__":
    unittest.main()
