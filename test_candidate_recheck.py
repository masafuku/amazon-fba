import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import candidate_recheck as cr
import ops_finance
from sp_api import client


def insert(conn, asin, tier, price, cost, monthly_sold, roi, excluded=None, row_id=None):
    conn.execute(
        '''INSERT INTO agent_candidates (run_id, asin, title, us_price_usd, jp_cost_jpy, weight_kg, qualified, data_json, created_at,
               monthly_sold, priority_tier, excluded_kind)
           VALUES ('r', ?, ?, ?, ?, 0.1, 1, ?, '2026-10-01', ?, ?, ?)''',
        (asin, f'title {asin}', price, cost, json.dumps({'roi_pct': roi}), monthly_sold, tier, excluded))


class TestCandidateRecheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 'test.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            insert(conn, 'S1', 'S', 30.0, 1500, 150, 1.2)      # ROI100%超・強い実売 -> S
            insert(conn, 'A1', 'A+', 30.0, 1500, 50, 1.2)
            insert(conn, 'B1', 'B+', 30.0, 1500, None, 0.5)     # 対象外のTier
            insert(conn, 'X1', 'S', 30.0, 1500, 150, 1.2, excluded='food')   # 完全除外
            insert(conn, 'S1', 'A+', 30.0, 1500, 150, 1.2)      # S1の新しい評価(最新行はこちら)
        self.sleep = mock.patch.object(cr.time, 'sleep')
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def test_loads_latest_row_per_asin_in_target_tiers_only(self):
        self.assertEqual([(c['asin'], c['priority_tier']) for c in cr.load_candidates()], [('A1', 'A+'), ('S1', 'A+')])

    def test_price_drop_lowers_tier_and_unchanged_stays(self):
        # A1: バイボックスが$10に下がる -> 利益が大きく減りROIが下がる
        def offers(asin):
            return {'buy_box': 10.0, 'lowest_fba': 10.0, 'offers': 5}

        def fees(asin, price):
            return {'referral': price * 0.15, 'fba': 3.5, 'other': 0.0, 'total': 0}

        with mock.patch.object(client, 'get_offers_summary', side_effect=offers), mock.patch.object(client, 'get_fees_estimate', side_effect=fees):
            counts = cr.recheck()
        self.assertEqual(counts['ok'], 2)
        text = cr.report()
        self.assertIn('Tierが変わった: 2件（下がる2・上がる0）', text)
        self.assertIn('A1 A+→', text)

    def test_no_buy_box_and_error_are_recorded_and_errors_retried(self):
        with mock.patch.object(client, 'get_offers_summary', side_effect=[{'buy_box': None, 'lowest_fba': None, 'offers': 0}, client.SpApiError('x')]):
            counts = cr.recheck()
        self.assertEqual((counts['no_buy_box'], counts['error']), (1, 1))
        with mock.patch.object(client, 'get_offers_summary', return_value={'buy_box': None, 'lowest_fba': None, 'offers': 0}) as call:
            cr.recheck()
        self.assertEqual(call.call_count, 1)   # 失敗した1件だけが再取得される

    def test_recompute_matches_original_formula(self):
        candidate = {'jp_cost_jpy': 1500, 'weight_kg': 0.1, 'monthly_sold': 150}
        new = cr.recompute(candidate, 30.0, {'referral': 4.5, 'fba': 3.5, 'other': 0.0})
        expected = ops_finance.calc_unit_profit(30.0, 1500, 0.1, amazon_fee_rate=0.15, fba_fee_usd=3.5)
        self.assertEqual(new['roi'], expected['roi_pct'])


if __name__ == '__main__':
    unittest.main()
