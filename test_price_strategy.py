import unittest

import price_strategy as ps


def profit(price):
    # 手数料が$10.00から上がる商品(ウカンムリ型): $10.00〜$10.85は$9.98より利益が少ない
    fee = 5.0 if price < 10 else 5.85
    return round(price - fee - 3.0, 2)


class TestDecide(unittest.TestCase):
    def test_raises_to_just_below_the_layer_whose_cheaper_stock_clears_fast(self):
        # ナイトフォレスト(2026-11-10): 市場は月200個(1日約6.7個)
        layers = [[9.78, 1, 5], [9.8, 1, 11], [9.98, 2, 42], [11.0, 1, 14], [12.0, 1, 14]]
        p = ps.decide(9.59, 9.78, layers, 200, 12, 0, 0, profit)
        self.assertEqual((p['action'], p['target']), ('raise', 9.97))   # $10.99の下は安い在庫58個=約9日分で多すぎる
        self.assertIn('16個', p['reason'])

    def test_skips_prices_where_fees_eat_the_raise(self):
        # 上の層が$10.50だけ: $10.49は$9.98より利益が少ないので上げない
        p = ps.decide(9.98, 9.98, [[10.5, 1, 1]], 200, 12, 0, 0, profit)
        self.assertEqual(p['action'], 'keep')

    def test_unknown_market_allows_only_a_few_cheaper_units(self):
        # キティのシール(月間販売数なし): $7.99の層の下は10個以下 → $7.98
        layers = [[6.71, 1, 2], [6.89, 1, 7], [7.48, 1, 1], [7.99, 4, 31], [8.39, 1, 9]]
        flat = lambda x: round(x - 4.0, 2)
        p = ps.decide(6.89, 6.89, layers, None, 24, 10, 20, flat)
        self.assertEqual((p['action'], p['target']), ('raise', 7.98))
        # 売れ行きが遅く在庫が多すぎる(3個/20日→160日分)なら上げない
        self.assertEqual(ps.decide(6.89, 6.89, layers, None, 24, 3, 20, flat)['action'], 'keep')

    def test_big_tied_layer_means_keep(self):
        # 毎日香を$8.89にした後: 上の$8.99の下に143個(約21日分)
        layers = [[8.48, 1, 1], [8.49, 3, 17], [8.88, 1, 9], [8.89, 7, 116], [8.99, 2, 11]]
        p = ps.decide(8.89, 8.89, layers, 200, 8, 3, 9, lambda x: round(x - 7.7, 2))
        self.assertEqual(p['action'], 'keep')

    def test_overstock_cuts_below_the_cheapest_competitor(self):
        # 在庫60個・30日で6個(在庫あり30日) → 300日分。最安$7.00に2社。市場は月90個
        p = ps.decide(7.5, 7.5, [[7.0, 2, 10], [7.5, 3, 30]], 90, 60, 6, 30, lambda x: round(x - 5.0, 2))
        self.assertEqual((p['action'], p['target']), ('cut', 6.99))
        self.assertIn('最安$7.00', p['reason'])

    def test_no_cut_below_break_even_or_when_stock_is_low(self):
        layers = [[7.0, 2, 10]]
        self.assertEqual(ps.decide(7.5, 7.5, layers, 90, 60, 6, 30, lambda x: round(x - 7.0, 2))['action'], 'keep')
        self.assertNotEqual(ps.decide(7.5, 7.5, layers, 90, 5, 0, 30, lambda x: round(x - 5.0, 2))['action'], 'cut')

    def test_cut_is_capped(self):
        p = ps.decide(10.0, 10.0, [[5.0, 1, 50]], 300, 80, 2, 30, lambda x: round(x - 4.0, 2))
        self.assertEqual(p['target'], 9.0)

    def test_already_cheapest_without_sales_is_a_feedback_problem(self):
        # キティ定規: 最安と同額で在庫あり20日・0個
        p = ps.decide(6.4, 7.01, [[7.01, 4, 20], [7.49, 2, 266]], 50, 29, 0, 20, lambda x: round(x - 5.0, 2))
        self.assertEqual(p['action'], 'feedback')

    def test_no_cost_means_keep(self):
        self.assertEqual(ps.decide(7.0, 7.0, [[8.0, 1, 1]], 100, 5, 1, 10, lambda x: None)['action'], 'keep')


class TestOfferLayers(unittest.TestCase):
    def test_groups_live_new_offers_and_excludes_ours(self):
        product = {'liveOffersOrder': [0, 1, 2, 3], 'offers': [
            {'sellerId': 'ME', 'condition': 1, 'offerCSV': [1, 899, 0], 'stockCSV': [1, 5]},
            {'sellerId': 'A', 'condition': 1, 'offerCSV': [1, 899, 0], 'stockCSV': [1, 7]},
            {'sellerId': 'B', 'condition': 1, 'offerCSV': [1, 849, 100], 'stockCSV': [1, 3]},
            {'sellerId': 'C', 'condition': 2, 'offerCSV': [1, 500, 0], 'stockCSV': [1, 9]},   # 中古
            {'sellerId': 'D', 'condition': 1, 'offerCSV': [1, 100, 0], 'stockCSV': [1, 9]},   # 出品終了
        ]}
        self.assertEqual(ps._offer_layers(product, 'ME'), [[8.99, 1, 7], [9.49, 1, 3]])


if __name__ == '__main__':
    unittest.main()
