import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ops_finance
import stock_ledger as sl

ORDER_EMAIL = """[ 発注日 ] 2026/10/06(火)13:55
■出展企業(問い合わせ先)：Zoomy BUNGU
----------------------------------------------------------------------
[　受付番号　]　94655928
[　 SD品番 　]　15782388S3
[　 商品名 　]　【ナカバヤシ】シリコンブックマーカー パタップ
[ JANコード　]　4902205744450
[　　内訳　　]　DSB-PTP-CB クリアブルー
[　注文点数　]　90点
[　注文単価　]　\\196
[　注文金額　]　\\17,640
----------------------------------------------------------------------
[送料(見込み)] \\0
"""

SHIPPED_EMAIL = """出展企業名：丸進
▼出荷内容
━━━━━━━━━━━━━━━━━━━━━━━

[　 発注日 　]　2026/10/06(火)13:21
[　受付番号　]　94654547
[　 SD品番 　]　11916786S3
[　 商品名 　]　【サンリオ】スリム定規15cm
[メーカー品番]　502600
[ JANコード　]
[　　内訳　　]　シナモロール
[ セット毎数 ]　3点
[注文セット数]　4点
[　注文点数　]　12点
[ 注文単価 ]　\\195
[ 注文合計 ]　\\2,340
----------------------------------------------------------------------

[　 発注日 　]　2026/10/06(火)13:21
[　受付番号　]　94654550
[　 SD品番 　]　15913004S2
[　 商品名 　]　【サンリオ】シール&ケースセット
[ JANコード　]
[　　内訳　　]　マイメロディ
[　注文点数　]　15点
[ 注文単価 ]　\\238
[ 注文合計 ]　\\3,570
----------------------------------------------------------------------
[　商品小計　]　\\5,910
[　 送 　料　]　\\0
━━━━━━━━━━━━━━━━━━━━━━━
■配送業者
佐川急便

■送り状番号
140418920994
"""

SCHEDULED_EMAIL = """出展企業名：丸進
[　 発注日 　]　2026/10/06(火)13:21
[　受付番号　]　94654547
[　 SD品番 　]　11916786S3
[　 商品名 　]　【サンリオ】スリム定規15cm
[ JANコード　]
[　　内訳　　]　シナモロール
[　注文点数　]　12点
[　注文単価　]　\\195
[　注文合計　]　\\2,340
[ 出荷予定日 ]　2026/10/08
"""

TNK_ARRIVED_EMAIL = """++++
荷主様氏名: - 福地菜央 様
受付日: 2026-10-02
荷受けした箱数: 1
  FBAシップメントID: FBA19RGV6RMN
  宛先FC名: HIA1
発送予定日: 2026-10-06
++++
"""

TNK_SHIPPED_EMAIL = """++++
発送日: 2026-10-06
発送した箱数: 1
容積重量: 1.38 kg
実重量: 1.8 kg
  FBAシップメントID: FBA19RGV6RMN
  宛先FC名: HIA1
宛先情報: Amazon.com Services, Inc., 3327 E Harrisburg Pike, MIDDLETOWN, PA, 17057, US
  クーリエ: FEDEX
  配送方法: FEDEX INTERNATIONAL ECONOMY
  追跡番号: 878153169479
++++
"""


class StockLedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 'test.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def query(self, sql, params=()):
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            return conn.execute(sql, params).fetchall()


class TestParsing(unittest.TestCase):
    def test_shipped_email_items_and_tracking(self):
        items = sl.parse_sd_items(SHIPPED_EMAIL)
        self.assertEqual([i['sdReceptionNo'] for i in items], ['94654547', '94654550'])
        self.assertEqual(items[0]['supplierName'], '丸進')
        self.assertEqual(items[0]['orderDate'], '2026-10-06')
        self.assertEqual(items[0]['quantity'], 12)
        self.assertEqual(items[0]['amountJpy'], 2340)
        self.assertIsNone(items[0]['janCode'])
        self.assertEqual(sl.parse_sd_shipment(SHIPPED_EMAIL), {'carrier': '佐川急便', 'trackingNo': '140418920994', 'netShippingJpy': 0.0})

    def test_tnk_shipped(self):
        info = sl.parse_tnk(TNK_SHIPPED_EMAIL)
        self.assertEqual(info['fbaShipmentId'], 'FBA19RGV6RMN')
        self.assertEqual(info['actualKg'], 1.8)
        self.assertEqual(info['volumetricKg'], 1.38)
        self.assertEqual(info['trackingNo'], '878153169479')
        self.assertEqual(info['date'], '2026-10-06')

    def test_classify(self):
        self.assertEqual(sl.classify('＜SD＞出荷完了いたしました(丸進)'), sl.SD_SHIPPED)
        self.assertEqual(sl.classify('TNK Logisticsからのお知らせ: 貨物が到着しました'), sl.TNK_ARRIVED)
        self.assertEqual(sl.classify('TNK Logisticsからのお知らせ: 貨物を発送しました'), sl.TNK_SHIPPED)
        self.assertEqual(sl.classify('RE:[CASE 1] US - Brand Approval Request for サラサーティ'), 'brand')
        self.assertIsNone(sl.classify('＜SD＞丸進の商品入荷！取引企業の新着・プライスダウン情報をお届けします'))

    def test_brand_status(self):
        subject = 'US - Brand Approval Request for SUN-STAR'
        self.assertEqual(sl.parse_brand(subject, 'determined that you are not eligible to sell'), ('SUN-STAR', sl.BRAND_REJECTED))
        self.assertEqual(sl.parse_brand(subject, 'have approved your application'), ('SUN-STAR', sl.BRAND_APPROVED))
        self.assertEqual(sl.parse_brand(subject, 'are reviewing the information'), ('SUN-STAR', sl.BRAND_PENDING))


