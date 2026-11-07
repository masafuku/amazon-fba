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


def item(asin='B0FKTF2XVZ', qty=1, title='Sanrio FriXion Ball Knock Set'):
    return {'ASIN': asin, 'QuantityOrdered': qty, 'Title': title}


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
            ('fetch_items', lambda order_id: self.items.get(order_id, [item()])),
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

    def test_new_order_is_notified_once_with_name_and_time_only(self):
        self.orders = [order('A-1', 'Shipped')]
        self.run_watch()
        self.orders = [order('A-1', 'Shipped'), order('A-2')]
        result = self.run_watch()
        self.assertEqual(result['new'], ['A-2'])
        self.assertEqual(self.sent, ['【売れた 10/26 02:01】Sanrio FriXion Ball Knock Set'])
        self.run_watch()
        self.assertEqual(len(self.sent), 1)

    def test_quantity_is_shown_only_when_more_than_one(self):
        self.orders = [order('A-1', 'Shipped')]
        self.run_watch()
        self.orders.append(order('A-2'))
        self.items = {'A-2': [item(qty=3)]}
        self.run_watch()
        self.assertIn('Sanrio FriXion Ball Knock Set ×3', self.sent[0])

    def test_shipped_and_cancel_are_notified(self):
        self.orders = [order('A-1', 'Pending')]
        self.run_watch()
        self.orders = [order('A-1', 'Shipped')]
        self.run_watch()
        self.assertEqual(self.sent[0], '【出荷 10/26 12:00】Sanrio FriXion Ball Knock Set')
        self.orders = [order('A-1', 'Canceled')]
        self.run_watch()
        self.assertEqual(self.sent[1], '【⚠ キャンセル 10/26 12:00】Sanrio FriXion Ball Knock Set')

    def test_change_to_pending_is_recorded_silently(self):
        self.orders = [order('A-1', 'Unshipped')]
        self.run_watch()
        self.orders = [order('A-1', 'Pending')]
        self.run_watch()
        self.assertEqual(self.sent, [])
        self.assertEqual(order_watch._state(), {'A-1': 'Pending'})

    def test_failed_send_is_retried_next_time(self):
        self.orders = [order('A-1', 'Shipped')]
        self.run_watch()
        self.orders.append(order('A-2'))

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
        result = self.run_watch(dry_run=True)
        self.assertEqual(result['new'], ['A-2'])
        self.assertEqual(self.sent, [])
        self.assertNotIn('A-2', order_watch._state())


if __name__ == '__main__':
    unittest.main()
