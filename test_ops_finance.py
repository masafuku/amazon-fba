import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ops_finance
from ops_finance import (
    _apply_priority_fields,
    _classify_priority_tier,
    _excluded_kind,
    _gated_brand,
    _is_media,
    _priority_rejection_reason,
    _is_searchable_keyword,
    _shipping_cost_jpy_for_weight,
    calc_unit_profit,
    evaluate_mcp_candidates,
    is_qualified_priority_tier,
    normalize_jp_cost_for_tax,
    build_daily_digest_message,
    load_asins_needing_listing_check,
    load_digest_window,
    save_listing_status,
    pick_next_keyword,
)


class TestCalcUnitProfitShipping(unittest.TestCase):
    """国際送料の想定(CEO: 「国際便なので一万円くらいはしそうです。ただし何個纏めて
    仕入れるかで損益分岐点が変わらそうです」→ 実際のフォワーダー見積もり
    (グローバルブランド社、容積重量7.20kgでFedEx International Economy実質
    ¥13,034)を受けて重量連動モデルに改訂)。「まとめ買い個数」は引き続きJP原価から
    逆算する(総額が概ね¥50,000になる個数)が、送料自体は「その個数分の合計重量」を
    EMS料金表ベース(実見積もりでスケール済み)に当てはめて計算する。
    """

    def test_shipping_cost_is_zero(self):
        # CEO指示(2026-09-22): 想定輸送費は妥当でないため0円として扱う
        for price, cost, weight in [(50.0, 300.0, 0.1), (200.0, 60000.0, 2.0), (50.0, 1000.0, 0.01)]:
            result = calc_unit_profit(us_price_usd=price, jp_cost_jpy=cost, weight_kg=weight)
            self.assertEqual(result['shipping_cost_usd'], 0.0)

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


class TestIsQualifiedPriorityTier(unittest.TestCase):
    """2026-09-28、tier(pass/consider/reference/reject)をpriority_tierに統合。
    qualified = priority_tier in (S/A+/A-/B+/B-/C+)。C-(赤字)・D(実売証拠なし)・
    None(完全除外品)は不合格。"""

    def test_qualified_tiers(self):
        for tier in ('S', 'A+', 'A-', 'B+', 'B-', 'C+'):
            self.assertTrue(is_qualified_priority_tier(tier), tier)

    def test_unqualified_tiers(self):
        for tier in ('C-', 'D', None):
            self.assertFalse(is_qualified_priority_tier(tier), tier)


class TestPriorityRejectionReason(unittest.TestCase):
    """不合格理由の文言。利益率には一切言及しない(2026-09-28、安全弁廃止)。"""

    def test_qualified_entry_has_no_reason(self):
        entry = {'priority_tier': 'C+', 'roi_pct': 0.1}
        self.assertIsNone(_priority_rejection_reason(entry))

    def test_negative_roi_mentions_deficit(self):
        entry = {'priority_tier': 'C-', 'roi_pct': -0.2}
        reason = _priority_rejection_reason(entry)
        self.assertIn('赤字', reason)
        self.assertNotIn('利益率', reason)

    def test_no_evidence_mentions_sales_basis(self):
        entry = {'priority_tier': 'D', 'roi_pct': 0.5, 'monthly_sold': 10, 'sales_rank_drops_30': None}
        reason = _priority_rejection_reason(entry)
        self.assertIn('実売の根拠なし', reason)
        self.assertNotIn('利益率', reason)

    def test_roi_none_mentions_calculation_failure(self):
        entry = {'priority_tier': 'D', 'roi_pct': None}
        reason = _priority_rejection_reason(entry)
        self.assertIn('計算できない', reason)


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
        self.assertTrue(_is_searchable_keyword('Tiger thermos'))  # HARIOは出品制限ブランドのため対象外(TestGatedBrand)
        self.assertTrue(_is_searchable_keyword('Zebra Sarasa'))

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

    def test_figure_and_collectible_keywords_are_excluded(self):
        # CEO: 「キーワードサーチの結果がフィギアなどの出品が難しいものが
        # おおい。除外できる?」— ブランドゲーティング/Transparency/ライセンス
        # 許諾で繰り返し行き止まりになったカテゴリ(メモリ「Excluded sourcing
        # categories」参照)。
        for keyword in (
            'Nendoroid Hatsune Miku', 'Funko Pop Vinyl', 'PVC Scale Figure',
            'Gundam Model Kit', 'Pokemon Trading Cards', 'Gacha Capsule Toy',
        ):
            self.assertFalse(_is_searchable_keyword(keyword), keyword)

    def test_tcg_product_titles_without_the_word_card_are_excluded(self):
        # CEO: 「ポケモンカードなどのコレクタブルが残ってる」— 実際の商品
        # タイトルは「trading card」ではなく製品形態名(Booster Pack/Box等)や
        # ブランド名そのもので検索結果に紛れ込んでいたため、それらも追加。
        for keyword in (
            'Pokemon Scarlet & Violet Booster Box',
            'Pokemon TCG Elite Trainer Box',
            'Yu-Gi-Oh! Legendary Duelists Booster Pack',
            'Pokemon Cards Bulk Lot 100 Cards',
            'Magic The Gathering Commander Deck',
        ):
            self.assertFalse(_is_searchable_keyword(keyword), keyword)


