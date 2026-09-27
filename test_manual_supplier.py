"""add_manual_supplier(): 手動(Claudeがブラウザ等で調べた結果)で見つけた仕入れ先を記録し、
安ければ利益・ROI・優先度Tierを再計算する機能の回帰テスト。

CEO(2026-09-27):
「SDなど、複数の仕入れ先が見つかった場合、表示方法は？」→「全社を並べて表示」
「どうやって情報を登録しますか？」→「NETSEAは自動で埋めてください。SDは自動では埋まらないので、
claudeが記録する」
「手動で仕入れ先を追加したとき、利益・ROIも自動で再計算しますか？」→「する」
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ops_finance


class TestAddManualSupplier(unittest.TestCase):
    def _db(self, tmp):
        return mock.patch.object(ops_finance, 'DB_PATH', Path(tmp) / 'test.sqlite3')

    def _insert_candidate(self, conn, asin='B0012ORKN8', jp_cost=309.0, created_at='2026-09-27T00:00:00+00:00'):
        data = {
            'us_price_usd': 11.63, 'jp_cost_usd': round(jp_cost / 150, 2), 'jp_cost_jpy': jp_cost,
            'amazon_fee_usd': 1.5, 'fba_fee_usd': 4.09, 'weight_kg': 0.17,
            'roi_pct': 1.0, 'monthly_sold': 200, 'sales_rank': 79798, 'brand': 'サラサーティ',
        }
        conn.execute(
            "INSERT INTO agent_candidates (run_id, category, asin, title, qualified, tier, priority_tier, "
            "monthly_sold, sales_rank, us_price_usd, jp_cost_jpy, weight_kg, margin_pct, unit_profit_usd, "
            "data_json, created_at) VALUES ('r1', 'kw', ?, 'Sarasaty Lingerie Detergent', 1, 'pass', 'A', "
            "200, 79798, 11.63, ?, 0.17, 0.3, 3.0, ?, ?)",
            (asin, jp_cost, json.dumps(data), created_at),
        )

    def test_returns_not_matched_when_asin_unknown(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            result = ops_finance.add_manual_supplier('B0UNKNOWN1', 'sd', 'ハリマ共和物産', 276)
        self.assertEqual(result, {'matched': False, 'recalculated': False, 'rows_updated': 0})

    def test_adds_supplier_to_all_rows_of_the_asin(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                self._insert_candidate(conn, created_at='2026-09-26T00:00:00+00:00')
                self._insert_candidate(conn, created_at='2026-09-27T00:00:00+00:00')
            result = ops_finance.add_manual_supplier(
                'B0012ORKN8', 'sd', 'ハリマ共和物産', 276,
                url='https://www.superdelivery.com/p/r/pd_p/5067075/', min_qty=1,
            )
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                rows = conn.execute('SELECT data_json FROM agent_candidates WHERE asin=?', ('B0012ORKN8',)).fetchall()
        self.assertEqual(result['rows_updated'], 2)
        self.assertTrue(result['matched'])
        for (data_json,) in rows:
            suppliers = json.loads(data_json)['manual_suppliers']
            self.assertEqual(len(suppliers), 1)
            self.assertEqual(suppliers[0]['shop_name'], 'ハリマ共和物産')
            self.assertEqual(suppliers[0]['price_jpy'], 276.0)
            self.assertEqual(suppliers[0]['min_qty'], 1)

    def test_same_source_and_shop_replaces_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                self._insert_candidate(conn)
            ops_finance.add_manual_supplier('B0012ORKN8', 'sd', 'ハリマ共和物産', 276)
            ops_finance.add_manual_supplier('B0012ORKN8', 'sd', 'ハリマ共和物産', 262)   # 初回限定価格に更新
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                data = json.loads(conn.execute('SELECT data_json FROM agent_candidates').fetchone()[0])
        self.assertEqual(len(data['manual_suppliers']), 1)
        self.assertEqual(data['manual_suppliers'][0]['price_jpy'], 262.0)

    def test_multiple_suppliers_coexist(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                self._insert_candidate(conn)
            ops_finance.add_manual_supplier('B0012ORKN8', 'sd', 'ハリマ共和物産', 276)
            ops_finance.add_manual_supplier('B0012ORKN8', 'sd', 'カネイシ', 283)
            ops_finance.add_manual_supplier('B0012ORKN8', 'sd', '中央物産', 287)
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                data = json.loads(conn.execute('SELECT data_json FROM agent_candidates').fetchone()[0])
        self.assertEqual(
            sorted(s['shop_name'] for s in data['manual_suppliers']), ['カネイシ', 'ハリマ共和物産', '中央物産'],
        )

    def test_recalculates_when_cheaper_than_current_cost(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                self._insert_candidate(conn, jp_cost=600.0)   # 現在の原価は高め
            result = ops_finance.add_manual_supplier('B0012ORKN8', 'sd', 'ハリマ共和物産', 276)
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                jp_cost, profit, data_json = conn.execute(
                    'SELECT jp_cost_jpy, unit_profit_usd, data_json FROM agent_candidates').fetchone()
            data = json.loads(data_json)
        self.assertTrue(result['recalculated'])
        # 免税事業者(IS_JCT_REGISTERED=False)は税込に揃える: 276 * 1.1
        self.assertAlmostEqual(jp_cost, 276 * 1.1, places=1)
        self.assertGreater(profit, 3.0)
        self.assertEqual(data['jp_cost_jpy_before_wholesale'], 600.0)
        self.assertIn('roi_after', result)
        self.assertIn('tier_after', result)

    def test_does_not_recalculate_when_not_cheaper(self):
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                self._insert_candidate(conn, jp_cost=200.0)   # 既に安い
            result = ops_finance.add_manual_supplier('B0012ORKN8', 'sd', '中央物産', 287)
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                jp_cost, profit = conn.execute('SELECT jp_cost_jpy, unit_profit_usd FROM agent_candidates').fetchone()
        self.assertFalse(result['recalculated'])
        self.assertEqual(jp_cost, 200.0)
        self.assertEqual(profit, 3.0)

    def test_netsea_and_manual_supplier_leapfrog_via_shared_jp_cost_jpy(self):
        """NETSEA自動連携と手動追加のどちらが先でも、常に安い方がjp_cost_jpyに採用される
        (両方がjp_cost_jpy列を基準に比較する設計の検証)。"""
        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            ops_finance.init_ops_tables()
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                self._insert_candidate(conn, jp_cost=600.0)
                row_id = conn.execute('SELECT id FROM agent_candidates').fetchone()[0]

            # 1. 手動でSD(¥276)を先に追加 -> 採用される
            ops_finance.add_manual_supplier('B0012ORKN8', 'sd', 'ハリマ共和物産', 276)
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                after_manual = conn.execute('SELECT jp_cost_jpy FROM agent_candidates WHERE id=?', (row_id,)).fetchone()[0]
            self.assertAlmostEqual(after_manual, 276 * 1.1, places=1)

            # 2. NETSEAがさらに安い卸(¥198)を後から見つける -> こちらに更新される
            netsea_result = ops_finance.apply_wholesale_result(
                row_id, {'shop_name': 'エムディーエス', 'product_url': 'https://example.test/', 'set_num': 5, 'jan_code': '123', 'unit_price_jpy': 198.0},
            )
            with sqlite3.connect(ops_finance.DB_PATH) as conn:
                after_netsea = conn.execute('SELECT jp_cost_jpy FROM agent_candidates WHERE id=?', (row_id,)).fetchone()[0]
        self.assertTrue(netsea_result['recalculated'])
        self.assertAlmostEqual(after_netsea, 198 * 1.1, places=1)


if __name__ == '__main__':
    unittest.main()
