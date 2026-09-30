import unittest

from notify_line import build_line_message


class TestNotifyLine(unittest.TestCase):
    def test_build_line_message_empty(self):
        message = build_line_message([])
        self.assertIn("該当する商品は見つかりませんでした", message)

    def test_build_line_message_single(self):
        # main.py の find_price_gap_products() が実際に返す形に合わせたfixture
        # (us_url/jp_url を含む)。
        results = [
            {
                "asin": "B00EXAMPLE",
                "title": "Example Product",
                "us_price": 100.0,
                "us_price_jpy": 15000.0,
                "jp_price": 20000.0,
                "price_diff_percent": 0.3333,
                "us_url": "https://www.amazon.com/dp/B00EXAMPLE",
                "jp_url": "https://www.amazon.co.jp/dp/B00EXAMPLE",
            }
        ]
        message = build_line_message(results)
        self.assertIn("ASIN: B00EXAMPLE", message)
        self.assertIn("US Price: $100.00", message)
        self.assertIn("JP Price: ¥20000", message)
        self.assertIn("差額率: 33.3%", message)
        self.assertIn("US URL: https://www.amazon.com/dp/B00EXAMPLE", message)
