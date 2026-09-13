import unittest

from evaluate_observations import summarise


class EvaluateObservationTests(unittest.TestCase):
    def test_summary_separates_depth_from_executable_size(self):
        row = {
            "kind": "follower_quote",
            "observed_at_ms": 2000,
            "lane": "candidate_1",
            "market_categories": ["CRYPTO"],
            "public_detection_lag_ms": 500,
            "actual_sample_delay_ms": 251,
            "trade": {"price": 0.5},
            "paper_quote": {
                "filled": True,
                "meets_min_order_size": False,
                "executable": False,
                "fee_usd": 0.01,
                "vwap": 0.501,
            },
            "paper_challengers": {
                "fixed_slippage_c": {"0.5": {
                    "filled": True, "meets_min_order_size": True, "executable": True,
                    "fee_usd": 0.01, "vwap": 0.502,
                }},
                "venue_minimum": {"quote": None},
            },
        }
        detections = [
            {"kind": "feed_detection", "observed_at_ms": 1000, "trade_key": "t", "feed": "trades"},
            {"kind": "feed_detection", "observed_at_ms": 1250, "trade_key": "t", "feed": "activity"},
        ]
        result = summarise(detections + [row])
        self.assertEqual(result["all_quotes"]["depth_filled"], 1)
        self.assertEqual(result["all_quotes"]["executable_quotes"], 0)
        self.assertEqual(result["by_market_category"]["CRYPTO"]["buy_signals"], 1)
        self.assertEqual(result["challengers"]["slippage_0.5c"]["executable_quotes"], 1)
        self.assertEqual(result["feed_detection"]["activity_minus_trades_ms"]["median"], 250)
        self.assertIsNone(result["promotion_threshold"])
        self.assertFalse(result["order_capability"])


if __name__ == "__main__":
    unittest.main()
