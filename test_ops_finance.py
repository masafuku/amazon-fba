import unittest

from ops_finance import (
    _classify_tier,
    _is_searchable_keyword,
    _shipping_cost_jpy_for_weight,
    calc_unit_profit,
    normalize_jp_cost_for_tax,
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


class TestCalcUnitProfitRoi(unittest.TestCase):
    """ROI(投下資本利益率、CEO: 「今回のケースは、投下資本に対して利益の割合も
    重要ですよね」)- US価格に対する実質利益率(margin_pct)とは分母が異なる、
    JP原価(投下資本)に対する実質利益率。
    """

    def test_roi_pct_uses_jp_cost_as_denominator(self):
        result = calc_unit_profit(us_price_usd=100.0, jp_cost_jpy=3000.0, weight_kg=0.2)
        jp_cost_usd = 3000.0 / 150.0
        # result['unit_profit_usd']は既に丸め済みのため、比較は小数点以下3桁までとする
        # (calc_unit_profit内部ではroi_pctを丸め前のunit_profit_usdから計算しているため)
        self.assertAlmostEqual(
            result['roi_pct'], round(result['unit_profit_usd'] / jp_cost_usd, 4), places=3,
        )

    def test_roi_diverges_from_margin_for_high_price_low_cost_item(self):
        # US価格に対しJP原価が相対的に低い商品(五条悟フィギュアの実例パターン)は
        # margin_pct(対US価格)よりroi_pct(対JP原価)の方がずっと大きくなる
        result = calc_unit_profit(us_price_usd=306.3, jp_cost_jpy=8373.0, weight_kg=0.15)
        self.assertGreater(result['roi_pct'], result['margin_pct'])

    def test_roi_pct_zero_when_jp_cost_is_zero(self):
        result = calc_unit_profit(us_price_usd=50.0, jp_cost_jpy=0.0, weight_kg=0.1)
        self.assertEqual(result['roi_pct'], 0.0)


class TestClassifyTier(unittest.TestCase):
    """合格ラインの多段階化(CEO: 「合格ラインは何段階かに分けてください」)。
    _classify_tier() の境界値を確認する。

    CEO: 「輸出ビジネスだと利益率よりも、ROIの方が適切な指標では？」「利益率は
    15%にしましょう」— ROI(投下資本利益率)を主な合格基準、実質利益率15%を
    安全弁とする二段階ゲート(両方満たして初めてpass)。デフォルトは
    MIN_MARGIN_PCT=0.15, MIN_ROI_PCT=0.50。
    """

    def test_exactly_at_both_thresholds_passes(self):
        self.assertEqual(_classify_tier(0.15, 0.50, 100, 50), 'pass')

    def test_margin_below_threshold_with_good_roi_is_consider_not_pass(self):
        # ROIは基準を満たすが、利益率という安全弁を割っているのでpassにしない
        self.assertEqual(_classify_tier(0.1499, 0.90, 100, 50), 'consider')

    def test_roi_below_threshold_with_good_margin_is_consider_not_pass(self):
        # 利益率は十分だが、ROI(主な合格基準)が基準未満ならpassにしない
        self.assertEqual(_classify_tier(0.30, 0.4999, 100, 50), 'consider')

    def test_roi_none_does_not_pass_even_with_good_margin(self):
        self.assertEqual(_classify_tier(0.30, None, 100, 50), 'consider')

    def test_exactly_zero_margin(self):
        self.assertEqual(_classify_tier(0.0, 0.50, 100, 50), 'consider')

    def test_just_below_zero_margin_with_nonneg_gross(self):
        # margin_pctはマイナスだが、手数料を一切引かない粗差(US-JP)はちょうど0
        self.assertEqual(_classify_tier(-0.01, None, 100, 100), 'reference')

    def test_negative_margin_negative_gross(self):
        self.assertEqual(_classify_tier(-0.01, None, 90, 100), 'reject')

    def test_no_price_data(self):
        self.assertEqual(_classify_tier(None, None, None, None), 'reject')

    def test_custom_thresholds(self):
        self.assertEqual(
            _classify_tier(0.10, 0.30, 100, 50, min_margin_pct=0.10, min_roi_pct=0.30), 'pass',
        )
        self.assertEqual(
            _classify_tier(0.09, 0.30, 100, 50, min_margin_pct=0.10, min_roi_pct=0.30), 'consider',
        )


class TestNormalizeJpCostForTax(unittest.TestCase):
    """CEO: 「課税事業者としては登録されていないとおもいます」「今は税込前提で
    試算してください」— 卸価格(税抜表示が通例)とAmazon JP小売価格(税込表示が
    通例)の税基準を揃えてから比較する normalize_jp_cost_for_tax() を確認する。
    ops_finance.IS_JCT_REGISTERED は現状False(免税事業者)前提。
    """

    def test_wholesale_only_gets_tax_added_when_not_registered(self):
        # 免税事業者: 税抜卸価格に10%上乗せした値になる
        self.assertAlmostEqual(normalize_jp_cost_for_tax(1000, None), 1100)

    def test_jp_amazon_only_stays_as_is_when_not_registered(self):
        # 免税事業者: Amazon JP小売価格は既に税込表示なので調整不要
        self.assertEqual(normalize_jp_cost_for_tax(None, 1000), 1000)

    def test_picks_cheaper_after_normalizing_both(self):
        # 卸税抜700(税込770) vs JP小売税込750 -> 税込770の方が高いのでJP小売750を採用
        self.assertEqual(normalize_jp_cost_for_tax(700, 750), 750)
        # 卸税抜600(税込660) vs JP小売税込750 -> 卸(税込660)の方が安い
        self.assertAlmostEqual(normalize_jp_cost_for_tax(600, 750), 660)

    def test_both_none_returns_none(self):
        self.assertIsNone(normalize_jp_cost_for_tax(None, None))


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
