import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))

import ops_finance
from ops_finance import candidate_excluded_kind as kind
import backfill_exclusions as bf


class TestExclusionKinds(unittest.TestCase):
    def test_muji_by_title_or_brand(self):
        self.assertEqual(kind('MUJI Gel Ink Ballpoint Pen 0.5mm'), 'muji')
        self.assertEqual(kind('Gel Pen Black 0.5', '無印良品'), 'muji')

    def test_electronics_titles(self):
        for title in ("Panasonic Men's Shaver ES-RS10-S", 'Vessel Electric Screwdriver Ball Grip', 'Panasonic Clip Headphones RP-HZ47',
                      'Pentel Electric Eraser', 'ミニ 充電式 ファン'):
            self.assertEqual(kind(title), 'electronics', title)

    def test_stationery_and_goods_are_not_excluded(self):
        for title in ('Kokuyo Campus Slide Binder B5', 'Uni Kuru Toga Mechanical Pencil Metal', "Midori Traveler's Notebook Passport Size",
                      'Zojirushi Stainless Water Bottle 500ml', 'Tea Kettle Stainless Steel', 'Sanrio Slim Ruler 15cm Kitty'):
            self.assertIsNone(kind(title), title)

    def test_cosmetics_still_excluded(self):
        self.assertEqual(kind('CANMAKE Creamy Touch Liner'), 'drug_cosmetic')

    def test_apply_priority_fields_uses_brand_for_muji(self):
        entry = ops_finance._apply_priority_fields({'title': 'Gel Pen Black 0.5', 'brand': 'MUJI', 'roi_pct': 1.2, 'monthly_sold': 150})
        self.assertEqual((entry['excluded_kind'], entry['priority_tier']), ('muji', None))


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 'test.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            for asin, title, tier, brand, excluded in (
                ('M1', 'Gel Pen Black', 'S', 'MUJI', None),
                ('E1', 'Panasonic Shaver', 'A+', None, None),
                ('K1', 'Kokuyo Binder', 'A+', 'Kokuyo', None),
                ('C0', 'Cosmetic Already', None, None, 'drug_cosmetic'),
            ):
                conn.execute(
                    "INSERT INTO agent_candidates (run_id, asin, title, qualified, data_json, created_at, priority_tier, excluded_kind) "
                    "VALUES ('r', ?, ?, 1, ?, '2026-10-01', ?, ?)", (asin, title, json.dumps({'brand': brand}), tier, excluded))

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_finds_only_new_exclusions_and_apply_matches_existing_excluded_shape(self):
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            found = bf.find_new_exclusions(conn)
        self.assertEqual(sorted((f[1], f[4]) for f in found), [('E1', 'electronics'), ('M1', 'muji')])
        with mock.patch.object(sys, 'argv', ['x', '--apply']), mock.patch.object(bf, 'DB_PATH', ops_finance.DB_PATH):
            bf.main()
        with sqlite3.connect(ops_finance.DB_PATH) as conn:
            rows = {r[0]: r[1:] for r in conn.execute('SELECT asin, excluded_kind, priority_tier, qualified, reason FROM agent_candidates')}
        self.assertEqual(rows['M1'], ('muji', None, 0, 'excluded_muji'))
        self.assertEqual(rows['E1'], ('electronics', None, 0, 'excluded_electronics'))
        self.assertEqual(rows['K1'], (None, 'A+', 1, None))     # 触らない
        self.assertEqual(rows['C0'], ('drug_cosmetic', None, 1, None))   # 既に除外の行は触らない


if __name__ == '__main__':
    unittest.main()
