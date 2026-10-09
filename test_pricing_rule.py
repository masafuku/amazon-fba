import unittest
from unittest import mock

import pricing_rule
from sp_api import client


class TestComputeBounds(unittest.TestCase):
    def test_min_price_is_break_even(self):
        minimum, maximum = pricing_rule.compute_bounds(unit_cost_jpy=300, current_price_usd=9.99)
        cost_usd = 300 / 150
        profit_at_min = minimum * (1 - 0.15) - 3.5 - 0.5 - cost_usd * 0.125 - cost_usd
        self.assertGreaterEqual(profit_at_min, 0)
        self.assertLess(profit_at_min, 0.02)
        profit_below = (minimum - 0.05) * (1 - 0.15) - 3.5 - 0.5 - cost_usd * 0.125 - cost_usd
        self.assertLess(profit_below, 0)
        self.assertEqual(maximum, 19.98)

    def test_max_never_below_min(self):
        minimum, maximum = pricing_rule.compute_bounds(unit_cost_jpy=3000, current_price_usd=5.0)
        self.assertGreater(maximum, minimum)


class TestActualFees(unittest.TestCase):
    def test_uses_actual_fba_fee_and_reaches_break_even(self):
        fee_fn = lambda price: {"referral": price * 0.15, "fba": 2.52, "other": 0.0}
        minimum, _ = pricing_rule.compute_bounds(326, 7.49, fee_fn)
        cost_usd = 326 / 150
        other = 0.5 + cost_usd * 0.125 + cost_usd
        self.assertAlmostEqual(minimum, (2.52 + other) / 0.85, delta=0.011)
        self.assertGreaterEqual(minimum - minimum * 0.15 - 2.52 - other, 0)

    def test_follows_price_dependent_fba_fee(self):
        # 10ドル以下はFBA手数料が安く、超えると高くなる想定
        fee_fn = lambda price: {"referral": price * 0.15, "fba": 2.0 if price <= 10 else 5.0, "other": 0.0}
        minimum, _ = pricing_rule.compute_bounds(300, 20.0, fee_fn)
        self.assertLessEqual(minimum, 10)


class TestFeesEstimate(unittest.TestCase):
    def test_parses_fee_details(self):
        payload = {"payload": {"FeesEstimateResult": {"Status": "Success", "FeesEstimate": {
            "TotalFeesEstimate": {"Amount": 3.66},
            "FeeDetailList": [{"FeeType": "ReferralFee", "FeeAmount": {"Amount": 1.14}},
                              {"FeeType": "FBAFees", "FeeAmount": {"Amount": 2.52}}]}}}}
        with mock.patch.object(client, "_request", return_value=payload):
            self.assertEqual(client.get_fees_estimate("B0X", 7.59), {"referral": 1.14, "fba": 2.52, "total": 3.66, "other": 0.0})

    def test_failure_raises(self):
        with mock.patch.object(client, "_request", return_value={"payload": {"FeesEstimateResult": {"Status": "ClientError", "Error": {"Message": "x"}}}}):
            with self.assertRaises(client.SpApiError):
                client.get_fees_estimate("B0X", 7.59)


class TestEnrollPricingRule(unittest.TestCase):
    def test_patch_keeps_our_price_and_validates_by_default(self):
        with mock.patch.object(client.settings, "seller_id", "A1SELLER"), \
                mock.patch.object(client, "_request", return_value={"status": "VALID"}) as request:
            client.enroll_pricing_rule("SKU/1", "RULER", "1293652560402-COMPETITIVE_BUYBOX", 9.99, 8.5, 19.98)
        path, params = request.call_args[0]
        kwargs = request.call_args[1]
        self.assertEqual(path, "/listings/2021-08-01/items/A1SELLER/SKU%2F1")
        self.assertEqual(params["mode"], "VALIDATION_PREVIEW")
        self.assertEqual(kwargs["method"], "PATCH")
        offer = kwargs["body"]["patches"][0]["value"][0]
        self.assertEqual(offer["our_price"][0]["schedule"][0]["value_with_tax"], 9.99)
        self.assertEqual(offer["minimum_seller_allowed_price"][0]["schedule"][0]["value_with_tax"], 8.5)
        self.assertEqual(offer["automated_pricing_merchandising_rule_plan"][0]["merchandising_rule"]["rule_id"],
                         "1293652560402-COMPETITIVE_BUYBOX")

    def test_apply_has_no_preview_mode(self):
        with mock.patch.object(client.settings, "seller_id", "A1SELLER"), \
                mock.patch.object(client, "_request", return_value={}) as request:
            client.enroll_pricing_rule("S", "RULER", "r", 1, 1, 2, validate_only=False)
        self.assertIsNone(request.call_args[0][1]["mode"])


if __name__ == "__main__":
    unittest.main()