class TestEvaluateMcpCandidatesExclusions(unittest.TestCase):
    """商品タイトル段階の除外。食品・医薬品/化粧品・包丁類(輸出に課題があるため
    完全除外)は評価の前に弾く。フィギュア/コレクタブルは、CEO指示(2026-09-26)で
    優先度Tierを付けるようになったため、弾かずに評価し is_figure の印だけを付ける。
    ブランド名("Sanrio"等)のような広いキーワードで検索した結果に紛れ込むケースに
    対応する(キーワード自体は問題なくてもタイトルで判定)。"""

    def _candidate(self, title, asin='B0TEST0001', **sell_overrides):
        sell = {
            'asin': asin, 'title': title, 'price': 20.0, 'weight_kg': 0.1,
            'referral_fee_percent': 15.0, 'fba_pickpack_fee': 3.0,
            'monthly_sold': 100, 'sales_rank': 5000,
        }
        sell.update(sell_overrides)
        return {
            'sell': sell,
            'cost': {'price': 1000, 'asin': 'JP123'},
            'price_diff_rate': 0.5,
        }

    def test_figure_titled_candidate_is_evaluated_and_flagged(self):
        result = evaluate_mcp_candidates({
            'candidates': [self._candidate('Sanrio Hello Kitty Nendoroid Figure')],
        })
        entries = result['qualified'] + result['rejected']
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertTrue(entry['is_figure'])
        self.assertIsNone(entry['excluded_kind'])
        self.assertIn(entry['priority_tier'], ('S', 'A+', 'A-', 'B+', 'B-', 'C+', 'C-', 'D'))
        self.assertNotEqual(entry.get('reason'), 'figure_or_collectible')

    def test_ordinary_stationery_candidate_is_not_flagged(self):
        result = evaluate_mcp_candidates({
            'candidates': [self._candidate('Sanrio Hello Kitty Ruler 15cm')],
        })
        entry = (result['qualified'] + result['rejected'])[0]
        self.assertFalse(entry['is_figure'])
        self.assertIsNone(entry['excluded_kind'])
        self.assertIsNotNone(entry['priority_tier'])

    def test_excluded_categories_are_rejected_before_profit_calc(self):
        for title, kind in (
            ('Shun Classic 7" Santoku Knife', 'knife'),
            ('DHC Deep Cleansing Oil Makeup Remover', 'drug_cosmetic'),
            ('Japanese Matcha Green Tea Powder 100g', 'food'),
        ):
            result = evaluate_mcp_candidates({'candidates': [self._candidate(title)]})
            self.assertEqual(result['qualified'], [], title)
            self.assertEqual(len(result['rejected']), 1, title)
            entry = result['rejected'][0]
            self.assertEqual(entry['excluded_kind'], kind, title)
            self.assertEqual(entry['reason'], f'excluded_{kind}', title)
            self.assertIsNone(entry['priority_tier'], title)


class TestEvaluateMcpCandidatesQualifiedBoundary(unittest.TestCase):
    """2026-09-28、qualifiedはpriority_tierから導出する形に統合された。
    利益率(margin_pct)は一切合否に関与しない(旧・安全弁は廃止)。"""

    def _candidate(self, price, jp_cost, monthly_sold=100, asin='B0TEST0002'):
        return {
            'sell': {
                'asin': asin, 'title': 'Ordinary Stationery Item', 'price': price, 'weight_kg': 0.1,
                'referral_fee_percent': 15.0, 'fba_pickpack_fee': 3.0,
                'monthly_sold': monthly_sold, 'sales_rank': 5000,
            },
            'cost': {'price': jp_cost, 'asin': 'JP123'},
            'price_diff_rate': 0.5,
        }

    def test_low_margin_high_roi_with_sales_evidence_is_qualified(self):
        # 利益率13.8%(旧15%安全弁を割っている)だが、ROI27.5%(>=20%)・強い実売(100件)
        # があるので合格する(旧ロジックならmarginで弾かれconsiderだった組み合わせ)
        result = evaluate_mcp_candidates({'candidates': [self._candidate(price=20.0, jp_cost=1500.0)]})
        self.assertEqual(len(result['qualified']), 1)
        self.assertLess(result['qualified'][0]['margin_pct'], 0.15)
        self.assertEqual(result['qualified'][0]['priority_tier'], 'A+')

    def test_no_sales_evidence_is_rejected_regardless_of_roi(self):
        result = evaluate_mcp_candidates({
            'candidates': [self._candidate(price=100.0, jp_cost=1000.0, monthly_sold=None)],
        })
        self.assertEqual(result['qualified'], [])
        self.assertEqual(len(result['rejected']), 1)
        self.assertEqual(result['rejected'][0]['priority_tier'], 'D')
        self.assertIn('実売の根拠なし', result['rejected'][0]['reason'])

    def test_negative_roi_is_rejected(self):
        result = evaluate_mcp_candidates({'candidates': [self._candidate(price=10.0, jp_cost=100000.0)]})
        self.assertEqual(result['qualified'], [])
        self.assertEqual(result['rejected'][0]['priority_tier'], 'C-')
        self.assertIn('赤字', result['rejected'][0]['reason'])

    def test_qualified_list_sorted_by_tier_then_roi(self):
        # 強い実売+高ROI(S) > 強い実売+低ROI(C+程度)の順にはならず、qualifiedのみ抽出、
        # Tier順(S>A+>...)で並ぶことを確認
        low_roi = self._candidate(price=30.0, jp_cost=2500.0, asin='B0LOWROI01')  # 実売あり、ROI低め
        high_roi = self._candidate(price=200.0, jp_cost=3000.0, asin='B0HIGHROI1')  # 実売あり、ROI高め
        result = evaluate_mcp_candidates({'candidates': [low_roi, high_roi]})
        asins = [e['asin'] for e in result['qualified']]
        self.assertEqual(asins[0], 'B0HIGHROI1')

    def test_coarse_skip_never_qualifies_even_with_good_tier(self):
        # 粗選別で落ちた行は、価格が両方揃ってpriority_tierが合格範囲になっても
        # qualifiedには入らない(evaluate_mcp_candidates()の意図的な例外)
        skip = {
            'asin': 'B0SKIPPED1', 'title': 'Skipped Item', 'price': 200.0, 'jp_price': 3000.0,
            'weight_kg': 0.1, 'monthly_sold': 100, 'sales_rank': 5000,
            'reason': 'price diff rate too low',
        }
        result = evaluate_mcp_candidates({'candidates': [], 'skipped': [skip]})
        self.assertEqual(result['qualified'], [])
        self.assertEqual(len(result['rejected']), 1)
        self.assertTrue(result['rejected'][0]['reason'].startswith('粗選別で除外'))


