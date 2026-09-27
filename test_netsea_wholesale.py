import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ops_finance


def _item(supplier_id, product_id, jan, sets, shop='テスト商事'):
    return {'supplier_id': supplier_id, 'product_id': product_id, 'jan_code': jan, 'shop_name': shop,
            'product_name': f'商品{product_id}', 'product_url': f'https://example.test/{product_id}',
            'image_copy_flag': 'N', 'direct_send_flag': 'Y', 'set': sets}


def _set(direct_id, jan, num, price_ex_tax, sold_out='N'):
    return {'direct_item_id': direct_id, 'jan_code': jan, 'set_num': num, 'price': price_ex_tax // num,
            'set_price_without_tax': price_ex_tax, 'sold_out_flag': sold_out}


class TestCatalogRows(unittest.TestCase):
    def test_rows_use_set_jan_unit_price_and_skip_janless(self):
        items = [
            _item(1, 'a', '4902205744436', [_set('a-1', '4902205744436', 5, 990)]),
            _item(1, 'b', '', [_set('b-1', '', 1, 100)]),                          # JANなし -> 除外
            _item(2, 'c', '4900000000001', [_set('c-1', '', 1, 300, 'Y')]),       # 商品側のJANを使い、売り切れ
        ]
        rows = ops_finance.netsea_rows_from_items(items, '2026-09-27T00:00:00+00:00')
        self.assertEqual(len(rows), 2)
        first = next(r for r in rows if r[1] == 'a')
        self.assertEqual((first[3], first[7], first[8], first[9]), ('4902205744436', 5, 198.0, 990.0))
        sold = next(r for r in rows if r[1] == 'c')
        self.assertEqual((sold[3], sold[10]), ('4900000000001', 1))


