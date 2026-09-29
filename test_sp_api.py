"""SP-API連携(収支・在庫ダッシュボード)のユニットテスト。

Keepaからの独立性を保つ設計を検証する意図も込めて、これらのテストは
keepa_mcp/keepa関連のものを一切importしない。
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import ops_finance as of
import sd_email_parser
import sp_api.client as sp_client
import sp_api_sync
from sp_api.config import Settings as SpSettings


class TestSpApiSettings(unittest.TestCase):
    def test_not_configured_when_missing_credentials(self):
        settings = SpSettings(
            lwa_client_id="", lwa_client_secret="", refresh_token="",
            region="na", marketplace_id="ATVPDKIKX0DER",
        )
        self.assertFalse(settings.configured)

    def test_configured_when_all_credentials_present(self):
        settings = SpSettings(
            lwa_client_id="id", lwa_client_secret="secret", refresh_token="token",
            region="na", marketplace_id="ATVPDKIKX0DER",
        )
        self.assertTrue(settings.configured)

    def test_missing_one_field_is_not_configured(self):
        settings = SpSettings(
            lwa_client_id="id", lwa_client_secret="", refresh_token="token",
            region="na", marketplace_id="ATVPDKIKX0DER",
        )
        self.assertFalse(settings.configured)


class TestLwaTokenCaching(unittest.TestCase):
    def setUp(self):
        sp_client._access_token = None
        sp_client._access_token_expires_at = 0.0

    def test_reuses_cached_token_before_expiry(self):
        with patch.object(sp_client, "_fetch_access_token", return_value=("tok1", 3600.0)) as mock_fetch:
            token1 = sp_client.get_access_token()
            token2 = sp_client.get_access_token()
        self.assertEqual(token1, "tok1")
        self.assertEqual(token2, "tok1")
        mock_fetch.assert_called_once()

    def test_refreshes_when_forced(self):
        with patch.object(sp_client, "_fetch_access_token", side_effect=[("tok1", 3600.0), ("tok2", 3600.0)]):
            token1 = sp_client.get_access_token()
            token2 = sp_client.get_access_token(force_refresh=True)
        self.assertEqual(token1, "tok1")
        self.assertEqual(token2, "tok2")

    def test_refreshes_when_expired(self):
        with patch.object(sp_client, "_fetch_access_token", side_effect=[("tok1", 0.001), ("tok2", 3600.0)]):
            token1 = sp_client.get_access_token()
            time.sleep(0.05)
            token2 = sp_client.get_access_token()
        self.assertEqual(token1, "tok1")
        self.assertEqual(token2, "tok2")


class TestSpApiSyncGuard(unittest.TestCase):
    """認証情報未設定なら、SP-APIを一切呼ばずに正常終了することを確認する。"""

    def test_run_once_skips_without_credentials(self):
        fake_settings = MagicMock(configured=False)
        with patch.object(sp_api_sync, "sp_settings", fake_settings), \
             patch.object(sp_api_sync.sp_client, "get_orders") as mock_get_orders:
            sp_api_sync.run_once()
        mock_get_orders.assert_not_called()


class TestSdEmailParser(unittest.TestCase):
    # 本日実際に受信したSuper Delivery注文確定メールの本文(Zoomy BUNGU分、
    # パタップ クリアイエロー10点)をそのままfixtureにする。
    SAMPLE_BODY = (
        "■出展企業(問い合わせ先)：Zoomy BUNGU\n"
        "https://www.superdelivery.com/p/do/dpsl/di/1003908/\n"
        "[注文時のメッセージ]\n\n"
        "----------------------------------------------------------------------\n"
        "[　受付番号　]　93977902\n"
        "[　 SD品番 　]　15782388S2\n"
        "[　 商品名 　]　【ナカバヤシ】シリコンブックマーカー パタップ\n"
        "[メーカー品番]　1531671\n"
        "[ JANコード　]　4902205744443\n"
        "[　　内訳　　]　DSB-PTP-CY クリアイエロー\n"
        "[ セット毎数 ]　5点\n"
        "[注文セット数]　2セット\n"
        "[　注文点数　]　10点\n"
        "[　注文単価　]　\\196\n"
        "[　注文金額　]　\\1,960\n"
        "----------------------------------------------------------------------\n"
    )

    def test_parses_single_reception_block(self):
        records = sd_email_parser.parse_email_body(self.SAMPLE_BODY, order_date="2026-09-08")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["sdReceptionNo"], "93977902")
        self.assertEqual(record["sdProductNo"], "15782388S2")
        self.assertEqual(record["janCode"], "4902205744443")
        self.assertEqual(record["variant"], "DSB-PTP-CY クリアイエロー")
        self.assertEqual(record["quantity"], 10)
        self.assertEqual(record["unitPriceJpy"], 196.0)
        self.assertEqual(record["amountJpy"], 1960.0)
        self.assertEqual(record["supplierName"], "Zoomy BUNGU")
        self.assertEqual(record["orderDate"], "2026-09-08")

    def test_no_reception_number_yields_no_records(self):
        self.assertEqual(sd_email_parser.parse_email_body("no matching content here"), [])

    def test_amount_with_comma_parses_correctly(self):
        body = self.SAMPLE_BODY.replace("\\1,960", "\\12,345")
        records = sd_email_parser.parse_email_body(body)
        self.assertEqual(records[0]["amountJpy"], 12345.0)


class TestSdEmailParserGuard(unittest.TestCase):
    def test_run_once_skips_without_gmail_credentials(self):
        with patch.object(sd_email_parser.gmail, "configured", return_value=False), \
             patch.object(sd_email_parser.gmail, "search_messages") as mock_search:
            sd_email_parser.run_once()
        mock_search.assert_not_called()


class TestAllocateOrderShipping(unittest.TestCase):
    """CEO: 「送料は商品ごとに配分して」— 同一発注(supplier_name+order_date)内の
    商品行に、送料を数量按分で書き込み、COGS計算(compute_finance_summary)に
    反映されることを確認する。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = of.DB_PATH
        of.DB_PATH = Path(self._tmpdir.name) / "test.sqlite3"
        of.init_ops_tables()

    def tearDown(self):
        of.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    def test_shipping_allocated_proportionally_by_quantity(self):
        of.upsert_jp_purchase_record({
            "orderDate": "2026-08-26", "sdReceptionNo": "R1", "supplierName": "丸進",
            "sdProductNo": None, "productName": "定規", "janCode": "JAN1", "variant": None,
            "unitPriceJpy": 195, "quantity": 30, "amountJpy": 5850,
        })
        of.upsert_jp_purchase_record({
            "orderDate": "2026-08-26", "sdReceptionNo": "R2", "supplierName": "丸進",
            "sdProductNo": None, "productName": "シール", "janCode": "JAN2", "variant": None,
            "unitPriceJpy": 238, "quantity": 30, "amountJpy": 7140,
        })
        updated = of.allocate_order_shipping("丸進", "2026-08-26", 800)
        self.assertEqual(len(updated), 2)
        # 30個+30個=60個のうち、各行30個ずつ -> 半分ずつ(¥400)に配分される
        for row in updated:
            self.assertAlmostEqual(row["shippingCostJpy"], 400.0)

    def test_shipping_allocation_only_affects_matching_order(self):
        of.upsert_jp_purchase_record({
            "orderDate": "2026-08-26", "sdReceptionNo": "R3", "supplierName": "丸進",
            "sdProductNo": None, "productName": "定規", "janCode": "JAN3", "variant": None,
            "unitPriceJpy": 195, "quantity": 10, "amountJpy": 1950,
        })
        of.upsert_jp_purchase_record({
            "orderDate": "2026-09-08", "sdReceptionNo": "R4", "supplierName": "Zoomy BUNGU",
            "sdProductNo": None, "productName": "パタップ", "janCode": "JAN4", "variant": None,
            "unitPriceJpy": 196, "quantity": 10, "amountJpy": 1960,
        })
        updated = of.allocate_order_shipping("丸進", "2026-08-26", 800)
        self.assertEqual(len(updated), 1)
        self.assertAlmostEqual(updated[0]["shippingCostJpy"], 800.0)

    def test_finance_summary_cogs_includes_allocated_shipping(self):
        of.set_asin_jan_map("B0TEST", "JAN5")
        of.upsert_jp_purchase_record({
            "orderDate": "2026-08-26", "sdReceptionNo": "R5", "supplierName": "丸進",
            "sdProductNo": None, "productName": "定規", "janCode": "JAN5", "variant": None,
            "unitPriceJpy": 200, "quantity": 10, "amountJpy": 2000,
        })
        of.allocate_order_shipping("丸進", "2026-08-26", 500)  # -> 送料/個 = ¥50
        of.upsert_sp_orders([{
            "orderId": "O1", "purchaseDate": "2026-09-01", "asin": "B0TEST", "sku": "SKU1",
            "quantity": 2, "itemPriceUsd": 10.0, "orderStatus": "Shipped",
        }])
        of.upsert_sp_order_items("O1", [{
            "asin": "B0TEST", "sku": "SKU1", "quantity": 2, "itemPriceUsd": 10.0,
        }])
        summary = of.compute_finance_summary(days=30, usd_to_jpy=150.0)
        # 着地原価/個 = ¥200(商品単価) + ¥50(送料按分) = ¥250 -> $250/150 * 2個 = $3.33...
        self.assertAlmostEqual(summary["cogsUsd"], (250 / 150.0) * 2, places=2)