class TestClassifyPriorityTier(unittest.TestCase):
    """発注の優先度Tier(S/A+/A-/B+/B-/C+/C-/D、2026-09-28再設計、ROI分割は
    2026-09-28にCEO判断で50%から20%へ引き下げ)。
    新シグネチャ: _classify_priority_tier(roi_pct, monthly_sold, sales_rank_drops_30)。
    「実売の確度」(強い実売>=100件 / 実売あり30-99件 / ランク変動のみ>=10回 / 実売なし)と
    「ROI水準」(>=100% / 20-100% / 0-20% / 赤字)の4x4マトリクスで決まる。"""

    # --- 4x4マトリクスの全16セル ---
    def test_strong_evidence_row(self):
        self.assertEqual(_classify_priority_tier(1.5, 150, None), 'S')
        self.assertEqual(_classify_priority_tier(0.7, 150, None), 'A+')
        self.assertEqual(_classify_priority_tier(0.1, 150, None), 'C+')
        self.assertEqual(_classify_priority_tier(-0.3, 150, None), 'C-')

    def test_real_evidence_row(self):
        self.assertEqual(_classify_priority_tier(1.5, 50, None), 'A+')
        self.assertEqual(_classify_priority_tier(0.7, 50, None), 'A-')
        self.assertEqual(_classify_priority_tier(0.1, 50, None), 'C+')
        self.assertEqual(_classify_priority_tier(-0.3, 50, None), 'C-')

    def test_rank_evidence_row(self):
        self.assertEqual(_classify_priority_tier(1.5, None, 15), 'B+')
        self.assertEqual(_classify_priority_tier(0.7, None, 15), 'B-')
        self.assertEqual(_classify_priority_tier(0.1, None, 15), 'C+')
        self.assertEqual(_classify_priority_tier(-0.3, None, 15), 'C-')

    def test_no_evidence_row_is_always_d(self):
        # 実売なしはROIがどれだけ良くてもD
        self.assertEqual(_classify_priority_tier(1.5, None, None), 'D')
        self.assertEqual(_classify_priority_tier(0.7, None, None), 'D')
        self.assertEqual(_classify_priority_tier(0.1, None, None), 'D')
        self.assertEqual(_classify_priority_tier(-0.3, None, None), 'D')

    # --- 実売の確度の境界値 ---
    def test_monthly_sold_boundary_100_is_strong_99_is_real(self):
        self.assertEqual(_classify_priority_tier(0.7, 100, None), 'A+')  # strong+50-100% -> A+
        self.assertEqual(_classify_priority_tier(0.7, 99, None), 'A-')   # real+50-100% -> A-

    def test_monthly_sold_boundary_30_is_real_29_is_no_evidence(self):
        self.assertEqual(_classify_priority_tier(3.0, 30, None), 'A+')
        self.assertEqual(_classify_priority_tier(3.0, 29, None), 'D')

    def test_rank_drops_boundary_10_is_evidence_9_is_not(self):
        self.assertEqual(_classify_priority_tier(1.5, None, 10), 'B+')
        self.assertEqual(_classify_priority_tier(1.5, None, 9), 'D')

    def test_real_monthly_sold_below_30_does_not_fall_back_to_rank(self):
        # monthly_soldが実数値(20)である以上、たとえsales_rank_drops_30が高くても
        # ランク推定にはフォールバックしない(実数値の方を優先して「実売なし」扱い)
        self.assertEqual(_classify_priority_tier(3.0, 20, 50), 'D')

    # --- ROIの境界値 ---
    def test_roi_boundary_100_percent(self):
        self.assertEqual(_classify_priority_tier(1.0, 150, None), 'S')
        self.assertEqual(_classify_priority_tier(0.9999, 150, None), 'A+')

    def test_roi_boundary_20_percent(self):
        self.assertEqual(_classify_priority_tier(0.2, 150, None), 'A+')
        self.assertEqual(_classify_priority_tier(0.1999, 150, None), 'C+')

    def test_roi_boundary_zero(self):
        self.assertEqual(_classify_priority_tier(0.0, 150, None), 'C+')
        self.assertEqual(_classify_priority_tier(-0.0001, 150, None), 'C-')

    def test_roi_none_is_d(self):
        self.assertEqual(_classify_priority_tier(None, 150, None), 'D')
        self.assertEqual(_classify_priority_tier(None, None, None), 'D')

    # --- Sの利益$3フロアが撤廃されたことの確認 ---
    def test_s_has_no_profit_floor_anymore(self):
        # 強い実売+ROI>=100%であれば、利益額そのものは判定に一切関与しない
        self.assertEqual(_classify_priority_tier(1.2, 100, None), 'S')

    # --- メディアの特別扱いが撤廃されたことの確認(is_media引数自体が無い) ---
    def test_apply_priority_fields_no_longer_special_cases_media(self):
        entry = {'asin': '4088737687', 'title': 'One Piece Vol 36 (Japanese Edition)',
                 'roi_pct': 2.77, 'monthly_sold': None, 'sales_rank_drops_30': 0}
        _apply_priority_fields(entry)
        # ランク変動0回は基準(>=10)未満なので実売なし扱い -> D(メディアだから、ではない)
        self.assertEqual(entry['priority_tier'], 'D')

        entry2 = {'asin': '4088737688', 'title': 'One Piece Vol 37 (Japanese Edition)',
                  'roi_pct': 2.77, 'monthly_sold': None, 'sales_rank_drops_30': 15}
        _apply_priority_fields(entry2)
        # メディアでもランク変動15回(>=10)あれば、雑貨と同じくB系として扱われる
        self.assertEqual(entry2['priority_tier'], 'B+')


