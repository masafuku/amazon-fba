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
        summary = of.compute_finance_summary(days=30, usd_to_jpy=150.0)
        # 着地原価/個 = ¥200(商品単価) + ¥50(送料按分) = ¥250 -> $250/150 * 2個 = $3.33...
        self.assertAlmostEqual(summary["cogsUsd"], (250 / 150.0) * 2, places=2)


if __name__ == "__main__":
    unittest.main()
