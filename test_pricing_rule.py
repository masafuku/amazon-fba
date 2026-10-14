import unittest
from unittest import mock

import pricing_rule
from sp_api import client


class TestComputeBounds(unittest.TestCase):
    def test_min_price_includes_target_roi(self):
        minimum, _ = pricing_rule.compute_bounds(unit_cost_jpy=300, current_price_usd=9.99)
        cost_usd = 300 / 150
        profit_at_min = minimum * (1 - 0.15) - 3.5 - pricing_rule.INTL_SHIPPING_USD - cost_usd * 0.125 - cost_usd
        self.assertGreaterEqual(profit_at_min / cost_usd, pricing_rule.MIN_ROI)
        self.assertLess(profit_at_min / cost_usd, pricing_rule.MIN_ROI + 0.02)

    @mock.patch.object(pricing_rule, "MIN_ROI", 0.0)
    def test_min_price_is_break_even_when_roi_is_zero(self):
        minimum, maximum = pricing_rule.compute_bounds(unit_cost_jpy=300, current_price_usd=9.99)
        cost_usd = 300 / 150
        profit_at_min = minimum * (1 - 0.15) - 3.5 - pricing_rule.INTL_SHIPPING_USD - cost_usd * 0.125 - cost_usd
        self.assertGreaterEqual(profit_at_min, 0)
        self.assertLess(profit_at_min, 0.02)
        profit_below = (minimum - 0.05) * (1 - 0.15) - 3.5 - pricing_rule.INTL_SHIPPING_USD - cost_usd * 0.125 - cost_usd
        self.assertLess(profit_below, 0)
        self.assertEqual(maximum, 19.98)

    def test_max_never_below_min(self):
        minimum, maximum = pricing_rule.compute_bounds(unit_cost_jpy=3000, current_price_usd=5.0)
        self.assertGreater(maximum, minimum)


class TestActualFees(unittest.TestCase):
    @mock.patch.object(pricing_rule, "MIN_ROI", 0.0)
    def test_uses_actual_fba_fee_and_reaches_break_even(self):
        fee_fn = lambda price: {"referral": price * 0.15, "fba": 2.52, "other": 0.0}
        minimum, _ = pricing_rule.compute_bounds(326, 7.49, fee_fn)
        cost_usd = 326 / 150
        other = pricing_rule.INTL_SHIPPING_USD + cost_usd * 0.125 + cost_usd
        self.assertAlmostEqual(minimum, (2.52 + other) / 0.85, delta=0.011)
        self.assertGreaterEqual(minimum - minimum * 0.15 - 2.52 - other, 0)

    def test_follows_price_dependent_fba_fee(self):
        # 10ドル以下はFBA手数料が安く、超えると高くなる想定
        fee_fn = lambda price: {"referral": price * 0.15, "fba": 2.0 if price <= 10 else 5.0, "other": 0.0}
        minimum, _ = pricing_rule.compute_bounds(300, 20.0, fee_fn)
        self.assertLessEqual(minimum, 10)


class TestMarketFloor(unittest.TestCase):
    def test_midpoint_of_buy_box_and_break_even_rounded_up(self):
        self.assertEqual(pricing_rule.market_floor(roi_floor=6.87, break_even=6.36, buy_box=7.49 + 0.0), 6.93)

    def test_never_below_roi_floor(self):
        # キティシールの例: 真ん中(7.13)がROI下限(7.37)を下回る -> ROI下限
        self.assertEqual(pricing_rule.market_floor(roi_floor=7.37, break_even=6.78, buy_box=7.48), 7.37)

    def test_no_buy_box_falls_back_to_roi_floor(self):
        self.assertEqual(pricing_rule.market_floor(roi_floor=5.51, break_even=5.2, buy_box=None), 5.51)

    def test_get_buy_box_price(self):
        payload = {"payload": {"Summary": {"BuyBoxPrices": [{"ListingPrice": {"Amount": 7.49}}]}}}
        with mock.patch.object(client, "_request", return_value=payload):
            self.assertEqual(client.get_buy_box_price("B0X"), 7.49)
        with mock.patch.object(client, "_request", return_value={"payload": {"Summary": {}}}):
            self.assertIsNone(client.get_buy_box_price("B0X"))