class TestExcludedKind(unittest.TestCase):
    """食品・医薬品/化粧品・包丁類(CEO: 「食品、医薬品、刃物は輸出に課題が
    あるので完全除外」)。はさみ・カッターは除外しない。"""

    def test_knives_are_excluded(self):
        for title in ('Shun Premier Grey 8" Chef\'s Knife', 'Global G-2 Santoku Knife 18cm', 'Kitchen Knife Set 3 Pieces'):
            self.assertEqual(_excluded_kind(title, is_title=True), 'knife', title)

    def test_scissors_are_not_excluded(self):
        # 発注済みの万能分別はさみ(Tier S)など、はさみ・文房具のカッターは除外しない
        for title in (
            '万能分別はさみ(サンスター文具)', 'Sun-Star Stationery All-Purpose Sorting Scissors',
            'Kokuyo Campus Scissors', 'OLFA Cutter Blade Refill',
        ):
            self.assertIsNone(_excluded_kind(title, is_title=True), title)

    def test_utility_knives_and_cutters_are_not_excluded(self):
        # CEO: 刃物は包丁・ナイフ類のみ。カッター(OLFA/NT等)は除外しない。
        for title in (
            'OLFA 25mm Extra Heavy-Duty Utility Knife (H-1)', 'NT Cutter Heavy Duty Cartridge Knife',
            'KAI 18mm Snap-off Utility Knife Blades, 20-Pack', 'Craft Knife Precision Hobby Set',
            'Yoshikawa Butter Knife Fine Butter Sharping',
        ):
            self.assertIsNone(_excluded_kind(title, is_title=True), title)

    def test_knife_block_alone_is_not_excluded_but_a_knife_block_set_is(self):
        self.assertIsNone(_excluded_kind('Shun Bamboo Knife Block, 22-Slot', is_title=True))
        self.assertEqual(_excluded_kind('Shun Classic 6-Piece Knife Block Set | Chef\'s, Paring', is_title=True), 'knife')
        self.assertIsNone(_excluded_kind('Shun Knife Care Kit', is_title=True))

    def test_hazmat_flammable_products_are_excluded(self):
        # CEO確認 2026-09-27: SOFT99 ガラコ ロールオンはSDSで引火性液体(H225)。
        self.assertEqual(
            _excluded_kind('SOFT99 Glaco Roll On Large - Water-Beading Glass Sealant - 120 ml', is_title=True),
            'hazmat',
        )
        self.assertEqual(_excluded_kind('SOFT99 Glaco'), 'hazmat')   # 検索キーワード側でも弾く

    def test_quasi_drug_bath_products_are_excluded_as_drug_cosmetic(self):
        # CEO確認 2026-09-27: 花王バブは医薬部外品。
        self.assertEqual(
            _excluded_kind('Kao Babu Bath - BAB Piece Full herb 12 Tablets Input', is_title=True),
            'drug_cosmetic',
        )

    def test_ordinary_liquid_detergent_is_not_excluded(self):
        # サラサーティは非危険物・医薬部外品でもない(危険物の類別: 非危険物と確認済み)。
        self.assertIsNone(
            _excluded_kind('Sarasaty Lingerie Detergent (1) , 4.05 Fl Oz (Pack of 1)', is_title=True)
        )

    def test_figures_named_after_food_are_not_excluded(self):
        # 本番のドライランで、食品語を含むフィギュアが完全除外に入っていた誤検知
        for title in (
            'Furyu Hatsune Miku Noodle Stopper Figure -Vintage Doll Style-',
            'Sonny Angel Snack Series - 1 Sealed Blind Box - Original Limited Edition Mini Figure',
        ):
            self.assertIsNone(_excluded_kind(title, is_title=True), title)

    def test_non_consumables_with_consumable_words_are_not_excluded(self):
        for title in (
            'Eye Up Sushi Stacking Game - Ages 3+', 'Kaneshotouki Pokemon Ramen Bowl 15 cm',
            'Luminara Candy Heart Kiss Me Fresh Mint Candle', 'Face Reading in Chinese Medicine',
            'LayLax ARMOR FACE GUARD Polycarbonate Hard Face Mask for Survival Game',
            'Sanrio Hello Kitty Hair Bang Clips, Pink ABS Resin, Makeup Hair Clip',
            'THE BREATHER Natural Breathing Exerciser Trainer For Drug-Free Respiratory Therapy',
        ):
            self.assertIsNone(_excluded_kind(title, is_title=True), title)

    def test_knife_accessories_are_not_excluded(self):
        for title in ('Knife Sharpener Whetstone 1000/3000', 'Knife Holder Magnetic Strip', 'Chef Knife Sheath Cover'):
            self.assertIsNone(_excluded_kind(title, is_title=True), title)

    def test_drug_and_cosmetic_are_excluded(self):
        for title in (
            'DHC Deep Cleansing Oil, Makeup Remover', 'Muji Sensitive Skin Lotion 400ml',
            'Sangi APAGARD Toothpaste for Sensitive Teeth', 'Milbon Smoothing Shampoo 6.8 oz',
            'DHC Vitamin C Supplement 60 Days',
        ):
            self.assertEqual(_excluded_kind(title, is_title=True), 'drug_cosmetic', title)

    def test_food_is_excluded(self):
        for title in ('Kikkoman Soy Sauce 1L', 'Meiji Chocolate Assorted Snack Pack', 'Uji Matcha Powder 100g'):
            self.assertEqual(_excluded_kind(title, is_title=True), 'food', title)

    def test_utensils_named_after_food_or_cosmetics_are_not_excluded(self):
        # 現在の広い語のリストをそのままタイトルに当てると誤検知していた実例
        for title in (
            'Puozult Digital Kitchen Scale 30kg Large Food Scale', 'RETTBERG Tea Kettle for Stovetop Induction',
            'Acrylic Floating Shelves Shower Shelf Shampoo Conditioner Holder', 'Yamazen Rice Cooker 0.5-1.5 go',
            'Kureha Seaguar Fluorocarbon Fishing Line',
        ):
            self.assertIsNone(_excluded_kind(title, is_title=True), title)

    def test_keyword_mode_uses_broad_food_list(self):
        self.assertEqual(_excluded_kind('Japan snacks'), 'food')
        self.assertEqual(_excluded_kind('Japanese kitchen knife'), 'knife')
        self.assertEqual(_excluded_kind('Shun knife'), 'knife')
        self.assertEqual(_excluded_kind('Japanese skincare'), 'drug_cosmetic')
        self.assertIsNone(_excluded_kind('Zebra Sarasa'))

    def test_cosmetic_brand_keywords_are_excluded_from_search_but_not_from_titles(self):
        for keyword in ('DHC', 'Biore', 'CANMAKE', 'Hada Labo', 'Kanebo', 'Kose', 'SK-II', 'Bihada Ichizoku'):
            self.assertEqual(_excluded_kind(keyword), 'drug_cosmetic', keyword)
        # タイトルではブランド名だけで除外しない(例: 「KOSE」名義の非化粧品を巻き込まない)
        self.assertIsNone(_excluded_kind('Kose Ceramic Storage Jar', is_title=True))

    def test_empty_text_is_not_excluded(self):
        self.assertIsNone(_excluded_kind(None))
        self.assertIsNone(_excluded_kind(''))


