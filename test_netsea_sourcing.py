import unittest
from unittest.mock import patch

import netsea_sourcing


class FakeAnalysis:
    """keepa_mcp.analysis のうち netsea_sourcing.py が使う関数だけを、テスト用の
    素朴なdictキー読み出しに差し替えたもの(product dictは各テストで自作する)。"""

    @staticmethod
    def current_price(product, domain="US"):
        return product.get("price")

    @staticmethod
    def package_weight_kg(product):
        return product.get("weight_kg", 0.1)

    @staticmethod
    def referral_fee_percent(product):
        return None

    @staticmethod
    def fba_pickpack_fee(product, domain="US"):
        return None

    @staticmethod
    def sales_rank(product):
        return product.get("sales_rank")

    @staticmethod
    def review_count(product):
        return None

    @staticmethod
    def monthly_sold(product):
        return product.get("monthly_sold")

    @staticmethod
    def sales_rank_drops_30(product):
        return None

    @staticmethod
    def sales_rank_drops_90(product):
        return None

    @staticmethod
    def total_offer_count(product):
        return None

    @staticmethod
    def demand_signal(product, domain="US"):
        monthly = product.get("monthly_sold")
        rank = product.get("sales_rank")
        return {
            "primary_value": monthly if monthly is not None else rank,
            "primary_label": "テスト用",
            "confidence": "high" if monthly is not None else ("low" if rank is not None else "none"),
            "monthly_sold": monthly, "sales_rank_drops_30": None, "sales_rank_drops_90": None,
            "sales_rank": rank, "competitor_seller_count": None,
        }

    @staticmethod
    def brand_store_info(product):
        return {"has_brand_store": False, "brand_store_name": None, "brand_store_url": None}

    @staticmethod
    def price_volatility_ratio(product, domain="US"):
        return None

    @staticmethod
    def product_url(asin, domain="US"):
        return f"https://example.com/{asin}"

    @staticmethod
    def product_image_url(product):
        return None

    @staticmethod
    def rating(product):
        return None


def _netsea_item(jan_code, price_jpy, category_id="20110"):
    """netsea_client.get_items() が返す生の商品shapeを模したダミーデータ。"""
    return {
        "jan_code": jan_code,
        "product_name": f"商品-{jan_code}",
        "shop_name": "テスト卸業者",
        "supplier_id": "SUP1",
        "product_url": f"https://netsea.example/{jan_code}",
        "set": [{"sold_out_flag": "N", "price": price_jpy}],
    }


