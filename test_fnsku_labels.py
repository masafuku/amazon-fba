import tempfile
import unittest
from pathlib import Path

import fnsku_labels as fl


class TestFnskuLabels(unittest.TestCase):
    def test_expand_and_page_count(self):
        items = [{'fnsku': 'X001', 'title': 't', 'quantity': 15}, {'fnsku': 'X002', 'title': 't', 'quantity': 26}]
        labels = fl.expand(items)
        self.assertEqual(len(labels), 41)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'a.pdf')
            self.assertEqual(fl.build_pdf(labels, path), 2)                    # 40面なので41枚は2ページ
            self.assertEqual(fl.build_pdf(labels[:40], path), 1)
            self.assertEqual(fl.build_pdf(labels[:40], path, start_cell=1), 2)  # 使いかけシートの2枚目から
            self.assertEqual(fl.build_pdf([], path, grid=True), 1)
            self.assertTrue(Path(path).stat().st_size > 0)

    def test_spare_labels(self):
        self.assertEqual(len(fl.expand([{'fnsku': 'X', 'title': '', 'quantity': 3}], spare=2)), 5)

    def test_short_title_is_ascii_and_limited(self):
        t = fl.short_title('Sanrio ' + 'x' * 100 + ' ™ 日本語')
        self.assertTrue(t.isascii())
        self.assertLessEqual(len(t), 62)


if __name__ == '__main__':
    unittest.main()