class TestGatedBrand(unittest.TestCase):
    """出品制限ブランド(CEOがSeller Centralで確認: タカラトミー 2026-09-26、HARIO 2026-09-27)。"""

    def test_hario_and_takara_tomy_are_detected(self):
        self.assertEqual(_gated_brand('Hario V60 "Buono" Drip Kettle Stovetop Gooseneck'), 'HARIO')
        self.assertEqual(_gated_brand('ハリオ V60 ドリッパー'), 'HARIO')
        self.assertEqual(_gated_brand('Takara Tomy Beyblade X UX-21 Hell Nether Deck Set'), 'タカラトミー')
        self.assertEqual(_gated_brand('some title', 'タカラトミー(TAKARA TOMY)'), 'タカラトミー')
        self.assertEqual(_gated_brand('MUJI Smooth Gel Ink Pen 0.5mm'), '無印良品(MUJI)')
        self.assertEqual(_gated_brand('無印良品 ステンレスユニットシェルフ'), '無印良品(MUJI)')

    def test_similar_words_are_not_detected(self):
        self.assertIsNone(_gated_brand('Super Mario Kitchen Timer'))
        self.assertIsNone(_gated_brand('Zebra Sarasa 0.5mm'))
        self.assertIsNone(_gated_brand('Mujigae Rainbow Notebook'))
        self.assertIsNone(_gated_brand(None, ''))

    def test_gated_brand_keywords_are_not_searchable(self):
        for keyword in ('HARIO', 'Hario V60', 'Hario dripper', 'Takara Tomy', 'Beyblade X', 'MUJI', '無印良品'):
            self.assertFalse(_is_searchable_keyword(keyword), keyword)
        self.assertTrue(_is_searchable_keyword('Zojirushi'))


