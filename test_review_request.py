import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import ops_finance
import review_request

NOW = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)


class TestReviewRequest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 't.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.executemany('INSERT INTO sp_orders (order_id, purchase_date, order_status) VALUES (?, ?, ?)', [
                ('OK-1', '2026-10-25T00:00:00Z', 'Shipped'),
                ('REFUND-1', '2026-10-29T00:00:00Z', 'Shipped'),
                ('NOT-YET', '2026-10-28T00:00:00Z', 'Shipped'),    # Amazonがまだ送れないと言う注文
                ('TOO-NEW', '2026-11-09T00:00:00Z', 'Shipped'),    # 5日未満はAPIも呼ばない
                ('PENDING', '2026-10-25T00:00:00Z', 'Pending'),
                ('TOO-OLD', '2026-09-01T00:00:00Z', 'Shipped'),
            ])
        self.sent, self.checked = [], []

        def can_request(order_id):
            self.checked.append(order_id)
            return order_id != 'NOT-YET'

        for name, value in [('can_request', can_request), ('has_refund', lambda oid: oid == 'REFUND-1'),
                            ('send_request', self.sent.append)]:
            p = mock.patch.object(review_request, name, value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_sends_once_and_skips_refunded_orders(self):
        result = review_request.run(now=NOW)
        self.assertEqual(result, {'sent': ['OK-1'], 'skipped_refunded': ['REFUND-1']})
        self.assertEqual(self.sent, ['OK-1'])
        self.assertEqual(sorted(self.checked), ['NOT-YET', 'OK-1', 'REFUND-1'])
        self.checked.clear()
        self.assertEqual(review_request.run(now=NOW), {'sent': [], 'skipped_refunded': []})
        self.assertEqual(self.sent, ['OK-1'])
        self.assertEqual(self.checked, ['NOT-YET'])   # 送れなかった注文だけ次回も確かめる

    def test_dry_run_sends_and_records_nothing(self):
        self.assertEqual(review_request.run(dry_run=True, now=NOW)['sent'], ['OK-1'])
        self.assertEqual(self.sent, [])
        self.assertEqual(review_request._state(), {})


if __name__ == '__main__':
    unittest.main()
