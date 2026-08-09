import unittest

from ops_finance import _classify_tier


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