class TestPerProductPnl(unittest.TestCase):
    """Phase 2/3: sp_order_items経由の商品ごとのP&L(get_per_product_pnl)。
    1注文に複数ASINが含まれる場合の手数料の数量按分も検証する。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = of.DB_PATH
        of.DB_PATH = Path(self._tmpdir.name) / "test.sqlite3"
        of.init_ops_tables()

    def tearDown(self):
        of.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    def test_single_asin_order_pnl(self):
        of.upsert_jp_purchase_record({
            "orderDate": "2026-08-26", "sdReceptionNo": "R1", "supplierName": "丸進",
            "sdProductNo": None, "productName": "定規", "janCode": "JAN1", "variant": None,
            "unitPriceJpy": 195, "quantity": 30, "amountJpy": 5850,
        })
        of.set_asin_jan_map("B0RULER", "JAN1")
        of.upsert_sp_orders([{
            "orderId": "O1", "purchaseDate": "2026-09-01", "asin": None, "sku": None,
            "quantity": None, "itemPriceUsd": None, "orderStatus": "Shipped",
        }])
        of.upsert_sp_order_items("O1", [{
            "asin": "B0RULER", "sku": "SKU1", "quantity": 2, "itemPriceUsd": 7.49,
        }])
        of.upsert_sp_financial_events("O1", [
            {"eventType": "Commission", "amountUsd": -2.0, "postedDate": "2026-09-01"},
        ])
        result = of.get_per_product_pnl(days=30, usd_to_jpy=150.0)
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row["asin"], "B0RULER")
        self.assertEqual(row["units"], 2)
        self.assertAlmostEqual(row["revenueUsd"], 14.98)
        self.assertAlmostEqual(row["feesUsd"], -2.0)
        self.assertAlmostEqual(row["cogsUsd"], (195 / 150.0) * 2, places=2)

    def test_multi_asin_order_splits_fees_by_quantity(self):
        of.upsert_sp_orders([{
            "orderId": "O2", "purchaseDate": "2026-09-01", "asin": None, "sku": None,
            "quantity": None, "itemPriceUsd": None, "orderStatus": "Shipped",
        }])
        of.upsert_sp_order_items("O2", [
            {"asin": "B0A", "sku": "SKU-A", "quantity": 1, "itemPriceUsd": 10.0},
            {"asin": "B0B", "sku": "SKU-B", "quantity": 3, "itemPriceUsd": 5.0},
        ])
        of.upsert_sp_financial_events("O2", [
            {"eventType": "Commission", "amountUsd": -4.0, "postedDate": "2026-09-01"},
        ])
        result = {row["asin"]: row for row in of.get_per_product_pnl(days=30)}
        # 手数料-4.0ドルが数量比(1:3)で按分される -> B0A=-1.0, B0B=-3.0
        self.assertAlmostEqual(result["B0A"]["feesUsd"], -1.0)
        self.assertAlmostEqual(result["B0B"]["feesUsd"], -3.0)


class TestShipmentPnl(unittest.TestCase):
    """Phase 4: FBA納品便(sp_inbound_shipments)ごとのP&L(get_shipment_pnl)。
    原価の紐付けと、売上のFIFO近似割り当てを検証する。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = of.DB_PATH
        of.DB_PATH = Path(self._tmpdir.name) / "test.sqlite3"
        of.init_ops_tables()

    def tearDown(self):
        of.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    def _shipment(self, shipment_id="sh1", confirmation_id="FBA1", window_start="2026-10-18T00:00Z"):
        return {
            "shipmentId": shipment_id, "planId": "plan1", "shipmentConfirmationId": confirmation_id,
            "status": "READY_TO_SHIP", "destinationFc": "HIA1",
            "deliveryWindowStart": window_start, "deliveryWindowEnd": "2026-10-24T23:59Z",
            "createdAt": "2026-09-29T00:00:00Z",
        }

    def test_cost_only_when_no_sales_yet(self):
        of.upsert_jp_purchase_record({
            "orderDate": "2026-08-26", "sdReceptionNo": "R1", "supplierName": "丸進",
            "sdProductNo": None, "productName": "定規", "janCode": "JAN1", "variant": None,
            "unitPriceJpy": 195, "quantity": 30, "amountJpy": 5850,
        })
        of.set_asin_jan_map("B0RULER", "JAN1")
        of.upsert_sp_inbound_shipment(self._shipment(), [{"asin": "B0RULER", "sku": "SKU1", "quantity": 30}])

        result = of.get_shipment_pnl(usd_to_jpy=150.0)
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row["shipmentConfirmationId"], "FBA1")
        self.assertEqual(row["unitsShipped"], 30)
        self.assertEqual(row["unitsSold"], 0)
        self.assertAlmostEqual(row["costUsd"], (195 / 150.0) * 30, places=2)
        self.assertAlmostEqual(row["revenueUsd"], 0.0)

    def test_fifo_allocates_sales_to_earliest_shipment_first(self):
        of.upsert_jp_purchase_record({
            "orderDate": "2026-08-26", "sdReceptionNo": "R2", "supplierName": "丸進",
            "sdProductNo": None, "productName": "定規", "janCode": "JAN2", "variant": None,
            "unitPriceJpy": 195, "quantity": 60, "amountJpy": 11700,
        })
        of.set_asin_jan_map("B0RULER2", "JAN2")
        # 2つの便(古い順: sh-old -> sh-new)、それぞれ10個ずつ出荷
        of.upsert_sp_inbound_shipment(
            self._shipment("sh-old", "FBA-OLD", "2026-10-01T00:00Z"),
            [{"asin": "B0RULER2", "sku": "SKU2", "quantity": 10}],
        )
        of.upsert_sp_inbound_shipment(
            self._shipment("sh-new", "FBA-NEW", "2026-10-15T00:00Z"),
            [{"asin": "B0RULER2", "sku": "SKU2", "quantity": 10}],
        )
        # 15個売れた注文(古い便の10個 + 新しい便の5個にまたがる想定)
        of.upsert_sp_orders([{
            "orderId": "O1", "purchaseDate": "2026-10-05", "asin": None, "sku": None,
            "quantity": None, "itemPriceUsd": None, "orderStatus": "Shipped",
        }])
        of.upsert_sp_order_items("O1", [{"asin": "B0RULER2", "sku": "SKU2", "quantity": 15, "itemPriceUsd": 7.49}])

        result = {row["shipmentConfirmationId"]: row for row in of.get_shipment_pnl(usd_to_jpy=150.0)}
        # 古い便(10個出荷)が先に売上を吸収 -> 10個分完売、新しい便は残り5個分だけ売れた扱い
        self.assertEqual(result["FBA-OLD"]["unitsSold"], 10)
        self.assertEqual(result["FBA-NEW"]["unitsSold"], 5)
        self.assertAlmostEqual(result["FBA-OLD"]["revenueUsd"], 7.49 * 10, places=2)
        self.assertAlmostEqual(result["FBA-NEW"]["revenueUsd"], 7.49 * 5, places=2)


if __name__ == "__main__":
    unittest.main()