class TestIsMedia(unittest.TestCase):
    def test_isbn_asin_is_a_book(self):
        self.assertTrue(_is_media('4088737687', 'One Piece Vol 36 (Japanese Edition)'))
        self.assertTrue(_is_media('456789012X', None))

    def test_dvd_cd_bluray_titles_are_media(self):
        for asin, title in (
            ('B00439G0SA', 'Lethal Weapon 3 Blu-ray'), ('B0002DCQZW', 'ロード・オブ・ザ・リング [DVD]'),
            ('B07177NDKC', 'Way It Is (Bonus Track)'), ('B06XCLTQVQ', 'Collection (Shm)'),
            ('B008YPK7LK', 'Movie - Happy Feet Two [Japan DVD]'),
        ):
            self.assertTrue(_is_media(asin, title), title)

    def test_ordinary_goods_are_not_media(self):
        for asin, title in (
            ('B09KTQ28X5', 'Shimomura ASC-733 Sharp Cabbage Peeler'), ('B07MDFFCZ3', 'Smooth Gel Ink Ballpoint Pen 0.5mm'),
            ('B0CNKCK41P', 'Sanrio Hello Kitty Ruler 15cm'), ('B004O7GLL2', "Holbein Artists' Watercolor 15ml Titanium White"),
            ('B0FQC3YVP7', 'Manga Drawing Pen Set G-Pen'),
        ):
            self.assertFalse(_is_media(asin, title), title)


class TestLoadDigestWindow(unittest.TestCase):
    """朝/夜のLINE通知: 優先度Tier S/A+/A-/B+ だけを、メディア・出品制限ブランド・フィギュア・
    完全除外を除いて、Tier順に並べる(2026-09-28 Tier再設計後)。"""

    def _insert(self, conn, asin, title, priority_tier, roi, tier='pass', is_figure=0, excluded_kind=None, brand=None):
        conn.execute(
            "INSERT INTO agent_candidates (run_id, category, asin, title, qualified, tier, priority_tier, "
            "excluded_kind, is_figure, margin_pct, unit_profit_usd, data_json, created_at) "
            "VALUES ('r1', 'kw', ?, ?, 1, ?, ?, ?, ?, 0.3, 3.0, ?, '2026-09-27T00:00:00+00:00')",
            (asin, title, tier, priority_tier, excluded_kind, is_figure, __import__('json').dumps({'roi_pct': roi, 'brand': brand})),
        )

    def test_filters_and_orders_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.sqlite3'
            with mock.patch.object(ops_finance, 'DB_PATH', db_path):
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    self._insert(conn, 'B0AAAAAAA1', 'Stationery Pen', 'B+', 0.8)
                    self._insert(conn, 'B0AAAAAAA2', 'Kitchen Peeler', 'S', 1.5)
                    self._insert(conn, 'B0AAAAAAA3', 'Desk Ruler', 'A+', 2.0)
                    self._insert(conn, 'B0AAAAAAA4', 'Desk Tape', 'A-', 1.2)
                    self._insert(conn, 'B0AAAAAAA5', 'Some CD (Bonus Track)', 'A+', 3.0)            # メディア
                    self._insert(conn, '4088737687', 'One Piece Vol 36', 'A+', 2.7)                  # 本(ISBN)
                    self._insert(conn, 'B0AAAAAAA6', 'Hario V60 Kettle', 'S', 2.0, brand='HARIO')  # 出品制限ブランド
                    self._insert(conn, 'B0AAAAAAA7', 'Anime Figure', 'A+', 2.0, is_figure=1)         # フィギュア
                    self._insert(conn, 'B0AAAAAAA8', 'Kitchen Knife', 'A+', 2.0, excluded_kind='knife')  # 完全除外
                    self._insert(conn, 'B0AAAAAAA9', 'Low Tier Thing', 'B-', 0.2)                   # 対象Tier外
                    self._insert(conn, 'B0AAAAAA10', 'C Tier Thing', 'C+', 9.0)                      # 対象Tier外
                candidates, _keywords, _since = load_digest_window('2026-09-26T00:00:00+00:00')
        self.assertEqual([c['asin'] for c in candidates], ['B0AAAAAAA2', 'B0AAAAAAA3', 'B0AAAAAAA4', 'B0AAAAAAA1'])
        self.assertEqual([c['priority_tier'] for c in candidates], ['S', 'A+', 'A-', 'B+'])