class TestIngest(StockLedgerTestCase):
    def test_order_then_schedule_then_ship_then_receive(self):
        sl.set_sd_product_asin('11916786S3', 'B0CNKDP9WP')
        sl.ingest_email({'id': 'm1', 'subject': '＜SD＞出荷予定日のご連絡(丸進)', 'date': '2026-10-07T10:50:02Z', 'body': SCHEDULED_EMAIL})
        events = sl.ingest_email({'id': 'm2', 'subject': '＜SD＞出荷完了いたしました(丸進)', 'date': '2026-10-08T06:05:01Z', 'body': SHIPPED_EMAIL})
        self.assertEqual(len(events), 2)
        row = self.query('SELECT asin, expected_ship_date, supplier_shipped_at, tracking_no, quantity FROM jp_purchase_records WHERE sd_reception_no = ?', ('94654547',))[0]
        self.assertEqual(row, ('B0CNKDP9WP', '2026-10-08', '2026-10-08', '140418920994', 12))

        self.assertEqual(sl.mark_received(tracking_no='140418920994', on='2026-10-10'), 2)
        pipeline = {r['asin']: r for r in sl.stock_pipeline(today='2026-10-10')}
        self.assertEqual(pipeline['B0CNKDP9WP']['at_home'], 12)
        self.assertEqual(pipeline['(未紐付け:15913004S2)']['at_home'], 15)

        sl.mark_received(reception_nos=['94654547'], undo=True)
        pipeline = {r['asin']: r for r in sl.stock_pipeline(today='2026-10-10')}
        self.assertEqual(pipeline['B0CNKDP9WP']['at_home'], 0)
        self.assertEqual(pipeline['B0CNKDP9WP']['domestic_transit'], 12)
        self.assertEqual(self.query("SELECT COUNT(*) FROM ops_events WHERE event_type = 'home_received' AND ref = '94654547'"), [(0,)])

    def test_reingest_is_idempotent_and_does_not_overwrite(self):
        email = {'id': 'm1', 'subject': '＜SD＞ご注文内容控え(Zoomy BUNGU)', 'date': '2026-10-06T04:56:27Z', 'body': ORDER_EMAIL}
        self.assertEqual(len(sl.ingest_email(email)), 1)
        self.assertEqual(sl.ingest_email(email), [])
        self.assertEqual(self.query('SELECT COUNT(*) FROM jp_purchase_records'), [(1,)])

    def test_tnk_events_move_shipment_stage(self):
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.execute("INSERT INTO sp_inbound_shipments (shipment_id, shipment_confirmation_id, status) VALUES ('sh1', 'FBA19RGV6RMN', 'SHIPPED')")
            conn.execute("INSERT INTO sp_inbound_shipment_items (shipment_id, asin, sku, quantity) VALUES ('sh1', 'B0G2RBRV24', 'SKU', 10)")
        self.assertEqual(sl.stock_pipeline()[0]['planned'], 10)
        sl.ingest_email({'id': 't1', 'subject': 'TNK Logisticsからのお知らせ: 貨物が到着しました', 'date': '2026-10-02T04:16:45Z', 'body': TNK_ARRIVED_EMAIL})
        self.assertEqual(sl.stock_pipeline()[0]['at_tnk'], 10)
        sl.ingest_email({'id': 't2', 'subject': 'TNK Logisticsからのお知らせ: 貨物を発送しました', 'date': '2026-10-07T00:06:44Z', 'body': TNK_SHIPPED_EMAIL})
        self.assertEqual(sl.stock_pipeline()[0]['intl_transit'], 10)
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.execute("UPDATE sp_inbound_shipments SET status = 'RECEIVING'")
        self.assertEqual(sl.stock_pipeline()[0]['intl_transit'], 0)

    def test_days_of_cover_uses_30_day_sales(self):
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            conn.execute("INSERT INTO sp_fba_inventory (asin, sku, fulfillable_quantity) VALUES ('A1', 'S', 20)")
            conn.execute("INSERT INTO sp_orders (order_id, purchase_date, order_status) VALUES ('o1', '2026-10-01T00:00:00Z', 'Shipped')")
            conn.execute("INSERT INTO sp_orders (order_id, purchase_date, order_status) VALUES ('o2', '2026-08-01T00:00:00Z', 'Shipped')")
            conn.execute("INSERT INTO sp_order_items (order_id, asin, quantity) VALUES ('o1', 'A1', 15), ('o2', 'A1', 99)")
        row = sl.stock_pipeline(today='2026-10-10')[0]
        self.assertEqual(row['sold_30d'], 15)
        self.assertEqual(row['days_of_cover'], 40.0)


if __name__ == '__main__':
    unittest.main()
