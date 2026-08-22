import unittest

from keepa_mcp.analysis import (
    _live_offers,
    distinct_seller_ids,
    sales_rank_drops_90,
    total_live_stock,
    total_offer_count,
)


class TestTotalOfferCount(unittest.TestCase):
    """CEO: 「セラー数が38となっていますが、keepaで直接見た値と明らかに違います。
    調査おねがいします。」— 実データ検証の結果、通常の商品取得(offers=N不要)に
    含まれるstats.totalOfferCountが、正しいライブなセラー数を無料で返すと判明。
    """

    def test_reads_total_offer_count(self):
        product = {"stats": {"totalOfferCount": 2}}
        self.assertEqual(total_offer_count(product), 2)

    def test_negative_sentinels_normalize_to_none(self):
        for sentinel in (-1, -2):
            with self.subTest(sentinel=sentinel):
                product = {"stats": {"totalOfferCount": sentinel}}
                self.assertIsNone(total_offer_count(product))

    def test_missing_stats_returns_none(self):
        self.assertIsNone(total_offer_count({}))


class TestLiveOffersFiltering(unittest.TestCase):
    """実データ検証(ASIN B07V31TRKB): 生のoffers配列41件のうち、実際にライブ
    なのは2件のみ(liveOffersOrderが指すインデックスの要素、lastSeen==stats.
    lastOffersUpdateとも一致)。旧実装はこの絞り込みをせず41件から38セラーを
    数えてしまっていた。"""

    def _make_product(self):
        # 3件の古い(stale)オファー + 2件のライブなオファー、というインデックス
        # 構成(liveOffersOrder=[3, 4])。offerIdは意図的にインデックスと
        # ずらしてある(liveOffersOrderがofferIdではなく配列インデックスである
        # ことを検証するため)。
        return {
            "liveOffersOrder": [3, 4],
            "stats": {"lastOffersUpdate": 9999},
            "offers": [
                {"offerId": 30, "sellerId": "STALE1", "lastSeen": 1000, "isAmazon": False},
                {"offerId": 31, "sellerId": "STALE2", "lastSeen": 2000, "isAmazon": False},
                {"offerId": 32, "sellerId": "STALE3", "lastSeen": 3000, "isAmazon": False},
                {"offerId": 3, "sellerId": "LIVE1", "lastSeen": 9999, "isAmazon": False},
                {"offerId": 4, "sellerId": "LIVE2", "lastSeen": 9999, "isAmazon": True},
            ],
        }

    def test_live_offers_uses_array_index_not_offer_id(self):
        product = self._make_product()
        live = _live_offers(product)
        self.assertEqual(len(live), 2)
        self.assertEqual({o["sellerId"] for o in live}, {"LIVE1", "LIVE2"})

    def test_distinct_seller_ids_excludes_stale_offers(self):
        product = self._make_product()
        # exclude_amazon=True (default) もLIVE2(isAmazon=True)を除外する
        self.assertEqual(distinct_seller_ids(product), ["LIVE1"])

    def test_distinct_seller_ids_can_include_amazon(self):
        product = self._make_product()
        self.assertEqual(distinct_seller_ids(product, exclude_amazon=False), ["LIVE1", "LIVE2"])

    def test_falls_back_to_last_seen_when_no_live_offers_order(self):
        product = self._make_product()
        del product["liveOffersOrder"]
        live = _live_offers(product)
        self.assertEqual({o["sellerId"] for o in live}, {"LIVE1", "LIVE2"})

    def test_no_offers_returns_empty(self):
        self.assertEqual(_live_offers({}), [])
        self.assertEqual(distinct_seller_ids({}), [])


class TestTotalLiveStock(unittest.TestCase):
    def test_sums_last_stock_value_across_live_offers(self):
        product = {
            "liveOffersOrder": [0, 1],
            "offers": [
                {"sellerId": "S1", "stockCSV": [1000, 48]},
                {"sellerId": "S2", "stockCSV": [1000, 5, 2000, 16]},
            ],
        }
        self.assertEqual(total_live_stock(product), 64)

    def test_offer_without_stock_csv_is_skipped_not_treated_as_zero(self):
        product = {
            "liveOffersOrder": [0, 1],
            "offers": [
                {"sellerId": "S1", "stockCSV": [1000, 10]},
                {"sellerId": "S2"},  # stock=1を付けずに取得した場合など
            ],
        }
        self.assertEqual(total_live_stock(product), 10)

    def test_no_live_offers_returns_none(self):
        self.assertIsNone(total_live_stock({"offers": []}))

    def test_no_stock_data_at_all_returns_none_not_zero(self):
        product = {
            "liveOffersOrder": [0],
            "offers": [{"sellerId": "S1"}],
        }
        self.assertIsNone(total_live_stock(product))


class TestSalesRankDrops90(unittest.TestCase):
    def test_reads_normal_value(self):
        self.assertEqual(sales_rank_drops_90({"stats": {"salesRankDrops90": 24}}), 24)

    def test_sentinel_normalizes_to_none(self):
        self.assertIsNone(sales_rank_drops_90({"stats": {"salesRankDrops90": -1}}))

    def test_missing_returns_none(self):
        self.assertIsNone(sales_rank_drops_90({}))


if __name__ == "__main__":
    unittest.main()