class TestListingStatus(unittest.TestCase):
    """SP-APIで照会した出品可否の保存・照会対象の選定・通知への反映。"""

    def _insert(self, conn, asin, title, priority_tier, listing_status=None, checked_at=None, brand=None):
        conn.execute(
            "INSERT INTO agent_candidates (run_id, category, asin, title, qualified, tier, priority_tier, "
            "margin_pct, unit_profit_usd, listing_status, listing_checked_at, data_json, created_at) "
            "VALUES ('r1', 'kw', ?, ?, 1, 'pass', ?, 0.3, 3.0, ?, ?, ?, '2026-09-27T00:00:00+00:00')",
            (asin, title, priority_tier, listing_status, checked_at, __import__('json').dumps({'roi_pct': 1.5, 'brand': brand})),
        )

    def test_selects_unchecked_and_stale_top_tier_asins_only(self):
        fresh = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        stale = '2026-09-01T00:00:00+00:00'
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.sqlite3'
            with mock.patch.object(ops_finance, 'DB_PATH', db_path):
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    self._insert(conn, 'B0AAAAAAA1', 'Pen', 'A+')                                # 未確認 -> 対象
                    self._insert(conn, 'B0AAAAAAA2', 'Ruler', 'S', 'ok', stale)                  # 古い -> 対象
                    self._insert(conn, 'B0AAAAAAA3', 'Tape', 'A+', 'ok', fresh)                   # 新しい -> 対象外
                    self._insert(conn, 'B0AAAAAAA4', 'Low', 'B-')                                # Tier対象外
                    self._insert(conn, '4088737687', 'Book', 'A+')                                # メディア
                    self._insert(conn, 'B0AAAAAAA5', 'Hario Kettle', 'S', brand='HARIO')        # 出品制限ブランド
                asins = load_asins_needing_listing_check(limit=10, max_age_days=7)
        self.assertEqual(asins, ['B0AAAAAAA2', 'B0AAAAAAA1'])   # S が先

    def test_all_tiers_includes_every_non_excluded_asin(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.sqlite3'
            with mock.patch.object(ops_finance, 'DB_PATH', db_path):
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    self._insert(conn, 'B0AAAAAAA1', 'Pen', 'A')
                    self._insert(conn, 'B0AAAAAAA2', 'Low', 'B-')
                    self._insert(conn, 'B0AAAAAAA3', 'Low C', 'C')
                    self._insert(conn, '4088737687', 'Book', 'C')                                # メディア: 全件では含める
                    self._insert(conn, 'B0AAAAAAA5', 'Hario Kettle', 'S', brand='HARIO')        # 出品制限ブランド: 含める
                    self._insert(conn, 'B0AAAAAAA8', 'Kitchen Knife', 'A')
                    conn.execute("UPDATE agent_candidates SET excluded_kind='knife' WHERE asin='B0AAAAAAA8'")
                asins = load_asins_needing_listing_check(limit=100, max_age_days=7, all_tiers=True)
        self.assertEqual(sorted(asins), sorted(['B0AAAAAAA1', 'B0AAAAAAA2', 'B0AAAAAAA3', '4088737687', 'B0AAAAAAA5']))
        self.assertEqual(asins[0], 'B0AAAAAAA5')   # S が先

    def test_save_listing_status_updates_all_rows_of_the_asin(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.sqlite3'
            with mock.patch.object(ops_finance, 'DB_PATH', db_path):
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    self._insert(conn, 'B0AAAAAAA1', 'Pen', 'A')
                    self._insert(conn, 'B0AAAAAAA1', 'Pen', 'A')
                self.assertEqual(save_listing_status('B0AAAAAAA1', 'approval_required'), 2)
                with sqlite3.connect(db_path) as conn:
                    rows = conn.execute("SELECT listing_status, listing_checked_at FROM agent_candidates").fetchall()
        self.assertTrue(all(status == 'approval_required' and checked for status, checked in rows))

    def test_digest_skips_product_approval_and_not_eligible_but_keeps_brand_only(self):
        # CEO: 「ブランド申請は出品にはほぼ全てあるので、隠す設定は不要」(2026-09-27) -
        # approval_required(ブランドの承認のみ)は、通知から除外しない。
        # product_approval_required / brand_and_product_approval_required(Transparency等)・
        # not_eligibleは、引き続き除外する。
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.sqlite3'
            with mock.patch.object(ops_finance, 'DB_PATH', db_path):
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    self._insert(conn, 'B0AAAAAAA1', 'Listable Pen', 'A+', 'ok')
                    self._insert(conn, 'B0AAAAAAA2', 'Unchecked Ruler', 'A+')
                    self._insert(conn, 'B0AAAAAAA3', 'Brand Approval Only', 'S', 'approval_required')
                    self._insert(conn, 'B0AAAAAAA4', 'Not Eligible', 'S', 'not_eligible')
                    self._insert(conn, 'B0AAAAAAA5', 'Needs Transparency', 'S', 'product_approval_required')
                    self._insert(conn, 'B0AAAAAAA6', 'Needs Both', 'S', 'brand_and_product_approval_required')
                candidates, _k, _s = load_digest_window('2026-09-26T00:00:00+00:00')
        self.assertEqual(
            sorted(c['asin'] for c in candidates), ['B0AAAAAAA1', 'B0AAAAAAA2', 'B0AAAAAAA3'],
        )
        message = build_daily_digest_message(candidates, [], '朝の')
        self.assertIn('出品: 可', message)
        self.assertIn('出品: 未確認', message)
        self.assertIn('出品: 要承認(ブランド)', message)


class TestBuildDailyDigestMessage(unittest.TestCase):
    def test_message_shows_tier_and_demand_fields(self):
        item = {
            'asin': 'B09KTQ28X5', 'title': 'Shimomura ASC-733 Sharp Cabbage Peeler', 'us_url': 'https://www.amazon.com/dp/B09KTQ28X5',
            'jp_url': None, 'us_price_usd': 21.38, 'jp_cost_jpy': 1001.0, 'sales_rank': 69765, 'review_count': 9,
            'margin_pct': 0.333, 'unit_profit_usd': 7.13, 'weight_estimated': False, 'category': 'kw', 'tier': 'pass',
            'priority_tier': 'A+', 'monthly_sold': 50, 'roi_pct': 1.07, 'competitor_seller_count': 34,
        }
        message = build_daily_digest_message([item], ['Muji pen case'], '朝の')
        self.assertIn('【Tier A+】', message)
        self.assertIn('ROI: 107%', message)
        self.assertIn('先月の購入: 50 / 競合(出品者): 34', message)
        self.assertIn('S: 0件 / A+: 1件 / A-: 0件 / B+: 0件', message)
        self.assertNotIn('【合格】', message)

    def test_empty_message(self):
        self.assertIn('優先度Tier S/A+/A-/B+ の候補はありませんでした', build_daily_digest_message([], [], '朝の'))


class TestApplyPriorityFields(unittest.TestCase):
    def test_excluded_entry_gets_no_priority_tier(self):
        entry = {'title': 'Shun Classic Santoku Knife', 'unit_profit_usd': 50.0, 'roi_pct': 3.0,
                 'monthly_sold': 100, 'sales_rank': 100}
        _apply_priority_fields(entry)
        self.assertEqual(entry['excluded_kind'], 'knife')
        self.assertIsNone(entry['priority_tier'])
        self.assertFalse(entry['is_figure'])

    def test_figure_entry_keeps_its_priority_tier(self):
        entry = {'title': 'Good Smile Nendoroid Hatsune Miku', 'unit_profit_usd': 10.0, 'roi_pct': 2.0,
                 'monthly_sold': 100, 'sales_rank': 100}
        _apply_priority_fields(entry)
        self.assertTrue(entry['is_figure'])
        self.assertIsNone(entry['excluded_kind'])
        self.assertEqual(entry['priority_tier'], 'S')


class TestPickNextKeywordSkipsExcluded(unittest.TestCase):
    def test_skips_figure_food_and_knife_keywords_already_in_the_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.sqlite3'
            with mock.patch.object(ops_finance, 'DB_PATH', db_path):
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    pool = [
                        ('Sofubi figure', 0), ('Kaiyodo Revoltech', 0), ('Japanese kitchen knife', 0),
                        ('Japan snacks', 0), ('Tamashii Nations', 0), ('Kokuyo notebook', 1),
                    ]
                    for keyword, times_used in pool:
                        conn.execute(
                            "INSERT INTO keyword_pool (keyword, source, added_at, times_used, status) "
                            "VALUES (?, 'manual', '2026-09-01T00:00:00+00:00', ?, 'active')",
                            (keyword, times_used),
                        )
                keyword, _price_min = pick_next_keyword()
        self.assertEqual(keyword, 'Kokuyo notebook')

    def test_returns_none_when_every_active_keyword_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.sqlite3'
            with mock.patch.object(ops_finance, 'DB_PATH', db_path):
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "INSERT INTO keyword_pool (keyword, source, added_at, times_used, status) "
                        "VALUES ('Banpresto', 'manual', '2026-09-01T00:00:00+00:00', 0, 'active')"
                    )
                self.assertEqual(pick_next_keyword(), (None, None))


if __name__ == '__main__':
    unittest.main()


class TestTnkShippingEstimate(unittest.TestCase):
    """依頼があったときだけ使うTNK実運賃の試算(calc_unit_profitの自動判定には使わない)。"""

    def test_cheapest_bracket_uses_the_30kg_per_kg_rate(self):
        result = ops_finance.estimate_tnk_shipping_cost_jpy_per_unit(0.17)
        self.assertAlmostEqual(result['rate_jpy_per_kg'], 938.3, places=1)
        self.assertAlmostEqual(result['per_unit_jpy'], 0.17 * 938.3, places=1)

    def test_standalone_mode_interpolates_the_real_table(self):
        result = ops_finance.estimate_tnk_shipping_cost_jpy_per_unit(1.0, use_cheapest_bracket=False)
        self.assertEqual(result['per_unit_jpy'], 4409)   # 表の1.0kgの実測値そのもの
        mid = ops_finance.estimate_tnk_shipping_cost_jpy_per_unit(0.75, use_cheapest_bracket=False)
        self.assertAlmostEqual(mid['per_unit_jpy'], (4080 + 4409) / 2, places=1)   # 0.5と1.0の中点

    def test_standalone_mode_extrapolates_beyond_the_table(self):
        result = ops_finance.estimate_tnk_shipping_cost_jpy_per_unit(40.0, use_cheapest_bracket=False)
        self.assertGreater(result['per_unit_jpy'], 28149)   # 30kgの実測値より高い
