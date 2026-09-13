import unittest

from observe_wallets import buy_quote, lane_accepts, requested_cash, taker_fee, trade_key


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

    def test_unfilled_quote_is_explicit(self):
        quote = buy_quote([{"price": "0.50", "size": "0.5"}], 1.0, 0.07)
        self.assertFalse(quote["filled"])
        self.assertGreater(quote["unspent_usd"], 0.7)

    def test_minimum_floor_and_maximum_cap(self):
        lane = {"pct": 0.005, "max_usd": 2.0, "min_usd": 1.0, "min_fill_floor": True}
        self.assertEqual(requested_cash({"usdcSize": 4.0}, lane), 1.0)
        self.assertEqual(requested_cash({"usdcSize": 1000.0}, lane), 2.0)

    def test_trade_key_is_stable_and_trade_specific(self):
        trade = {"timestamp": 1, "asset": "a", "side": "BUY", "size": 2, "price": 0.5}
        self.assertEqual(trade_key(trade), trade_key(dict(trade)))
        changed = dict(trade, size=3)
        self.assertNotEqual(trade_key(trade), trade_key(changed))

    def test_lane_market_filter_is_case_insensitive_and_fail_closed(self):
        lane = {"title_contains": "Ethereum Up or Down"}
        self.assertTrue(lane_accepts({"title": "Ethereum Up or Down - 1PM"}, lane))
        self.assertFalse(lane_accepts({"title": "Bitcoin Up or Down - 1PM"}, lane))


if __name__ == "__main__":
    unittest.main()
