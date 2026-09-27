"""daily_scan.py のセラーマイニング(発見+調査)を、日中は行わず夜間だけに
一本化した変更(CEO: 「優良が見つかってもその場では検索せず。セラーサーチは
夜間だけにする」2026-09-27)の回帰テスト。

これまでセラーマイニング関連のテストが一切なかった(コメント・docstringだけが
挙動の根拠だった)ため、最低限の回帰テストとして追加する。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import daily_scan
import ops_finance


class TestDiscoverSellersForPendingCandidates(unittest.TestCase):
    """discover_sellers_for_pending_candidates(): 夜間枠の冒頭で、日中の
    キーワード検索で見つかった、まだセラー未発見の候補を拾う。"""

    def _patch_db(self, tmp):
        db_path = Path(tmp) / 'test.sqlite3'
        return mock.patch.object(ops_finance, 'DB_PATH', db_path), mock.patch.object(daily_scan, 'DB_PATH', db_path), db_path

    def _insert_candidate(self, conn, asin, qualified, tier, margin_pct=0.3, source_type='keyword',
                          created_at='2026-09-27T00:00:00+00:00'):
        conn.execute(
            "INSERT INTO agent_candidates (run_id, category, asin, qualified, tier, margin_pct, "
            "unit_profit_usd, data_json, source_type, created_at) "
            "VALUES ('r1', 'kw', ?, ?, ?, ?, 3.0, '{}', ?, ?)",
            (asin, qualified, tier, margin_pct, source_type, created_at),
        )

    def test_picks_qualified_and_consider_candidates_not_already_in_seller_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2, db_path = self._patch_db(tmp)
            with p1, p2:
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    self._insert_candidate(conn, 'B0AAAAAAA1', qualified=1, tier='pass', margin_pct=0.5)
                    self._insert_candidate(conn, 'B0AAAAAAA2', qualified=0, tier='consider', margin_pct=0.3)
                    self._insert_candidate(conn, 'B0AAAAAAA3', qualified=0, tier='reject', margin_pct=0.1)  # 対象外
                    self._insert_candidate(conn, 'B0AAAAAAA4', qualified=1, tier='pass', source_type='seller')  # 対象外(seller由来)
                with mock.patch.object(daily_scan, 'discover_and_register_sellers', return_value=['S1']) as mocked:
                    found = daily_scan.discover_sellers_for_pending_candidates(limit=10)
        self.assertEqual(found, 2)
        called_asins = [call.args[0] for call in mocked.call_args_list]
        self.assertEqual(set(called_asins), {'B0AAAAAAA1', 'B0AAAAAAA2'})

    def test_skips_asin_already_registered_in_seller_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2, db_path = self._patch_db(tmp)
            with p1, p2:
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    self._insert_candidate(conn, 'B0AAAAAAA1', qualified=1, tier='pass')
                ops_finance.add_sellers(['S1'], source='keyword_expansion', seed_asin='B0AAAAAAA1')
                with mock.patch.object(daily_scan, 'discover_and_register_sellers') as mocked:
                    found = daily_scan.discover_sellers_for_pending_candidates(limit=10)
        self.assertEqual(found, 0)
        mocked.assert_not_called()

    def test_respects_limit_and_orders_qualified_before_consider(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2, db_path = self._patch_db(tmp)
            with p1, p2:
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    self._insert_candidate(conn, 'B0LOWQUAL1', qualified=0, tier='consider', margin_pct=0.9)
                    self._insert_candidate(conn, 'B0HIGHQUAL', qualified=1, tier='pass', margin_pct=0.1)
                with mock.patch.object(daily_scan, 'discover_and_register_sellers', return_value=['S1']) as mocked:
                    daily_scan.discover_sellers_for_pending_candidates(limit=1)
        # qualified=1 が margin_pctで劣っていても、qualified優先で先に選ばれる
        self.assertEqual(mocked.call_args_list[0].args[0], 'B0HIGHQUAL')

    def test_ignores_candidates_older_than_max_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2, db_path = self._patch_db(tmp)
            with p1, p2:
                ops_finance.init_ops_tables()
                with sqlite3.connect(db_path) as conn:
                    self._insert_candidate(conn, 'B0OLD00001', qualified=1, tier='pass',
                                           created_at='2026-01-01T00:00:00+00:00')
                with mock.patch.object(daily_scan, 'discover_and_register_sellers') as mocked:
                    found = daily_scan.discover_sellers_for_pending_candidates(limit=10, max_age_hours=48)
        self.assertEqual(found, 0)
        mocked.assert_not_called()


class TestDiscoverAndRegisterSellersRegistersAll(unittest.TestCase):
    """登録するセラー数は、3件に切り詰めずKeepaが返した分だけ登録する
    (CEO: 「候補セラーを全て検討候補にいれるのはどう？」2026-09-27)。"""

    def test_passes_the_uncapped_limit_to_find_other_sellers_for_candidate(self):
        with mock.patch.object(
            daily_scan, 'find_other_sellers_for_candidate',
            return_value={'seller_ids': ['S1', 'S2', 'S3', 'S4', 'S5']},
        ) as mocked_find, mock.patch.object(daily_scan, 'add_sellers') as mocked_add:
            result = daily_scan.discover_and_register_sellers('B0EXAMPLE1', source='keyword_expansion')
        mocked_find.assert_called_once_with(asin='B0EXAMPLE1', max_sellers=daily_scan.DISCOVER_SELLERS_PER_CANDIDATE)
        self.assertGreaterEqual(daily_scan.DISCOVER_SELLERS_PER_CANDIDATE, 5)
        self.assertEqual(result, ['S1', 'S2', 'S3', 'S4', 'S5'])
        mocked_add.assert_called_once_with(
            ['S1', 'S2', 'S3', 'S4', 'S5'], source='keyword_expansion', seed_asin='B0EXAMPLE1', seed_keyword=None,
        )


class TestRunDailyScanDoesNotTouchSellers(unittest.TestCase):
    """日中のキーワード検索サイクルは、セラー関連の処理を一切呼ばない。"""

    def test_run_daily_scan_never_calls_seller_discovery(self):
        qualified_item = {
            'asin': 'B0QUALIFIED', 'title': 't', 'margin_pct': 0.3, 'tier': 'pass',
            'us_price_usd': 20.0, 'jp_cost_jpy': 1000.0,
        }
        mcp_result = {'evaluated': 1, 'matched': 1, 'skipped': [], 'qualified': [qualified_item], 'rejected': []}
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.sqlite3'
            with mock.patch.object(ops_finance, 'DB_PATH', db_path):
                ops_finance.init_ops_tables()
                with mock.patch.object(daily_scan, 'find_arbitrage_candidates', return_value=mcp_result), \
                     mock.patch.object(daily_scan, 'evaluate_mcp_candidates', return_value={
                         'qualified': [qualified_item], 'rejected': [], 'weight_missing': [], 'fee_missing': [],
                     }), \
                     mock.patch.object(daily_scan, 'enrich_qualified_candidates_with_offer_details',
                                       return_value={'enriched': 1, 'failed': 0}), \
                     mock.patch.object(daily_scan, 'discover_and_register_sellers') as mocked_discover, \
                     mock.patch.object(daily_scan, 'discover_sellers_for_pending_candidates') as mocked_pending:
                    daily_scan.run_daily_scan(
                        keyword='test kw', category_name=None, category_id=None,
                        max_candidates=5, wait_for_tokens=False,
                    )
        mocked_discover.assert_not_called()
        mocked_pending.assert_not_called()   # 夜間枠からのみ呼ばれるべきで、日中は呼ばれない


if __name__ == '__main__':
    unittest.main()
