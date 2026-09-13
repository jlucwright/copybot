import unittest

from observe_wallets import (
    buy_quote,
    lane_accepts,
    load_state,
    requested_cash,
    normalise_v2_trade,
    roster_specs,
    state_baselined_wallets,
    taker_fee,
    trade_key,
    venue_minimum_cash,
)
from pathlib import Path
import json
import tempfile


class ObserverTests(unittest.TestCase):
    def test_fee_uses_market_curve(self):
        self.assertAlmostEqual(taker_fee(2, 0.5, 0.07), 0.035)

    def test_buy_quote_walks_depth_and_includes_fee_in_cap(self):
        quote = buy_quote(
            [{"price": "0.50", "size": "1"}, {"price": "0.51", "size": "10"}],
            1.0,
            0.07,
        )
        self.assertTrue(quote["filled"])
        self.assertEqual(quote["levels"], 2)
        self.assertLessEqual(quote["total_usd"], 1.000001)
        self.assertGreater(quote["fee_usd"], 0)
        self.assertTrue(quote["executable"])

    def test_unfilled_quote_is_explicit(self):
        quote = buy_quote([{"price": "0.50", "size": "0.5"}], 1.0, 0.07)
        self.assertFalse(quote["filled"])
        self.assertGreater(quote["unspent_usd"], 0.7)
        self.assertFalse(quote["executable"])

    def test_quote_enforces_slippage_and_market_minimum(self):
        quote = buy_quote(
            [{"price": "0.50", "size": "4"}, {"price": "0.51", "size": "10"}],
            2.0,
            0.0,
            min_order_size=5,
            max_price=0.50,
        )
        self.assertTrue(quote["filled"])
        self.assertFalse(quote["meets_min_order_size"])
        self.assertFalse(quote["executable"])

    def test_minimum_floor_and_maximum_cap(self):
        lane = {"pct": 0.005, "max_usd": 2.0, "min_usd": 1.0, "min_fill_floor": True}
        self.assertEqual(requested_cash({"usdcSize": 4.0}, lane), 1.0)
        self.assertEqual(requested_cash({"usdcSize": 1000.0}, lane), 2.0)

    def test_v2_trade_normalisation_derives_missing_usdc_size(self):
        row = {
            "proxy_wallet": "0x1", "transaction_hash": "0x2", "timestamp": 3,
            "condition_id": "0x4", "token_id": "5", "side": "BUY",
            "size": 4, "price": 0.25, "event_slug": "event",
        }
        trade = normalise_v2_trade(row)
        self.assertEqual(trade["asset"], "5")
        self.assertEqual(trade["usdcSize"], 1.0)
        self.assertEqual(trade["eventSlug"], "event")

    def test_venue_minimum_cash_is_capped_and_depth_aware(self):
        asks = [{"price": "0.50", "size": "4"}, {"price": "0.60", "size": "4"}]
        self.assertAlmostEqual(venue_minimum_cash(asks, 5, 0.0, 5.0), 2.6)
        self.assertIsNone(venue_minimum_cash(asks, 5, 0.0, 2.5))
        self.assertIsNone(venue_minimum_cash(asks[:1], 5, 0.0, 5.0))

    def test_trade_key_is_stable_and_trade_specific(self):
        trade = {"timestamp": 1, "asset": "a", "side": "BUY", "size": 2, "price": 0.5}
        self.assertEqual(trade_key(trade), trade_key(dict(trade)))
        changed = dict(trade, size=3)
        self.assertNotEqual(trade_key(trade), trade_key(changed))

    def test_lane_market_filter_is_case_insensitive_and_fail_closed(self):
        lane = {"title_contains": "Ethereum Up or Down"}
        self.assertTrue(lane_accepts({"title": "Ethereum Up or Down - 1PM"}, lane))
        self.assertFalse(lane_accepts({"title": "Bitcoin Up or Down - 1PM"}, lane))

    def test_roster_requires_explicit_paper_sizing(self):
        value = {
            "schema": "copybot.wallet-observer-roster.v1",
            "lanes": [{
                "name": "candidate_12345678",
                "wallet": "0x" + "1" * 40,
                "pct": 0.005,
                "max_usd_per_fill": 2,
                "min_order_usd": 1,
                "min_fill_floor": True,
                "buy_slippage_c": 0.15,
                "leaderboard_categories": ["CRYPTO"],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roster.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(roster_specs(path)[0]["leaderboard_categories"], ["CRYPTO"])

    def test_version_two_state_requires_per_wallet_baselines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 2, "seen": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_state(path)

    def test_version_one_state_is_rebaselined_fail_closed(self):
        self.assertEqual(state_baselined_wallets({"version": 1, "seen": ["old"]}), set())
        self.assertEqual(
            state_baselined_wallets({"version": 2, "seen": [], "baselined_wallets": ["0x1"]}),
            {"0x1"},
        )


if __name__ == "__main__":
    unittest.main()