class TestFindJanMatchedCandidatesPersistence(unittest.TestCase):
    """CEO: 「すくなくとも細かく結果を保存する形にして」「途中で止めても
    リエントラントにしたい」への対応を検証する。"""

    def setUp(self):
        # 合格1件(JAN1: US$100 vs JP\1500 -> 利益率・ROIとも大幅に基準超え)、
        # 不合格2件(JAN2/JAN3: US価格と原価がほぼ同額 -> 利益なし)。
        self.items_by_category = {
            "20110": [
                _netsea_item("JAN1", 1500),
                _netsea_item("JAN2", 3000),
                _netsea_item("JAN3", 2500),
            ]
        }
        self.products_by_jan = {
            # 需要データ(monthly_sold)も伴っているので、需要シグナルの足切りを
            # 追加した後もPASSのままであることを確認する。
            "JAN1": {"asin": "ASIN1", "title": "Good Item", "price": 100.0, "weight_kg": 0.1, "monthly_sold": 200},
            "JAN2": {"asin": "ASIN2", "title": "Bad Item", "price": 20.0, "weight_kg": 0.1},
            "JAN3": {"asin": "ASIN3", "title": "Bad Item 2", "price": 16.5, "weight_kg": 0.1},
        }

    def _fake_lookup(self, api_key, jan, domain="US"):
        if domain == "JP":
            return ([], {})  # JP側は常に未マッチ(卸価格をそのまま使う経路をテスト)
        product = self.products_by_jan.get(jan)
        return (([product] if product else []), {})

    def _run(self, **overrides):
        kwargs = dict(
            api_key="fake-key",
            netsea_token="fake-token",
            category_ids=["20110"],
            category_labels={"20110": "文具"},
            wait_for_tokens=False,
        )
        kwargs.update(overrides)
        return netsea_sourcing.find_jan_matched_candidates(**kwargs)

    def test_persists_one_call_per_jan_not_batched(self):
        with patch.object(netsea_sourcing, "analysis", FakeAnalysis), \
             patch.object(netsea_sourcing, "init_ops_tables"), \
             patch.object(netsea_sourcing, "fetch_supplier_pool", return_value=[]), \
             patch.object(netsea_sourcing, "find_netsea_items_for_categories", return_value=self.items_by_category), \
             patch.object(netsea_sourcing, "_already_evaluated_jans", return_value=set()), \
             patch.object(netsea_sourcing, "cached_lookup_by_code", side_effect=self._fake_lookup), \
             patch.object(netsea_sourcing, "enrich_qualified_candidates_with_offer_details"), \
             patch.object(netsea_sourcing, "persist_agent_run") as mock_persist:
            result = self._run()

        # 3件のJANそれぞれで1回ずつ、まとめてではなく個別に保存されること。
        self.assertEqual(mock_persist.call_count, 3)
        for call in mock_persist.call_args_list:
            evaluation = call.args[1]
            total_in_this_call = len(evaluation.get("qualified", [])) + len(evaluation.get("rejected", []))
            self.assertEqual(total_in_this_call, 1, "1回のpersist_agent_run呼び出しは1件のみを含むべき")

        self.assertEqual(len(result["qualified"]), 1)
        self.assertEqual(len(result["rejected"]), 2)
        self.assertEqual(result["skipped_already_done"], 0)
        self.assertIn("run_id", result)

    def test_earlier_items_already_persisted_when_later_item_errors(self):
        # JAN1(1件目)処理後にpersist_agent_runが呼ばれ、2回目の呼び出し
        # (JAN2の保存)で例外が起きても、1件目は既にDBに書き込まれた後である
        # ことを検証する(=中断されても保存漏れがないことの根拠)。
        with patch.object(netsea_sourcing, "analysis", FakeAnalysis), \
             patch.object(netsea_sourcing, "init_ops_tables"), \
             patch.object(netsea_sourcing, "fetch_supplier_pool", return_value=[]), \
             patch.object(netsea_sourcing, "find_netsea_items_for_categories", return_value=self.items_by_category), \
             patch.object(netsea_sourcing, "_already_evaluated_jans", return_value=set()), \
             patch.object(netsea_sourcing, "cached_lookup_by_code", side_effect=self._fake_lookup), \
             patch.object(netsea_sourcing, "enrich_qualified_candidates_with_offer_details"), \
             patch.object(
                 netsea_sourcing, "persist_agent_run",
                 side_effect=[None, RuntimeError("simulated crash/kill mid-run")],
             ) as mock_persist:
            with self.assertRaises(RuntimeError):
                self._run()

        # 例外発生時点で、1件目(JAN1)はすでにpersist_agent_run済み。
        self.assertEqual(mock_persist.call_count, 2)

    def test_skips_already_evaluated_jans_without_spending_tokens(self):
        with patch.object(netsea_sourcing, "analysis", FakeAnalysis), \
             patch.object(netsea_sourcing, "init_ops_tables"), \
             patch.object(netsea_sourcing, "fetch_supplier_pool", return_value=[]), \
             patch.object(netsea_sourcing, "find_netsea_items_for_categories", return_value=self.items_by_category), \
             patch.object(netsea_sourcing, "_already_evaluated_jans", return_value={"JAN1"}), \
             patch.object(netsea_sourcing, "cached_lookup_by_code", side_effect=self._fake_lookup) as mock_lookup, \
             patch.object(netsea_sourcing, "enrich_qualified_candidates_with_offer_details"), \
             patch.object(netsea_sourcing, "persist_agent_run") as mock_persist:
            result = self._run()

        # JAN1は既に評価済みなのでKeepaを一切呼ばずスキップされる。
        called_jans = {call.args[1] for call in mock_lookup.call_args_list}
        self.assertNotIn("JAN1", called_jans)
        self.assertEqual(result["skipped_already_done"], 1)
        # 残り2件(JAN2/JAN3)のみ保存される。
        self.assertEqual(mock_persist.call_count, 2)


class TestWaitForTokenAboveReserve(unittest.TestCase):
    """CEO: 「トークンは使い切らないで」— 予備(TOKEN_RESERVE)を下回らない
    水準まで待つこと(単に1トークンあるかどうかではないこと)を検証する。"""

    def test_waits_until_balance_exceeds_reserve(self):
        statuses = [
            {"tokens_left": 9, "refill_rate_per_minute": 60},   # 予備(10)未満 -> 待つ
            {"tokens_left": 10, "refill_rate_per_minute": 60},  # ちょうど予備 -> まだ待つ(11必要)
            {"tokens_left": 11, "refill_rate_per_minute": 60},  # 予備を超えた -> 進む
        ]
        with patch.object(netsea_sourcing, "get_token_status", side_effect=statuses) as mock_status, \
             patch.object(netsea_sourcing.time, "sleep") as mock_sleep:
            netsea_sourcing._wait_for_token_above_reserve("fake-key", reserve=10)

        self.assertEqual(mock_status.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)


class TestRunNetseaSourcingCycleRunningStatus(unittest.TestCase):
    """CEO: 「どこまで進んだ？」に答えられるよう、実行開始直後に'running'行を
    記録し、完了時に更新することを検証する(daily_scan.pyと同じパターン)。"""

    def test_log_agent_run_called_at_start_and_end(self):
        fake_result = {
            "qualified": [], "rejected": [], "category_items": 0,
            "jan_candidates": 0, "matched": 0, "skipped_already_done": 0,
            "run_id": "agent-test-run",
        }
        with patch.object(netsea_sourcing, "init_ops_tables"), \
             patch.object(netsea_sourcing, "find_jan_matched_candidates", return_value=fake_result), \
             patch.object(netsea_sourcing, "log_agent_run") as mock_log:
            netsea_sourcing.run_netsea_sourcing_cycle(
                api_key="fake-key", netsea_token="fake-token", category_ids=["20110"],
            )

        self.assertEqual(mock_log.call_count, 2)
        first_call_kwargs = mock_log.call_args_list[0].kwargs
        self.assertEqual(first_call_kwargs.get("status"), "running")
        second_call_kwargs = mock_log.call_args_list[1].kwargs
        self.assertNotEqual(second_call_kwargs.get("status"), "running")


if __name__ == "__main__":
    unittest.main()
