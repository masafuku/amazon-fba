import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import ops_finance
import order_watch

NOW = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)


def order(order_id, status='Pending', date='2026-10-25T17:01:49Z'):
    return {'AmazonOrderId': order_id, 'OrderStatus': status, 'PurchaseDate': date}


def item(asin='B0FKTF2XVZ', qty=1, price=None, title='Sanrio FriXion Ball Knock Set'):
    entry = {'ASIN': asin, 'QuantityOrdered': qty, 'Title': title}
    if price is not None:
        entry['ItemPrice'] = {'CurrencyCode': 'USD', 'Amount': str(price)}
    return entry


class TestOrderWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 't.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()
        self.sent = []
        self.orders = []
        self.items = {}
        for name, value in [
            ('fetch_orders', lambda since: self.orders),
            ('fetch_items', lambda order_id: self.items.get(order_id, [])),
            ('fetch_stock', lambda: {'B0FKTF2XVZ': 11}),
            ('estimate_profit', lambda asin, price, qty=1: 3.12 * qty),
            ('_listing_price', lambda asin: 15.0),
        ]:
            p = mock.patch.object(order_watch, name, value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_watch(self, **kwargs):
        return order_watch.run(send=self.sent.append, now=NOW, **kwargs)

    def test_first_run_records_existing_orders_without_notifying(self):
        self.orders = [order('A-1', 'Shipped')]
        result = self.run_watch()
        self.assertTrue(result['baseline'])
        self.assertEqual(self.sent, [])
        self.assertEqual(order_watch._state(), {'A-1': 'Shipped'})

    def test_new_order_is_notified_once(self):
        self.orders = [order('A-1', 'Shipped')]
        self.run_watch()
        self.orders = [order('A-1', 'Shipped'), order('A-2')]
        self.items = {'A-2': [item(price=15.0)]}
        result = self.run_watch()
        self.assertEqual(result['new'], ['A-2'])
        self.assertEqual(len(self.sent), 1)
        self.assertIn('【新規注文 10/26 02:01】保留中', self.sent[0])
        self.assertIn('$15.00', self.sent[0])
        self.assertIn('概算利益 約$3.12', self.sent[0])
        self.assertIn('FC在庫 11点', self.sent[0])
        self.assertIn('A-2', self.sent[0])
        self.run_watch()
        self.assertEqual(len(self.sent), 1)

    def test_pending_order_without_price_uses_listing_price(self):
        self.orders = [order('A-1', 'Shipped')]
        self.run_watch()
        self.orders.append(order('A-2'))
        self.items = {'A-2': [item()]}
        self.run_watch()
        self.assertIn('$15.00(出品価格から推定)', self.sent[0])

    def test_status_change_and_cancel_are_notified(self):
        self.orders = [order('A-1', 'Pending')]
        self.run_watch()
        self.orders = [order('A-1', 'Shipped')]
        self.items = {'A-1': [item()]}
        self.run_watch()
        self.assertIn('保留中(支払い確認待ち) → 出荷済み', self.sent[0])
        self.orders = [order('A-1', 'Canceled')]
        self.run_watch()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('⚠ 出荷済み → キャンセル', self.sent[1])

    def test_failed_send_is_retried_next_time(self):
        self.orders = [order('A-1', 'Shipped')]
        self.run_watch()
        self.orders.append(order('A-2'))
        self.items = {'A-2': [item(price=15.0)]}

        def boom(_):
            raise RuntimeError('LINE down')

        with self.assertRaises(RuntimeError):
            order_watch.run(send=boom, now=NOW)
        self.assertNotIn('A-2', order_watch._state())
        self.run_watch()
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_does_not_send_or_record(self):
        self.orders = [order('A-1', 'Shipped')]
        self.run_watch()
        self.orders.append(order('A-2'))
        self.items = {'A-2': [item(price=15.0)]}
        result = self.run_watch(dry_run=True)
        self.assertEqual(result['new'], ['A-2'])
        self.assertEqual(self.sent, [])
        self.assertNotIn('A-2', order_watch._state())

    def test_unit_cost_uses_purchase_records(self):
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.execute('INSERT INTO jp_purchase_records (order_date, sd_reception_no, product_name, quantity, amount_jpy, asin) '
                         'VALUES (?, ?, ?, ?, ?, ?)', ('2026-08-26', '1', 'x', 20, 14300.0, 'B0FKTF2XVZ'))
        self.assertEqual(order_watch.unit_cost_jpy('B0FKTF2XVZ'), 715.0)
        self.assertIsNone(order_watch.unit_cost_jpy('NOPE'))


if __name__ == '__main__':
    unittest.main()
