import unittest

from ops_finance import (
    _classify_tier,
    _is_searchable_keyword,
    _shipping_cost_jpy_for_weight,
    calc_unit_profit,
)


class TestCalcUnitProfitShipping(unittest.TestCase):
    """国際送料の想定(CEO: 「国際便なので一万円くらいはしそうです。ただし何個纏めて
    仕入れるかで損益分岐点が変わらそうです」→ 実際のフォワーダー見積もり
    (グローバルブランド社、容積重量7.20kgでFedEx International Economy実質
    ¥13,034)を受けて重量連動モデルに改訂)。「まとめ買い個数」は引き続きJP原価から
    逆算する(総額が概ね¥50,000になる個数)が、送料自体は「その個数分の合計重量」を
    EMS料金表ベース(実見積もりでスケール済み)に当てはめて計算する。
    """

    def test_shipping_scales_with_total_shipment_weight_not_flat(self):
        # 重量が大きく違えば(JP原価が同じでも)送料も変わる - 固定モデルとは逆の性質
        light = calc_unit_profit(us_price_usd=50.0, jp_cost_jpy=1000.0, weight_kg=0.01)
        heavy = calc_unit_profit(us_price_usd=50.0, jp_cost_jpy=1000.0, weight_kg=2.0)
        self.assertLess(light['shipping_cost_usd'], heavy['shipping_cost_usd'])

    def test_cheap_item_splits_shipping_across_many_units(self):
        # JP原価¥300 -> 50000 // 300 = 166個まとめ買い、合計16.6kg分の送料を
        # 166個で按分
        result = calc_unit_profit(us_price_usd=50.0, jp_cost_jpy=300.0, weight_kg=0.1)
        expected_usd = _shipping_cost_jpy_for_weight(166 * 0.1) / 166 / 150.0
        self.assertAlmostEqual(result['shipping_cost_usd'], round(expected_usd, 2), places=2)

    def test_expensive_item_bears_full_shipment_weight_alone(self):
        # JP原価¥60,000は予算¥50,000を超えるため、まとめ買い個数は最低1個 ->
        # その1個(2kg)分の送料を丸ごと負担する
        result = calc_unit_profit(us_price_usd=200.0, jp_cost_jpy=60000.0, weight_kg=2.0)
        expected_usd = _shipping_cost_jpy_for_weight(2.0) / 150.0
        self.assertAlmostEqual(result['shipping_cost_usd'], round(expected_usd, 2), places=2)

    def test_boundary_cost_equals_budget(self):
        # JP原価がちょうど予算(¥50,000)と一致する場合も、まとめ買い個数は1個
        result = calc_unit_profit(us_price_usd=200.0, jp_cost_jpy=50000.0, weight_kg=2.0)
        expected_usd = _shipping_cost_jpy_for_weight(2.0) / 150.0
        self.assertAlmostEqual(result['shipping_cost_usd'], round(expected_usd, 2), places=2)

    def test_custom_shipment_budget_is_honored(self):
        # shipment_budget_jpyを明示的に上書きできること
        result = calc_unit_profit(
            us_price_usd=50.0, jp_cost_jpy=5000.0, weight_kg=0.5,
            shipment_budget_jpy=30_000.0,
        )
        # 30000 // 5000 = 6個まとめ買い -> 合計3.0kg分の送料を6個で按分
        expected_usd = _shipping_cost_jpy_for_weight(6 * 0.5) / 6 / 150.0
        self.assertAlmostEqual(result['shipping_cost_usd'], round(expected_usd, 2), places=2)

    def test_forwarder_quote_calibration_point_is_exact(self):
        # 実見積もり(容積重量7.20kg -> ¥13,034)そのものを回帰テストとして固定する
        self.assertAlmostEqual(_shipping_cost_jpy_for_weight(7.20), 13_034.0, places=1)

    def test_shipping_cost_never_negative_at_zero_weight(self):
        self.assertGreater(_shipping_cost_jpy_for_weight(0.0), 0)


class TestCalcUnitProfitImportDuty(unittest.TestCase):
    """米国関税(CEO宛フォワーダー見積もりメール: 「基本的には通常関税（12.5％）が
    定義」) - JP原価(輸入申告額の代理指標)に対して既定12.5%を計上する。
    """

    def test_default_duty_rate_is_12_5_percent_of_jp_cost(self):
        result = calc_unit_profit(us_price_usd=50.0, jp_cost_jpy=5000.0, weight_kg=0.5)
        jp_cost_usd = 5000.0 / 150.0
        self.assertAlmostEqual(result['import_duty_usd'], round(jp_cost_usd * 0.125, 2), places=2)

    def test_custom_duty_rate_is_honored(self):
        result = calc_unit_profit(
            us_price_usd=50.0, jp_cost_jpy=5000.0, weight_kg=0.5, import_duty_rate=0.0,
        )
        self.assertEqual(result['import_duty_usd'], 0.0)

    def test_duty_reduces_unit_profit(self):
        with_duty = calc_unit_profit(us_price_usd=50.0, jp_cost_jpy=5000.0, weight_kg=0.5)
        without_duty = calc_unit_profit(
            us_price_usd=50.0, jp_cost_jpy=5000.0, weight_kg=0.5, import_duty_rate=0.0,
        )
        self.assertLess(with_duty['unit_profit_usd'], without_duty['unit_profit_usd'])


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


class TestIsSearchableKeyword(unittest.TestCase):
    """食品などFBA輸出に向かないキーワードの除外(CEO: 「食品などfba輸出に
    向かない物は検索から除外してください」)。
    """

    def test_ordinary_brand_keyword_is_searchable(self):
        self.assertTrue(_is_searchable_keyword('HARIO V60'))
        self.assertTrue(_is_searchable_keyword('S.H.Figuarts'))

    def test_food_keywords_are_excluded(self):
        for keyword in ('matcha powder', 'miso paste', 'shio koji', 'Japan snacks', 'onigiri mold'):
            self.assertFalse(_is_searchable_keyword(keyword), keyword)

    def test_partial_match_is_case_insensitive(self):
        self.assertFalse(_is_searchable_keyword('Premium MATCHA Set'))

    def test_amazon_top_level_category_still_excluded(self):
        self.assertFalse(_is_searchable_keyword('Grocery & Gourmet Food'))

    def test_japanese_script_still_excluded(self):
        self.assertFalse(_is_searchable_keyword('ジーショック'))

    def test_word_boundary_avoids_false_positive_substrings(self):
        # 単語境界で判定するため、"tea"を含むが無関係な単語("teak")は
        # 誤って除外されない(単純な部分文字列一致だと事故る典型例)。
        self.assertTrue(_is_searchable_keyword('teak wood furniture'))

    def test_multi_word_phrase_still_matches(self):
        self.assertFalse(_is_searchable_keyword('Kikkoman Soy Sauce'))

    def test_kitchenware_with_food_related_words_is_not_excluded(self):
        # 実際にお気に入り由来のキーワードプールで確認された誤検知:
        # "tea"/"coffee"を単独の語として除外すると、消費物(茶葉・コーヒー豆)
        # ではなく器具(ケトル・メーカー)まで巻き込んでしまう。
        self.assertTrue(_is_searchable_keyword('Tea Kettles'))
        self.assertTrue(_is_searchable_keyword('Pour Over Coffee Makers'))

    def test_actual_tea_and_coffee_consumables_are_excluded(self):
        for keyword in ('Green Tea', 'Loose Tea Leaves', 'Coffee Beans', 'Instant Coffee'):
            self.assertFalse(_is_searchable_keyword(keyword), keyword)


if __name__ == '__main__':
    unittest.main()
