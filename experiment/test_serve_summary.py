import json
from pathlib import Path
import tempfile
import unittest

from serve_summary import build_summary, SummaryCache


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

    def test_incremental_cache_reads_only_appended_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            observations = Path(directory) / "observations.jsonl"
            mempool = Path(directory) / "mempool.jsonl"
            observations.write_text(
                json.dumps({"kind": "baseline", "observed_at_ms": 1000}) + "\n",
                encoding="utf-8",
            )
            mempool.write_text(
                json.dumps({"kind": "mempool_detection", "observed_at_ms": 1100, "source": "one"}) + "\n",
                encoding="utf-8",
            )
            cache = SummaryCache(observations, mempool)
            first = cache.value(1200)
            self.assertEqual(first["event_count"], 1)
            self.assertEqual(first["mempool"]["detection_count"], 1)
            with mempool.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"kind": "mempool_detection", "observed_at_ms": 1300, "source": "two"}) + "\n")
            second = cache.value(1400)
            self.assertEqual(second["event_count"], 1)
            self.assertEqual(second["mempool"]["detection_count"], 2)
            self.assertEqual(second["mempool"]["sources"], ["one", "two"])

    def test_summary_joins_mempool_to_public_hash_and_scores_early_book(self):
        with tempfile.TemporaryDirectory() as directory:
            observations = Path(directory) / "observations.jsonl"
            mempool = Path(directory) / "mempool.jsonl"
            observations.write_text(json.dumps({
                "kind": "follower_quote", "observed_at_ms": 2100, "detected_at_ms": 2000,
                "public_detection_lag_ms": 1000, "actual_sample_delay_ms": 100,
                "trade": {"transactionHash": "0xABC", "price": 0.50},
                "paper_quote": {"filled": True, "meets_min_order_size": True,
                                "executable": True, "fee_usd": 0, "vwap": 0.50},
            }) + "\n", encoding="utf-8")
            mempool.write_text("\n".join((
                json.dumps({"kind": "mempool_detection", "observed_at_ms": 1200,
                            "source": "publicnode", "transaction_hash": "0xabc",
                            "order_size": 10, "fill_size": 2}),
                json.dumps({"kind": "mempool_quote", "observed_at_ms": 1250,
                            "detected_at_ms": 1200, "actual_sample_delay_ms": 50,
                            "source": "publicnode", "transaction_hash": "0xabc",
                            "leader_price": 0.50, "paper_quote": {"filled": True,
                            "meets_min_order_size": True, "executable": True,
                            "fee_usd": 0, "vwap": 0.50},
                            "paper_challengers": {"uncapped": {"filled": True,
                            "meets_min_order_size": True, "executable": True,
                            "fee_usd": 0, "vwap": 0.51}}}),
            )) + "\n", encoding="utf-8")
            result = SummaryCache(observations, mempool).value(3000)
            self.assertEqual(result["mempool"]["public_feed_hash_matches"], 1)
            self.assertEqual(result["mempool"]["lead_vs_public_detection_ms"]["median"], 800)
            self.assertEqual(result["mempool"]["book_samples"]["executable_quotes"], 1)
            self.assertEqual(result["mempool"]["book_samples"]["sample_delay_ms"]["median"], 50)
            self.assertEqual(result["mempool"]["uncapped_book_samples"]["executable_quotes"], 1)
            self.assertEqual(result["mempool"]["leader_order_size"]["partial_fills"], 1)
            self.assertEqual(result["mempool"]["leader_order_size"]["partial_fill_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
