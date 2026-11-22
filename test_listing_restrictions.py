import unittest
from unittest import mock

from sp_api.restrictions import summarize_restrictions
from sp_api import client


class TestSummarizeRestrictions(unittest.TestCase):
    def test_empty_restrictions_means_listable(self):
        self.assertEqual(summarize_restrictions({"restrictions": []}), ("ok", []))
        self.assertEqual(summarize_restrictions({}), ("ok", []))

    def test_approval_required_includes_link(self):
        payload = {"restrictions": [{"reasons": [{
            "reasonCode": "APPROVAL_REQUIRED", "message": "You need approval to list this brand.",
            "links": [{"resource": "https://sellercentral.amazon.com/hz/approvalrequest/restrictions/approve?asin=B004O7GLL2"}],
        }]}]}
        status, details = summarize_restrictions(payload)
        self.assertEqual(status, "approval_required")
        self.assertTrue(any("approvalrequest" in line for line in details))

    def test_brand_only_product_only_and_both_are_distinguished(self):
        brand = {"reasonCode": "APPROVAL_REQUIRED", "message": "このブランドには出品許可が必要です。", "links": []}
        product = {"reasonCode": "APPROVAL_REQUIRED", "message": "この商品を出品するための承認が必要です。", "links": []}
        # Holbein W203 (B004O7GLL2): ブランドのみ
        self.assertEqual(summarize_restrictions({"restrictions": [{"reasons": [brand]}]})[0], "approval_required")
        # セザンヌ (B07H97J6TP): 商品のみ
        self.assertEqual(summarize_restrictions({"restrictions": [{"reasons": [product]}]})[0], "product_approval_required")
        # Holbein ガッシュ (B075YJLKRZ): ブランド + 商品
        self.assertEqual(summarize_restrictions({"restrictions": [{"reasons": [brand, product]}]})[0], "brand_and_product_approval_required")
        english = {"reasonCode": "APPROVAL_REQUIRED", "message": "You need approval to list this product.", "links": []}
        self.assertEqual(summarize_restrictions({"restrictions": [{"reasons": [english]}]})[0], "product_approval_required")

    def test_not_eligible(self):
        payload = {"restrictions": [{"reasons": [{"reasonCode": "NOT_ELIGIBLE", "message": "Not eligible."}]}]}
        self.assertEqual(summarize_restrictions(payload)[0], "not_eligible")


class TestGetListingsRestrictions(unittest.TestCase):
    def test_requires_seller_id(self):
        with mock.patch.object(client.settings, "seller_id", ""):
            with self.assertRaises(client.SpApiError):
                client.get_listings_restrictions("B004O7GLL2")

    def test_calls_the_restrictions_endpoint_with_expected_params(self):
        with mock.patch.object(client.settings, "seller_id", "A1SELLER"), \
                mock.patch.object(client, "_request", return_value={"restrictions": []}) as request:
            result = client.get_listings_restrictions("B004O7GLL2")
        self.assertEqual(result, {"restrictions": []})
        path, params = request.call_args[0]
        self.assertEqual(path, "/listings/2021-08-01/restrictions")
        self.assertEqual(params["asin"], "B004O7GLL2")
        self.assertEqual(params["sellerId"], "A1SELLER")
        self.assertEqual(params["conditionType"], "new_new")


if __name__ == "__main__":
    unittest.main()
