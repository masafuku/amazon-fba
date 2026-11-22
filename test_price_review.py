import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import ops_finance
import price_review as prv

FEES = {"total": 4.0}


def fee_fn(asin, price):
    return FEES


class TestPriceReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 't.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()
        self.conn = sqlite3.connect(ops_finance.DB_PATH)
        # 原価¥300 → $2.00、関税$0.25
        self.conn.execute("INSERT INTO jp_purchase_records (asin, quantity, amount_jpy) VALUES ('A1', 10, 3000)")
        self.conn.execute("INSERT INTO sp_fba_inventory (asin, sku, fulfillable_quantity) VALUES ('A1', 'S1', 20)")
        self.conn.execute("INSERT INTO ops_events (source_id, event_type, occurred_on, ref) VALUES ('x', 'fba_in_stock', '2026-10-01', 'A1')")

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.tmp.cleanup()

    def sell(self, day, qty=1):
        oid = f"O-{day}-{qty}-{self.conn.execute('SELECT COUNT(*) FROM sp_orders').fetchone()[0]}"
        self.conn.execute("INSERT INTO sp_orders (order_id, purchase_date, order_status) VALUES (?, ?, 'Shipped')", (oid, f"{day}T10:00:00Z"))
        self.conn.execute("INSERT INTO sp_order_items (order_id, asin, sku, quantity) VALUES (?, 'A1', 'S1', ?)", (oid, qty))

    def snapshot(self, day, price, min_price, offers=10, bb=8.0):
        self.conn.execute(
            '''INSERT INTO price_snapshots (snapshot_on, sku, asin, item_name, our_price, min_price, buy_box_usd, offer_count)
               VALUES (?, 'S1', 'A1', '白檀お香', ?, ?, ?, ?)''', (day, price, min_price, bb, offers))

    def change(self, old_min=8.0, new_min=9.0, on='2026-11-01'):
        prv.record_change(self.conn, 'S1', 'A1', on, old_min, new_min, old_min, new_min, fee_fn)
        self.conn.row_factory = sqlite3.Row
        row = dict(self.conn.execute('SELECT * FROM price_changes').fetchone())
        self.conn.row_factory = None
        return row

    def test_unit_profit_matches_market_prices_formula(self):
        with mock.patch.object(prv.pr, 'INTL_SHIPPING_USD', 0.75):
            self.assertEqual(prv.unit_profit(9.0, FEES, 300), 2.0)   # 9 - 4 - 0.75 - 0.25 - 2.00

    def test_only_floor_changes_are_recorded(self):
        self.snapshot('2026-11-01', 8.0, 8.0)
        self.snapshot('2026-11-02', 8.5, 8.0)   # ルールによる価格の上下は記録しない
        self.assertEqual(prv.detect_changes(self.conn, '2026-11-02', fee_fn), [])
        self.snapshot('2026-11-03', 9.0, 9.0)
        self.assertEqual(prv.detect_changes(self.conn, '2026-11-03', fee_fn), ['S1'])
        self.assertEqual(prv.detect_changes(self.conn, '2026-11-03', fee_fn), [])   # 同じ日は1回だけ
        row = self.conn.execute('SELECT old_min, new_min, old_price, new_price FROM price_changes').fetchone()
        self.assertEqual(row, (8.0, 9.0, 8.5, 9.0))

    def test_profit_per_day_up_is_effective(self):
        for d in range(18, 32):
            self.sell(f'2026-10-{d}')            # 前: 14日で14個(1個の利益$1)
        for d in range(1, 15):
            if d % 2:
                self.sell(f'2026-11-{d:02d}')    # 後: 14日で7個(1個の利益$2) → 1日の利益は同じ
        for d in range(1, 15, 3):
            self.sell(f'2026-11-{d:02d}')        # +5個 → 増えた
        with mock.patch.object(prv.pr, 'INTL_SHIPPING_USD', 0.75):
            c = self.change(8.0, 9.0)
        self.assertEqual((c['old_unit_profit'], c['new_unit_profit']), (1.0, 2.0))
        r = prv.review_change(self.conn, c, date(2026, 11, 20))
        self.assertEqual((r['before']['units'], r['after']['units']), (14, 12))
        self.assertEqual((r['before']['stock_days'], r['after']['stock_days']), (14, 14))
        self.assertEqual(r['verdict'], '効果あり')

    def test_raise_without_sales_is_a_revert_candidate(self):
        for d in range(20, 30):
            self.sell(f'2026-10-{d}')
        r = prv.review_change(self.conn, self.change(), date(2026, 11, 9))
        self.assertEqual(r['verdict'], '元に戻す候補(値上げ後、売れていない)')

    def test_too_few_stock_days_is_on_hold(self):
        self.conn.execute("UPDATE ops_events SET occurred_on = '2026-10-29'")
        self.sell('2026-10-30')
        self.sell('2026-11-05')
        r = prv.review_change(self.conn, self.change(), date(2026, 11, 20))
        self.assertTrue(r['verdict'].startswith('判断保留(在庫あり 前3日'), r['verdict'])

    def test_in_progress_and_market_shift_are_flagged(self):
        for d in range(18, 32):
            self.sell(f'2026-10-{d}')
            self.snapshot(f'2026-10-{d}', 8.0, 8.0, offers=10)
        for d in range(1, 9):
            self.sell(f'2026-11-{d:02d}', 2)
            self.snapshot(f'2026-11-{d:02d}', 9.0, 9.0, offers=5)   # 競合が半分に減った
        r = prv.review_change(self.conn, self.change(), date(2026, 11, 9))
        self.assertEqual(r['verdict'], '途中経過 8/14日: 効果あり')
        self.assertEqual(r['market_note'], '競合の変化あり(価格の効果とは限らない)')

    def test_report_uses_product_names(self):
        self.snapshot('2026-11-01', 9.0, 9.0)
        self.change()
        text = "\n".join(prv.report(self.conn, date(2026, 11, 3)))
        self.assertIn('白檀お香', text)
        self.assertIn('最低価格 $8.00→$9.00', text)


if __name__ == '__main__':
    unittest.main()
