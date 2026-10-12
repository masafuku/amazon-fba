import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ops_finance
import supplier_pipeline as sp

KEEPA_HEADER = ['ASIN', 'Buy Box: 現在', 'Buy Box: 90 日平均', '売れ筋ランキング: 現在', '売れ筋ランキング: 過去90日間の減少',
                '月間売上トレンド: 先月の購入', 'FBA Pick&Pack 料金', '紹介料％', 'パッケージ: 重さ (g)',
                '新品アイテム数 FBA: 現在', '商品コード: EAN']


def keepa_row(asin, price='$ 12.00', rank='20000', drops='45', monthly='100', fba='$ 2.52', ref='15 %', weight='50'):
    return dict(zip(KEEPA_HEADER, [asin, price, price, rank, drops, monthly, fba, ref, weight, '5', '4900000000000']))


def map_row(asin, wholesale=300):
    return {'sd_id': '1', 'part_no': 'P1', 'wholesale_jpy': wholesale, 'asin': asin, 'name': f'name {asin}'}


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 't.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.execute('''INSERT INTO agent_candidates (run_id, asin, title, us_price_usd, jp_cost_jpy, weight_kg, qualified,
                data_json, created_at, source_type, priority_tier, monthly_sold) VALUES ('r','A1','t',5.0,330,0.05,0,'{"roi_pct": 0.1}',
                '2026-10-01','keyword','C+',10)''')

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_dry_run_changes_nothing_and_apply_keeps_cost_and_before_values(self):
        keepa = {'A1': keepa_row('A1')}
        res = sp.refresh_rows(['A1'], keepa)
        self.assertEqual(res[0]['tier_after'], 'S')
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT priority_tier FROM agent_candidates').fetchone()[0], 'C+')
        sp.refresh_rows(['A1'], keepa, apply=True)
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            tier, cost, q, data = conn.execute('SELECT priority_tier, jp_cost_jpy, qualified, data_json FROM agent_candidates').fetchone()
        self.assertEqual((tier, cost, q), ('S', 330, 1))
        self.assertEqual(json.loads(data)['refresh_before']['priority_tier'], 'C+')

    def test_skips_asin_without_keepa_price(self):
        res = sp.refresh_rows(['A1'], {'A1': keepa_row('A1', price='')})
        self.assertEqual(res[0]['status'], 'skip')


class TestCostBasis(unittest.TestCase):
    def test_pick_supplier_skips_restricted_and_takes_cheapest(self):
        offers = [{'shop': 'A', 'price_jpy': 100, 'restricted': True}, {'shop': 'B', 'price_jpy': 300}, {'shop': 'C', 'price_jpy': 260}]
        self.assertEqual(sp.pick_supplier(offers)['shop'], 'C')
        self.assertIsNone(sp.pick_supplier([{'shop': 'A', 'price_jpy': 100, 'restricted': True}]))

    def test_investigate_uses_supplier_price_and_reports_missing_supplier(self):
        keepa = {'A1': keepa_row('A1'), 'A2': {**keepa_row('A2'), '商品コード: EAN': '4900000000999'}}
        suppliers = {'4900000000000': [{'shop': '丸進', 'price_jpy': 260, 'sd_id': '1'}]}
        ev = sp.investigate(keepa, suppliers)
        entry = (ev['qualified'] + ev['rejected'])[0]
        self.assertAlmostEqual(entry['jp_cost_jpy'], 286.0)
        self.assertEqual((entry['cost_basis'], entry['cost_supplier']), (sp.COST_BASIS_WHOLESALE, '丸進'))
        self.assertEqual(ev['no_supplier'], ['A2'])

    def test_classify_cost_basis(self):
        self.assertEqual(sp.classify_cost_basis({}), sp.COST_BASIS_AMAZON_JP)
        self.assertEqual(sp.classify_cost_basis({'wholesale_cost_jpy': 300}), sp.COST_BASIS_WHOLESALE)
        self.assertEqual(sp.classify_cost_basis({'manual_suppliers': [{'restricted': True}]}), sp.COST_BASIS_AMAZON_JP)
        self.assertEqual(sp.classify_cost_basis({'manual_suppliers': [{'shop_name': 'X'}]}), sp.COST_BASIS_WHOLESALE)

    def test_rebase_replaces_amazon_jp_cost_even_when_wholesale_is_higher(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ops_finance, 'DB_PATH', Path(tmp) / 't.sqlite3'):
            ops_finance.init_ops_tables()
            data = {'amazon_fee_usd': 1.0, 'fba_fee_usd': 2.52, 'roi_pct': 0.9, 'sales_rank_drops_30': 20}
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                conn.execute('''INSERT INTO agent_candidates (run_id, asin, title, us_price_usd, jp_cost_jpy, weight_kg, qualified, data_json,
                    created_at, source_type, priority_tier, monthly_sold) VALUES ('r','A1','t',8.0,200,0.01,1,?,'2026-10-01','keyword','A+',100)''',
                    (json.dumps(data),))
            self.assertEqual(sp.cost_basis_report(('A+',))['counts'], {sp.COST_BASIS_AMAZON_JP: 1})
            r = sp.rebase_cost('A1', '丸進', 500, apply=True, restricted=True)   # 卸(税込550円)はAmazon JP参考値(200円)より高い
            self.assertAlmostEqual(r['cost_after'], 550.0)
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                cost, d = conn.execute('SELECT jp_cost_jpy, data_json FROM agent_candidates').fetchone()
            d = json.loads(d)
            self.assertAlmostEqual(cost, 550.0)
            self.assertEqual((d['cost_basis'], d['rebase_before']['jp_cost_jpy']), (sp.COST_BASIS_WHOLESALE, 200))
            self.assertTrue(d['cost_restricted'] and d['manual_suppliers'][0]['restricted'])
            self.assertEqual(sp.cost_basis_report(('A+', 'A-', 'B+', 'B-', 'C+', 'C-', 'D', 'S'))['counts'], {sp.COST_BASIS_WHOLESALE: 1})


class TestRestrictedSupplier(unittest.TestCase):
    def test_records_without_changing_cost_or_tier(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ops_finance, 'DB_PATH', Path(tmp) / 't.sqlite3'):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                conn.execute('''INSERT INTO agent_candidates (run_id, asin, title, us_price_usd, jp_cost_jpy, qualified, data_json,
                    created_at, source_type, priority_tier) VALUES ('r','A1','t',9,800,1,'{}','2026-10-01','supplier','A+')''')
            n = sp.add_restricted_supplier('A1', 'sd', 'サンスター文具', 696, '海外販売×')
            n2 = sp.add_restricted_supplier('A1', 'sd', 'サンスター文具', 700, '海外販売×')   # 置き換え
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                cost, tier, data = conn.execute('SELECT jp_cost_jpy, priority_tier, data_json FROM agent_candidates').fetchone()
            sup = json.loads(data)['manual_suppliers']
            self.assertEqual((n, n2, cost, tier, len(sup)), (1, 1, 800, 'A+', 1))
            self.assertTrue(sup[0]['restricted'])
            self.assertEqual(sup[0]['price_jpy'], 700.0)


class TestBladeExclusion(unittest.TestCase):
    def test_english_blade_titles_are_excluded_but_corner_cutter_is_not(self):
        for title, excluded in (('CANARY Cardboard Cutter with Non-Stick Blade Utility Knife', True),
                                ('Sunstar Kadomaru Pro Corner Cutter', False),
                                ('Kokuyo Campus Binder', False)):
            row = {**map_row('A1'), 'name': title}
            ev = sp.evaluate([row], {'A1': keepa_row('A1')}, 'x')
            self.assertEqual(not ev['qualified'], excluded, title)


class TestSanity(unittest.TestCase):
    def test_rank_missing_or_bad_is_screened(self):
        self.assertEqual(sp.market_sanity_flags(10, None, 300, None), ['no_rank'])
        self.assertEqual(sp.market_sanity_flags(10, 600_000, 300, 100), ['rank_over_500k'])
        self.assertEqual(sp.market_sanity_flags(None, 1000, 300, 100), ['no_price'])

    def test_price_outlier_only_without_real_monthly_sold(self):
        # US$39 = 5,850円 は卸¥300の19.5倍
        self.assertEqual(sp.market_sanity_flags(39, 100_000, 300, None), ['price_outlier'])
        self.assertEqual(sp.market_sanity_flags(39, 100_000, 300, 100), [])

    def test_normal_candidate_has_no_flags(self):
        self.assertEqual(sp.market_sanity_flags(12, 20_000, 300, 100), [])


class TestEvaluate(unittest.TestCase):
    def test_ean_mismatch_is_screened_but_match_or_unknown_passes(self):
        bad = {**map_row('A1'), 'jan': '4900000000999'}
        ev = sp.evaluate([bad], {'A1': keepa_row('A1')}, 'Zoomy')   # keepa EAN は 4900000000000
        self.assertEqual(ev['screened_out'][0]['screened_out'], ['ean_mismatch'])
        ok = {**map_row('A2'), 'jan': '4900000000000'}
        ev = sp.evaluate([ok], {'A2': keepa_row('A2')}, 'Zoomy')
        self.assertEqual(len(ev['qualified'] + ev['rejected']), 1)

    def test_wholesale_is_tax_adjusted_and_uses_official_formula(self):
        ev = sp.evaluate([map_row('A1', 300)], {'A1': keepa_row('A1')}, '丸進')
        entry = (ev['qualified'] + ev['rejected'])[0]
        self.assertAlmostEqual(entry['jp_cost_jpy'], 330.0)   # 免税事業者: 税抜¥300 → 税込¥330
        self.assertEqual(entry['wholesale_raw_jpy'], 300)
        official = ops_finance.calc_unit_profit(12.0, 330.0, 0.05, amazon_fee_rate=0.15, fba_fee_usd=2.52)
        self.assertEqual(entry['roi_pct'], official['roi_pct'])
        self.assertEqual(entry['priority_tier'], 'S')   # 月間100・ROI高い

    def test_unknown_weight_is_flagged_not_silently_assumed(self):
        ev = sp.evaluate([map_row('A1')], {'A1': keepa_row('A1', weight='')}, '丸進')
        entry = (ev['qualified'] + ev['rejected'])[0]
        self.assertTrue(entry['weight_estimated'])
        self.assertEqual(entry['weight_kg'], ops_finance.DEFAULT_WEIGHT_KG_FALLBACK)

    def test_rank_drops_only_is_marked_as_estimate(self):
        ev = sp.evaluate([map_row('A1')], {'A1': keepa_row('A1', monthly='', drops='45')}, '丸進')
        entry = (ev['qualified'] + ev['rejected'])[0]
        self.assertEqual(entry['evidence'], 'rank_drops_estimate')
        self.assertEqual(entry['priority_tier'], 'B+')

    def test_screened_out_is_not_registered(self):
        ev = sp.evaluate([map_row('A1')], {'A1': keepa_row('A1', rank='900000')}, '丸進')
        self.assertEqual(len(ev['screened_out']), 1)
        self.assertEqual(ev['qualified'] + ev['rejected'], [])

    def test_existing_asin_is_skipped_and_tier_diff_reported(self):
        existing = {'A1': {'tier': 'D', 'excluded_kind': None, 'source_type': 'keyword'}}
        ev = sp.evaluate([map_row('A1')], {'A1': keepa_row('A1')}, '丸進', existing)
        self.assertEqual(ev['skipped_existing'], ['A1'])
        self.assertEqual(ev['qualified'], [])
        self.assertEqual(ev['diffs'][0]['existing_tier'], 'D')
        self.assertEqual(ev['diffs'][0]['new_tier'], 'S')

    def test_missing_from_keepa(self):
        ev = sp.evaluate([map_row('A1')], {}, '丸進')
        self.assertEqual(ev['no_keepa'], ['A1'])


class TestRegisterAndAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 'test.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _count(self):
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            return conn.execute('SELECT COUNT(*) FROM agent_candidates').fetchone()[0]

    def test_dry_run_writes_nothing_and_apply_persists_as_supplier(self):
        ev = sp.evaluate([map_row('A1')], {'A1': keepa_row('A1')}, '丸進')
        self.assertTrue(sp.register(ev, '丸進')['dry_run'])
        self.assertEqual(self._count(), 0)
        res = sp.register(ev, '丸進', apply=True)
        self.assertEqual(res['count'], 1)
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            row = conn.execute('SELECT source_type, priority_tier, sales_rank, qualified FROM agent_candidates').fetchone()
        self.assertEqual(row, ('supplier', 'S', 20000, 1))
        self.assertEqual(sp.load_existing()['A1']['tier'], 'S')

    def test_audit_recomputes_old_style_rows_without_writing(self):
        old = {'jp_cost': 300, 'ref_pct': 0.15, 'fba_fee': 2.52, 'monthly_sold': 100, 'rank_drops_30': 15, 'roi_pct': 9.9}
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.execute(
                '''INSERT INTO agent_candidates (run_id, asin, title, us_price_usd, jp_cost_jpy, weight_kg, qualified,
                       data_json, created_at, source_type, priority_tier)
                   VALUES ('r', 'A1', 't', 12.0, 300, 0.05, 1, ?, '2026-10-12', 'supplier', 'S')''', (json.dumps(old),))
        before = self._count()
        rows = sp.audit_supplier_rows()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['jp_cost_after'], 330.0)
        self.assertNotEqual(rows[0]['roi_after'], old['roi_pct'])
        self.assertEqual(self._count(), before)

    def test_apply_audit_updates_row_keeps_before_and_is_idempotent(self):
        old = {'jp_cost': 300, 'ref_pct': 0.15, 'fba_fee': 2.52, 'monthly_sold': 100, 'rank_drops_30': 15, 'roi_pct': 9.9}
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.execute(
                '''INSERT INTO agent_candidates (run_id, asin, title, us_price_usd, jp_cost_jpy, weight_kg, qualified,
                       data_json, created_at, source_type, priority_tier)
                   VALUES ('r', 'A1', 't', 12.0, 300, 0.05, 1, ?, '2026-10-12', 'supplier', 'S')''', (json.dumps(old),))
        self.assertEqual(sp.apply_audit(sp.audit_supplier_rows()), 1)
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            cost, data = conn.execute('SELECT jp_cost_jpy, data_json FROM agent_candidates').fetchone()
        data = json.loads(data)
        self.assertAlmostEqual(cost, 330.0)
        self.assertEqual(data['audit_before']['priority_tier'], 'S')
        self.assertEqual(sp.audit_supplier_rows(), [])   # 再実行しても対象にならない

    def test_audit_reports_rows_without_recorded_fees_as_unknown(self):
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.execute(
                '''INSERT INTO agent_candidates (run_id, asin, title, us_price_usd, jp_cost_jpy, weight_kg, qualified,
                       data_json, created_at, source_type, priority_tier)
                   VALUES ('r', 'A2', 't', 12.0, 300, 0.05, 1, '{"jp_cost": 300}', '2026-10-12', 'supplier', 'B-')''')
        rows = sp.audit_supplier_rows()
        self.assertEqual(rows[0]['tier_after'], None)


if __name__ == '__main__':
    unittest.main()
