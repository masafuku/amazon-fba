import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import daily_report as dr
import ops_finance
import stock_ledger as sl

NOW = datetime(2026, 11, 11, 23, 0, tzinfo=timezone.utc)
SINCE = '2026-11-10T11:00:00+00:00'


class TestDailyReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 't.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()
        c = self.conn = sqlite3.connect(ops_finance.DB_PATH)
        # パタップ ブルー: 原価¥150($1.00)、相場$7.00で手数料 紹介料$1.05+FBA$3.00
        c.execute("INSERT INTO jp_purchase_records (sd_reception_no, product_name, asin, quantity, amount_jpy, received_at) "
                  "VALUES ('R1', '【ナカバヤシ】パタップ ブルー', 'A1', 50, 7500, '2026-10-01')")
        c.execute("INSERT INTO market_prices (asin, buy_box_usd, referral_fee_usd, fba_fee_usd, other_fee_usd) VALUES ('A1', 7.0, 1.05, 3.0, 0)")
        c.execute("INSERT INTO sp_fba_inventory (asin, sku, fulfillable_quantity) VALUES ('A1', 'S1', 40)")
        c.execute("INSERT INTO price_snapshots (snapshot_on, sku, asin, our_price) VALUES ('2026-11-09', 'S1', 'A1', 6.99)")
        c.execute("INSERT INTO ops_events (source_id, event_type, occurred_on, ref) VALUES ('x', 'fba_in_stock', '2026-10-20', 'A1')")
        for oid, price in [('O1', 6.99), ('O2', None)]:   # O2は支払い確定前(金額なし)
            c.execute("INSERT INTO sp_orders (order_id, purchase_date, order_status) VALUES (?, '2026-11-10T16:00:00Z', ?)",
                      (oid, 'Shipped' if price else 'Pending'))
            c.execute("INSERT INTO sp_order_items (order_id, asin, sku, quantity, item_price_usd) VALUES (?, 'A1', 'S1', 1, ?)", (oid, price))
        c.commit()
        p = mock.patch.object(dr.prv.pr, 'INTL_SHIPPING_USD', 0.75)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_pending_orders_use_the_listing_price_and_estimated_fees(self):
        [item] = dr.sales(self.conn, SINCE)
        self.assertEqual((item['qty'], round(item['revenue'], 2)), (2, 13.98))
        # 6.99 - (1.05/7*6.99 + 3.00) - 0.75 - 0.125 - 1.00 = 1.07 / 個
        self.assertAlmostEqual(item['profit'], 2 * 1.07, places=1)
        lines = dr.sales_lines([item], {'A1': '【ナカバヤシ】パタップ ブルー'})
        self.assertTrue(lines[0].startswith('売上: 2個 $13.98 / 利益 $2.1'))
        self.assertIn('パタップ ブルー  $6.99×2 = $13.98', lines[1])
        self.assertNotIn('見込み', '\n'.join(lines))

    def test_report_sections_and_new_marks(self):
        with mock.patch.object(sl, 'alerts', return_value=['SDの支払い未確認: 2026-10分 ¥89,772（請求書は発行済み。払ったら paid --month 2026-10 で登録）']):
            text, alerts_now = dr.build_report(SINCE, now=NOW)
            self.assertIn('【朝のまとめ 11/12】', text)
            self.assertIn('1. 新 SDの支払い未確認: 2026-10分 ¥89,772', text)
            self.assertNotIn('paid --month', text)
            self.assertIn('・パタップ ブルー: 2個売れた → 残り40個', text)
            dr.mark_sent(alerts_now)
            text, _ = dr.build_report(SINCE, now=NOW)
            self.assertIn('1. SDの支払い未確認', text)   # 2回目は「新」が付かない

    def test_evening_without_news_is_not_sent(self):
        with mock.patch.object(sl, 'alerts', return_value=[]):
            text, _ = dr.build_report('2026-11-11T00:00:00+00:00', evening=True, now=NOW)
        self.assertIsNone(text)

    def test_short_alert_drops_cli_hints(self):
        self.assertEqual(dr.short_alert('ASIN未紐付け: パタップ クリア（受付番号94655926）。出品するならmap-sdで紐付け'),
                         'ASIN未紐付け: パタップ クリア（受付番号94655926）')


if __name__ == '__main__':
    unittest.main()