class TestPutListingItem(unittest.TestCase):
    def test_fba_listing_has_no_quantity(self):
        with mock.patch.object(client.settings, "seller_id", "A1SELLER"), \
                mock.patch.object(client, "_request", return_value={"status": "ACCEPTED"}) as request:
            client.put_listing_item("SKU1", "B0X", "STATIONERY", 9.99)
        attrs = request.call_args[1]["body"]["attributes"]
        channel = attrs["fulfillment_availability"][0]
        self.assertEqual(channel["fulfillment_channel_code"], "AMAZON_NA")
        self.assertNotIn("quantity", channel)
        self.assertEqual(request.call_args[1]["method"], "PUT")


class TestConvertToFba(unittest.TestCase):
    def test_fba_channel_has_no_quantity_and_required_attributes_are_sent(self):
        with mock.patch.object(client.settings, "seller_id", "A1SELLER"), \
                mock.patch.object(client, "_request", return_value={"status": "VALID"}) as request:
            client.convert_to_fba("SKU/1", "RULER")
        path, params = request.call_args[0]
        kwargs = request.call_args[1]
        self.assertEqual(path, "/listings/2021-08-01/items/A1SELLER/SKU%2F1")
        self.assertEqual(params["mode"], "VALIDATION_PREVIEW")
        patches = {p["path"]: p["value"] for p in kwargs["body"]["patches"]}
        channel = patches["/attributes/fulfillment_availability"][0]
        self.assertEqual(channel["fulfillment_channel_code"], "AMAZON_NA")
        self.assertNotIn("quantity", channel)
        self.assertIs(patches["/attributes/batteries_required"][0]["value"], False)
        self.assertEqual(patches["/attributes/supplier_declared_dg_hz_regulation"][0]["value"], "not_applicable")

    def test_apply_has_no_preview_and_delete_uses_delete(self):
        with mock.patch.object(client.settings, "seller_id", "A1SELLER"), \
                mock.patch.object(client, "_request", return_value={}) as request:
            client.convert_to_fba("S", "RULER", validate_only=False)
            self.assertIsNone(request.call_args[0][1]["mode"])
            client.delete_listing_item("S")
        self.assertEqual(request.call_args[1]["method"], "DELETE")


class TestCreateInboundPlan(unittest.TestCase):
    def test_body_has_items_with_label_and_per_item_prep_owner(self):
        with mock.patch.object(client.settings, "marketplace_id", "ATVPDKIKX0DER"), \
                mock.patch.object(client, "_request", return_value={"inboundPlanId": "wf1", "operationId": "op1"}) as request:
            result = client.create_inbound_plan(
                [{"msku": "A", "quantity": "10"}, {"msku": "B", "quantity": 3, "prep_owner": "NONE"}],
                "第二便(仮)", {"name": "x", "countryCode": "JP"})
        self.assertEqual(result["operationId"], "op1")
        self.assertEqual(request.call_args[0][0], "/inbound/fba/2024-03-20/inboundPlans")
        body = request.call_args[1]["body"]
        self.assertEqual(body["destinationMarketplaces"], ["ATVPDKIKX0DER"])
        self.assertEqual(body["items"][0], {"msku": "A", "quantity": 10, "labelOwner": "SELLER", "prepOwner": "SELLER"})
        self.assertEqual(body["items"][1]["prepOwner"], "NONE")


class TestFeesEstimate(unittest.TestCase):
    def test_parses_fee_details(self):
        payload = {"payload": {"FeesEstimateResult": {"Status": "Success", "FeesEstimate": {
            "TotalFeesEstimate": {"Amount": 3.66},
            "FeeDetailList": [{"FeeType": "ReferralFee", "FeeAmount": {"Amount": 1.14}},
                              {"FeeType": "FBAFees", "FeeAmount": {"Amount": 2.52}}]}}}}
        with mock.patch.object(client, "_request", return_value=payload):
            self.assertEqual(client.get_fees_estimate("B0X", 7.59), {"referral": 1.14, "fba": 2.52, "total": 3.66, "other": 0.0})

    def test_retries_transient_internal_error(self):
        error = {"payload": {"FeesEstimateResult": {"Status": "ServerError", "Error": {"Code": "InternalError"}}}}
        ok = {"payload": {"FeesEstimateResult": {"Status": "Success", "FeesEstimate": {
            "TotalFeesEstimate": {"Amount": 3.66},
            "FeeDetailList": [{"FeeType": "ReferralFee", "FeeAmount": {"Amount": 1.14}},
                              {"FeeType": "FBAFees", "FeeAmount": {"Amount": 2.52}}]}}}}
        with mock.patch.object(client, "_request", side_effect=[error, ok]) as request, mock.patch.object(client.time, "sleep"):
            self.assertEqual(client.get_fees_estimate("B0X", 7.59)["fba"], 2.52)
        self.assertEqual(request.call_count, 2)

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
