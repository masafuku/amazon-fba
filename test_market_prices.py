import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import market_prices as mp
import ops_finance
import stock_ledger
from sp_api import client


class TestMarketPrices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 'test.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.execute("INSERT INTO jp_purchase_records (sd_reception_no, quantity, amount_jpy, asin) VALUES ('1', 30, 5850, 'A1'), ('2', 10, 1960, 'A2')")
            conn.execute("INSERT INTO sp_fba_inventory (asin, sku, fulfillable_quantity) VALUES ('A1', 'SKU1', 0), ('A2', 'SKU2', 0)")
        self.sleep = mock.patch.object(mp.time, 'sleep')
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def offers(self, asin):
        return {'A1': {'buy_box': 7.49, 'lowest_fba': 7.49, 'offers': 8}, 'A2': {'buy_box': 4.0, 'lowest_fba': 4.0, 'offers': 3}}[asin]

    def fees(self, asin, price):
        return {'referral': price * 0.15, 'fba': 2.52, 'other': 0.0, 'total': 0}

    def test_refresh_stores_prices_and_roi(self):
        with mock.patch.object(client, 'get_offers_summary', side_effect=self.offers), \
                mock.patch.object(client, 'get_fees_estimate', side_effect=self.fees):
            self.assertEqual(mp.refresh(['A1', 'A2']), {'updated': 2, 'failed': []})
        rows = {r['asin']: r for r in mp.roi_rows()}
        self.assertEqual(rows['A1']['buy_box'], 7.49)
        self.assertGreater(rows['A1']['roi'], 1.0)
        self.assertLess(rows['A2']['roi'], 0.2)
        alerts = mp.roi_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('SKU2', alerts[0])
        self.assertTrue(any('ROI低下' in a for a in stock_ledger.alerts()))

    def test_failed_asin_keeps_previous_value(self):
        with mock.patch.object(client, 'get_offers_summary', side_effect=self.offers), \
                mock.patch.object(client, 'get_fees_estimate', side_effect=self.fees):
            mp.refresh(['A1'])
        with mock.patch.object(client, 'get_offers_summary', side_effect=client.SpApiError('x')):
            self.assertEqual(mp.refresh(['A1']), {'updated': 0, 'failed': ['A1']})
        self.assertEqual(mp.roi_rows()[0]['buy_box'], 7.49)

    def test_no_buy_box_stores_nulls(self):
        with mock.patch.object(client, 'get_offers_summary', return_value={'buy_box': None, 'lowest_fba': None, 'offers': 0}):
            mp.refresh(['A1'])
        self.assertIsNone(mp.roi_rows()[0]['roi'])


if __name__ == '__main__':
    unittest.main()