class TestWholesale(unittest.TestCase):
    JAN = '4902205744436'

    def _db(self, tmp):
        return mock.patch.object(ops_finance, 'DB_PATH', Path(tmp) / 'test.sqlite3')

    def _insert_candidate(self, conn, asin='B0G2RBRV24', tier='A', jp_cost=291.0, ean=JAN, listing='ok',
                          title='Nakabayashi Silicone Book Marker', extra=None):
        data = {'us_price_usd': 12.0, 'jp_cost_usd': round(jp_cost / 150, 2), 'jp_cost_jpy': jp_cost,
                'amazon_fee_usd': 1.8, 'fba_fee_usd': 3.5, 'weight_kg': 0.05, 'ean': ean,
                'roi_pct': 1.0, 'monthly_sold': 100, 'sales_rank': 5000, 'brand': 'Nakabayashi'}
        data.update(extra or {})
        conn.execute(
            "INSERT INTO agent_candidates (run_id, category, asin, title, qualified, tier, priority_tier, "
            "listing_status, monthly_sold, sales_rank, us_price_usd, jp_cost_jpy, weight_kg, margin_pct, unit_profit_usd, "
            "data_json, created_at) VALUES ('r1', 'kw', ?, ?, 1, 'pass', ?, ?, 100, 5000, 12.0, ?, 0.05, 0.3, 3.0, ?, "
            "'2026-09-27T00:00:00+00:00')",
            (asin, title, tier, listing, jp_cost, json.dumps(data)),
        )

    def _catalog(self, items):
        ops_finance.replace_netsea_catalog(
            [str(i['supplier_id']) for i in items],
            ops_finance.netsea_rows_from_items(items, '2026-09-27T00:00:00+00:00'),
        )

    def test_match_picks_cheapest_in_stock_unit_price(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            self._catalog([
                _item(1, 'a', self.JAN, [_set('a-1', self.JAN, 5, 990)]),                    # 198/個
                _item(2, 'b', self.JAN, [_set('b-1', self.JAN, 1, 150, 'Y')], shop='売切'),  # 売り切れ -> 除外
                _item(3, 'c', self.JAN, [_set('c-1', self.JAN, 10, 2200)], shop='ロット大'),  # 220/個
            ])
            match = ops_finance.find_netsea_match([self.JAN])
            self.assertEqual((match['shop_name'], match['unit_price_jpy'], match['set_num']), ('テスト商事', 198.0, 5))
            self.assertIsNone(ops_finance.find_netsea_match(['4999999999999']))
            self.assertIsNone(ops_finance.find_netsea_match(['123']))   # 13桁でない

    def test_replace_catalog_replaces_only_given_suppliers(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            self._catalog([_item(1, 'a', self.JAN, [_set('a-1', self.JAN, 1, 500)]),
                           _item(2, 'b', '4900000000001', [_set('b-1', '4900000000001', 1, 300)])])
            self._catalog([_item(1, 'a', self.JAN, [_set('a-1', self.JAN, 1, 400)])])   # サプライヤー1だけ再同期
            self.assertEqual(ops_finance.find_netsea_match([self.JAN])['unit_price_jpy'], 400.0)
            self.assertIsNotNone(ops_finance.find_netsea_match(['4900000000001']))     # サプライヤー2は残る

    def test_upsert_then_delete_stale_removes_only_unseen_rows_of_that_supplier(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            old = ops_finance.netsea_rows_from_items(
                [_item(1, 'a', self.JAN, [_set('a-1', self.JAN, 1, 500)]),
                 _item(1, 'gone', '4900000000002', [_set('g-1', '4900000000002', 1, 300)]),
                 _item(2, 'b', '4900000000001', [_set('b-1', '4900000000001', 1, 300)])], '2026-09-01T00:00:00+00:00')
            ops_finance.upsert_netsea_rows(old)
            new = ops_finance.netsea_rows_from_items(
                [_item(1, 'a', self.JAN, [_set('a-1', self.JAN, 1, 400)])], '2026-09-27T00:00:00+00:00')
            ops_finance.upsert_netsea_rows(new)
            deleted = ops_finance.delete_stale_netsea_rows([1], '2026-09-27T00:00:00+00:00')
            self.assertEqual(deleted, 1)   # 出品終了した 'gone' だけ
            self.assertEqual(ops_finance.find_netsea_match([self.JAN])['unit_price_jpy'], 400.0)
            self.assertIsNone(ops_finance.find_netsea_match(['4900000000002']))
            self.assertIsNotNone(ops_finance.find_netsea_match(['4900000000001']))   # サプライヤー2は触らない

    def test_loader_selects_listable_top_tier_with_jan_and_unchecked(self):
        fresh = ops_finance.datetime.now(ops_finance.timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                self._insert_candidate(conn, 'B0AAAAAAA1', 'A')                                      # 対象
                self._insert_candidate(conn, 'B0AAAAAAA2', 'S', ean=None)                            # JANなし
                self._insert_candidate(conn, 'B0AAAAAAA3', 'A', listing='approval_required')         # 出品不可
                self._insert_candidate(conn, 'B0AAAAAAA4', 'B-')                                     # Tier対象外
                self._insert_candidate(conn, 'B0AAAAAAA5', 'A', extra={'wholesale_checked_at': fresh})  # 照会済み
                self._insert_candidate(conn, 'B0AAAAAAA6', 'S', extra={'netsea_jan': self.JAN})      # NETSEA由来
                self._insert_candidate(conn, 'B0AAAAAAA7', 'S', extra={'brand': 'HARIO'}, title='Hario Kettle')
            found = ops_finance.load_candidates_needing_wholesale(limit=10)
        self.assertEqual([c['asin'] for c in found], ['B0AAAAAAA1'])
        self.assertEqual(found[0]['jans'], [self.JAN])

    def test_apply_recalculates_profit_roi_and_tier_when_wholesale_is_cheaper(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                self._insert_candidate(conn, tier='B+', jp_cost=600.0)
                row_id = conn.execute('SELECT id FROM agent_candidates').fetchone()[0]
            self._catalog([_item(1, 'a', self.JAN, [_set('a-1', self.JAN, 5, 990)])])
            match = ops_finance.find_netsea_match([self.JAN])
            result = ops_finance.apply_wholesale_result(row_id, match)
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                jp_cost, profit, data_json = conn.execute(
                    'SELECT jp_cost_jpy, unit_profit_usd, data_json FROM agent_candidates WHERE id = ?', (row_id,)).fetchone()
            data = json.loads(data_json)
        self.assertTrue(result['recalculated'])
        # 免税事業者(IS_JCT_REGISTERED=False)は税込に揃える: 198 * 1.1
        self.assertAlmostEqual(jp_cost, 198 * 1.1, places=1)
        self.assertGreater(profit, 3.0)
        self.assertEqual(data['jp_cost_jpy_before_wholesale'], 600.0)
        self.assertEqual((data['wholesale_cost_jpy'], data['netsea_set_num']), (198.0, 5))
        self.assertIn('wholesale_checked_at', data)
        self.assertGreater(data['roi_pct'], 1.0)

    def test_apply_keeps_cost_when_wholesale_is_not_cheaper_and_records_no_match(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                self._insert_candidate(conn, 'B0AAAAAAA1', 'A', jp_cost=200.0)
                self._insert_candidate(conn, 'B0AAAAAAA2', 'A', jp_cost=200.0)
                ids = [r[0] for r in conn.execute('SELECT id FROM agent_candidates ORDER BY id')]
            self._catalog([_item(1, 'a', self.JAN, [_set('a-1', self.JAN, 5, 990)])])   # 198*1.1=217.8 > 200
            r1 = ops_finance.apply_wholesale_result(ids[0], ops_finance.find_netsea_match([self.JAN]))
            r2 = ops_finance.apply_wholesale_result(ids[1], None)
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                rows = conn.execute('SELECT jp_cost_jpy, unit_profit_usd, data_json FROM agent_candidates ORDER BY id').fetchall()
        self.assertTrue(r1['matched']); self.assertFalse(r1['recalculated'])
        self.assertFalse(r2['matched'])
        self.assertEqual(rows[0][0], 200.0); self.assertEqual(rows[0][1], 3.0)
        self.assertEqual(json.loads(rows[0][2])['wholesale_cost_jpy'], 198.0)
        self.assertIn('wholesale_checked_at', json.loads(rows[1][2]))
        self.assertNotIn('wholesale_cost_jpy', json.loads(rows[1][2]))


if __name__ == '__main__':
    unittest.main()
