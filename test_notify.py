import unittest

from notify_line import build_line_message


class TestNotifyLine(unittest.TestCase):
    def test_build_line_message_empty(self):
        message = build_line_message([])
        self.assertIn("差額30%以上の商品は見つかりませんでした", message)

    def test_build_line_message_single(self):
        results = [
            {
                "asin": "B00EXAMPLE",
                "title": "Example Product",
                "us_price": 100.0,
                "us_price_jpy": 15000.0,
                "jp_price": 20000.0,
                "price_diff_percent": 0.3333,
            }
        ]
        message = build_line_message(results)
        self.assertIn("ASIN: B00EXAMPLE", message)
        self.assertIn("US: $100.00 (¥15000)", message)
        self.assertIn("JP: ¥20000", message)
        self.assertIn("差額: 33.3%", message)
