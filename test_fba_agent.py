import gzip
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from config import Settings, load_dotenv
from fetch_keepa import fetch_keepa_products, map_keepa_product
from jp_amazon import parse_price
from us_amazon import build_us_search_url


class TestConfigLoad(unittest.TestCase):
    def test_load_env_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(
                    "KEEPA_API_KEY=testkey\n"
                    "EMAIL_SMTP_HOST=smtp.example.com\n"
                    "EMAIL_SMTP_PORT=587\n"
                    "EMAIL_USERNAME=test@example.com\n"
                    "EMAIL_PASSWORD=secret\n"
                    "EMAIL_FROM=test@example.com\n"
                    "EMAIL_TO=recipient@example.com\n"
                    "SEARCH_KEYWORD=import japan\n"
                    "SEARCH_CATEGORY=All\n"
                    "US_MAX_RESULTS=10\n"
                    "PRICE_DIFF_THRESHOLD=0.25\n"
                    "USD_TO_JPY=140.0\n"
                    "SALES_RANK_THRESHOLD=30000\n"
                )
            original_env = dict(os.environ)
            try:
                os.environ.pop("KEEPA_API_KEY", None)
                os.environ.pop("EMAIL_SMTP_HOST", None)
                os.environ.pop("EMAIL_SMTP_PORT", None)
                os.environ.pop("EMAIL_USERNAME", None)
                os.environ.pop("EMAIL_PASSWORD", None)
                os.environ.pop("EMAIL_FROM", None)
                os.environ.pop("EMAIL_TO", None)
                os.environ.pop("SEARCH_KEYWORD", None)
                os.environ.pop("SEARCH_CATEGORY", None)
                os.environ.pop("US_MAX_RESULTS", None)
                os.environ.pop("PRICE_DIFF_THRESHOLD", None)
                os.environ.pop("USD_TO_JPY", None)
                os.environ.pop("SALES_RANK_THRESHOLD", None)
                load_dotenv(env_path)
                settings = Settings.load()
            finally:
                os.environ.clear()
                os.environ.update(original_env)

            self.assertEqual(settings.keepa_api_key, "testkey")
            self.assertEqual(settings.email_smtp_host, "smtp.example.com")
            self.assertEqual(settings.email_smtp_port, 587)
            self.assertEqual(settings.search_keyword, "import japan")
            self.assertEqual(settings.us_max_results, 10)
            self.assertEqual(settings.price_diff_threshold, 0.25)
            self.assertEqual(settings.usd_to_jpy, 140.0)
            self.assertEqual(settings.sales_rank_threshold, 30000)


class TestPriceParsing(unittest.TestCase):
    def test_parse_price_yen(self):
        self.assertEqual(parse_price("¥ 12,345"), 12345.0)
        self.assertEqual(parse_price("12,345円"), 12345.0)
        self.assertEqual(parse_price("¥12,345"), 12345.0)
        self.assertEqual(parse_price("12,345"), 12345.0)


class TestBuildUsSearchUrl(unittest.TestCase):
    def test_build_search_url(self):
        url = build_us_search_url("import japan", "electronics")
        self.assertIn("k=import+japan", url)
        self.assertIn("i=electronics", url)


class TestMapKeepaProduct(unittest.TestCase):
    def test_map_keepa_product_buybox(self):
        product = {
            "asin": "B00EXAMPLE",
            "title": "Example Product",
            "brand": "Example Brand",
            "buyBoxPrice": 12345,
            "listPrice": 23456,
            "offers": [{"isFBA": True}],
            "stats": {"current": {"offerSalesRank": 1234}},
        }
        mapped = map_keepa_product(product)
        self.assertEqual(mapped["us_price"], 123.45)
        self.assertTrue(mapped["is_fba"])
        self.assertEqual(mapped["sales_rank"], 1234)
        self.assertEqual(mapped["product_url"], "https://www.amazon.com/dp/B00EXAMPLE")

    def test_map_keepa_product_listprice(self):
        product = {
            "asin": "B00EXAMPLE",
            "title": "Example Product",
            "brand": "Example Brand",
            "buyBoxPrice": 0,
            "listPrice": 23456,
            "offers": [],
            "stats": {"current": {"salesRank": 5678}},
        }
        mapped = map_keepa_product(product)
        self.assertEqual(mapped["us_price"], 234.56)
        self.assertEqual(mapped["sales_rank"], 5678)


class TestFetchKeepaProducts(unittest.TestCase):
    def test_fetch_keepa_products_decompresses_gzip_response(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({"products": [{"asin": "B00EXAMPLE", "title": "Example Product"}]}).encode("utf-8")
                compressed = gzip.compress(payload)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(compressed)))
                self.end_headers()
                self.wfile.write(compressed)

            def log_message(self, format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("fetch_keepa.KEEPA_API_URL", f"http://127.0.0.1:{server.server_port}/product"):
                products = fetch_keepa_products("dummy", ["B00EXAMPLE"])
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(products[0]["asin"], "B00EXAMPLE")
        self.assertEqual(products[0]["title"], "Example Product")
