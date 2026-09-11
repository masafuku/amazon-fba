import unittest
from unittest.mock import patch

import keepa_mcp.server as server
from keepa_mcp.keepa_client import KeepaError


class TestEnrichQualifiedCandidatesWithOfferDetails(unittest.TestCase):
    """v2: この関数はもうセラー数を扱わない(§セラー数はstats.totalOfferCount
    から無料で取得、_summarize_product()経由)。在庫合計(competitor_stock_total)
    のみを担当する。"""

    def test_sets_stock_total_on_success(self):
        entries = [{"asin": "B0GOOD1"}]

        def fake_get_offers(api_key, asin, domain="US", include_stock=False, force_refresh=False):
            self.assertTrue(include_stock)
            return (
                {
                    "liveOffersOrder": [0, 1],
                    "offers": [
                        {"sellerId": "S1", "isAmazon": False, "stockCSV": [1000, 10]},
                        {"sellerId": "S2", "isAmazon": False, "stockCSV": [1000, 5]},
                    ],
                },
                {"hit": False},
            )

        with patch.object(server, "_require_api_key", return_value="fake-key"), \
             patch.object(server, "_wait_for_budget", return_value=999), \
             patch.object(server, "cached_get_product_with_offers", side_effect=fake_get_offers):
            result = server.enrich_qualified_candidates_with_offer_details(entries, wait_for_tokens=True)

        self.assertEqual(entries[0]["competitor_stock_total"], 15)
        self.assertEqual(result, {"enriched": 1, "failed": 0})
        self.assertNotIn("competitor_seller_count", entries[0])

    def test_keepa_error_on_one_asin_does_not_abort_batch(self):
        entries = [{"asin": "B0GOOD1"}, {"asin": "B0BAD001"}]

        def fake_get_offers(api_key, asin, domain="US", include_stock=False, force_refresh=False):
            if asin == "B0BAD001":
                raise KeepaError("simulated 429")
            return ({"liveOffersOrder": [], "offers": []}, {"hit": False})

        with patch.object(server, "_require_api_key", return_value="fake-key"), \
             patch.object(server, "_wait_for_budget", return_value=999), \
             patch.object(server, "cached_get_product_with_offers", side_effect=fake_get_offers):
            result = server.enrich_qualified_candidates_with_offer_details(entries, wait_for_tokens=True)

        self.assertIsNone(entries[1]["competitor_stock_total"])
        self.assertEqual(result["failed"], 1)


class TestRecomputeSellerCount(unittest.TestCase):
    def test_reads_total_offer_count_from_normal_fetch(self):
        def fake_get_products(api_key, domain="US", asins=None, force_refresh=False):
            return ([{"asin": asins[0], "stats": {"totalOfferCount": 2}}], {})

        with patch.object(server, "_require_api_key", return_value="fake-key"), \
             patch.object(server, "cached_get_products", side_effect=fake_get_products):
            count = server.recompute_seller_count("B0TEST")

        self.assertEqual(count, 2)

    def test_no_product_found_returns_none(self):
        def fake_get_products(api_key, domain="US", asins=None, force_refresh=False):
            return ([], {})

        with patch.object(server, "_require_api_key", return_value="fake-key"), \
             patch.object(server, "cached_get_products", side_effect=fake_get_products):
            count = server.recompute_seller_count("B0TEST")

        self.assertIsNone(count)


class TestSummarizeProductIncludesDemandSignal(unittest.TestCase):
    """CEO: 「この記事を参考にして、今の実装に対して取り入れられ所はある？」への
    対応 -- _summarize_product()経由の全MCPツール結果にdemand_signalが自動で
    乗ること(get_product_detail/find_arbitrage_candidates/expand_from_seller/
    investigate_asinすべてがこの関数を通る)を確認する。"""

    def test_summary_includes_demand_signal_key(self):
        product = {
            "asin": "B0TEST",
            "monthlySold": 500,
            "stats": {"current": [None, None, None, 1234]},
        }
        summary = server._summarize_product(product, "US")

        self.assertIn("demand_signal", summary)
        self.assertEqual(summary["demand_signal"]["primary_value"], 500)
        self.assertEqual(summary["demand_signal"]["confidence"], "high")

    def test_summary_includes_brand_store_key(self):
        product = {
            "asin": "B0TEST",
            "brand": "HARIO",
            "brandStoreName": "HARIO",
            "brandStoreUrl": "/stores/Hario/page/xyz",
        }
        summary = server._summarize_product(product, "US")

        self.assertIn("brand_store", summary)
        self.assertTrue(summary["brand_store"]["has_brand_store"])


if __name__ == "__main__":
    unittest.main()
