import unittest

from ops_finance import _classify_tier, calc_unit_profit


class TestCalcUnitProfitShipping(unittest.TestCase):
    """国際送料の想定(CEO: 「国際便なので一万円くらいはしそうです。ただし何個纏めて
    仕入れるかで損益分岐点が変わらそうです」) - 1回の発送固定費用(既定¥10,000)を、
    JP原価から逆算した「まとめ買い個数」(総額が概ね¥50,000になる個数)で按分する。
    """

    def test_cheap_item_splits_shipping_across_many_units(self):
        # JP原価¥300 -> 50000 // 300 = 166個まとめ買い -> 10000/166/150 ≈ $0.40
        result = calc_unit_profit(us_price_usd=50.0, jp_cost_jpy=300.0, weight_kg=0.1)
        self.assertAlmostEqual(result['shipping_cost_usd'], 0.4, places=2)

    def test_expensive_item_bears_full_shipment_cost_alone(self):
        # JP原価¥60,000は予算¥50,000を超えるため、まとめ買い個数は最低1個 ->
        # 送料按分は1個で発送費用¥10,000を丸ごと負担 -> 10000/150 ≈ $66.67
        result = calc_unit_profit(us_price_usd=200.0, jp_cost_jpy=60000.0, weight_kg=2.0)
        self.assertAlmostEqual(result['shipping_cost_usd'], 66.67, places=2)

    def test_boundary_cost_equals_budget(self):
        # JP原価がちょうど予算(¥50,000)と一致する場合も、まとめ買い個数は1個
        result = calc_unit_profit(us_price_usd=200.0, jp_cost_jpy=50000.0, weight_kg=2.0)
        self.assertAlmostEqual(result['shipping_cost_usd'], 66.67, places=2)

    def test_custom_shipment_params_are_honored(self):
        # shipment_fixed_cost_jpy/shipment_budget_jpyを明示的に上書きできること
        result = calc_unit_profit(
            us_price_usd=50.0, jp_cost_jpy=5000.0, weight_kg=0.5,
            shipment_fixed_cost_jpy=15_000.0, shipment_budget_jpy=30_000.0,
        )
        # 30000 // 5000 = 6個まとめ買い -> 15000/6/150 = $16.67
        self.assertAlmostEqual(result['shipping_cost_usd'], 16.67, places=2)

    def test_weight_kg_no_longer_affects_shipping(self):
        # 重量が大きく違っても(JP原価が同じなら)送料は変わらない
        light = calc_unit_profit(us_price_usd=50.0, jp_cost_jpy=1000.0, weight_kg=0.01)
        heavy = calc_unit_profit(us_price_usd=50.0, jp_cost_jpy=1000.0, weight_kg=20.0)
        self.assertEqual(light['shipping_cost_usd'], heavy['shipping_cost_usd'])


class TestClassifyTier(unittest.TestCase):
    """合格ラインの多段階化(CEO: 「合格ラインは何段階かに分けてください」)。
    _classify_tier() の境界値を確認する。
    """

    def test_exactly_at_pass_threshold(self):
        self.assertEqual(_classify_tier(0.20, 100, 50), 'pass')

    def test_just_below_pass_threshold(self):
        self.assertEqual(_classify_tier(0.1999, 100, 50), 'consider')

    def test_exactly_zero_margin(self):
        self.assertEqual(_classify_tier(0.0, 100, 50), 'consider')

    def test_just_below_zero_margin_with_nonneg_gross(self):
        # margin_pctはマイナスだが、手数料を一切引かない粗差(US-JP)はちょうど0
        self.assertEqual(_classify_tier(-0.01, 100, 100), 'reference')

    def test_negative_margin_negative_gross(self):
        self.assertEqual(_classify_tier(-0.01, 90, 100), 'reject')

    def test_no_price_data(self):
        self.assertEqual(_classify_tier(None, None, None), 'reject')

    def test_custom_min_margin_pct(self):
        self.assertEqual(_classify_tier(0.10, 100, 50, min_margin_pct=0.10), 'pass')
        self.assertEqual(_classify_tier(0.09, 100, 50, min_margin_pct=0.10), 'consider')


if __name__ == '__main__':
    unittest.main()
