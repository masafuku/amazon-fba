import unittest
from unittest import mock

import notify_line
from notify_line import build_line_message


class TestSendLineMessage(unittest.TestCase):
    def test_list_becomes_multiple_bubbles_in_one_broadcast(self):
        with mock.patch.object(notify_line, "_post") as post:
            notify_line.send_line_message("token", ["在庫", "", "候補"])
        path, _, payload = post.call_args[0]
        self.assertEqual(path, "/broadcast")
        self.assertEqual(payload["messages"], [{"type": "text", "text": "在庫"}, {"type": "text", "text": "候補"}])
        self.assertEqual(post.call_count, 1)

    def test_single_string_still_works(self):
        with mock.patch.object(notify_line, "_post") as post:
            notify_line.send_line_message("token", "x" * 6000, user_id="U1")
        path, _, payload = post.call_args[0]
        self.assertEqual(path, "/push")
        self.assertEqual(len(payload["messages"][0]["text"]), 5000)


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
