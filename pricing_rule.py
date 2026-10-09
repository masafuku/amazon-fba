#!/usr/bin/env python3
"""出品中の全SKUをAmazonの自動価格設定ルールに紐付ける。

SP-APIの認証情報はローカルの.envにしか無く、仕入れ原価の正本はAWSのDBにあるため、
原価は `--costs` のJSON({asin: 1個あたり原価の円(送料配分込み)})で受け取る。
既定は検証のみ(VALIDATION_PREVIEW)で何も反映しない。反映は --apply。

最低価格 = 損益分岐点(利益0円になる価格。販売手数料・FBA手数料はProduct Fees APIの実額、関税・国際送料の余裕を含む)。MIN_ROIを上げると利益込みの下限になる。
最高価格 = 現在価格の MAX_MULTIPLE 倍。

使い方:
    pricing_rule.py --costs costs.json              # 一覧と検証(反映しない)
    pricing_rule.py --costs costs.json --apply      # 反映
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

from sp_api import client

EXCHANGE_RATE = 150.0
REFERRAL_FEE_RATE = 0.15
FBA_FEE_USD = 3.5
IMPORT_DUTY_RATE = 0.125
INTL_SHIPPING_USD = round(5873 / 90 / EXCHANGE_RATE, 3)  # 第一便の概算(運賃4,373+発送代行1,500)÷90個。請求書で実額に差し替える
MIN_ROI = 0.0
MAX_MULTIPLE = 2.0


def _estimated_fees(price: float) -> dict:
    return {"referral": price * REFERRAL_FEE_RATE, "fba": FBA_FEE_USD, "other": 0.0}


def compute_bounds(unit_cost_jpy: float, current_price_usd: float, fee_fn=None) -> tuple[float, float]:
    """fee_fn(price) -> {referral, fba, other}。省略時は概算(販売手数料15%・FBA $3.5)。
    手数料は価格に連動する(販売手数料率・FBAの価格帯)ので、価格を動かしながら収束させ、
    最後にその価格で利益が0以上であることを確かめる。"""
    fee_fn = fee_fn or _estimated_fees
    cost_usd = unit_cost_jpy / EXCHANGE_RATE
    other_costs = INTL_SHIPPING_USD + cost_usd * IMPORT_DUTY_RATE + cost_usd * (1 + MIN_ROI)

    def profit(price: float) -> float:
        fees = fee_fn(price)
        return price - fees["referral"] - fees["fba"] - fees.get("other", 0.0) - other_costs

    price = current_price_usd
    for _ in range(6):
        fees = fee_fn(price)
        rate = fees["referral"] / price
        nxt = math.ceil((fees["fba"] + fees.get("other", 0.0) + other_costs) / (1 - rate) * 100) / 100
        if abs(nxt - price) < 0.005:
            break
        price = nxt
    minimum = price
    while profit(minimum) < 0:
        minimum = round(minimum + 0.01, 2)
    maximum = round(max(current_price_usd * MAX_MULTIPLE, minimum * 1.5), 2)
    return minimum, maximum


def make_fee_fn(asin: str):
    """実額の手数料を取る関数(API上限が毎秒1回なので間隔を空け、同じ価格はキャッシュ)。"""
    cache: dict = {}

    def fee_fn(price: float) -> dict:
        key = round(price, 2)
        if key not in cache:
            time.sleep(1.1)
            cache[key] = client.get_fees_estimate(asin, key)
        return cache[key]

    return fee_fn


def list_listings() -> list:
    rows, token = [], None
    while True:
        page = client.search_listings_items(next_token=token)
        for item in page.get("items", []):
            summary = (item.get("summaries") or [{}])[0]
            offers = item.get("offers") or []
            price = None
            for offer in offers:
                amount = (offer.get("price") or {}).get("amount")
                if amount is not None:
                    price = float(amount)
            rows.append({
                "sku": item["sku"],
                "asin": summary.get("asin"),
                "productType": summary.get("productType"),
                "status": summary.get("status"),
                "price": price,
            })
        token = (page.get("pagination") or {}).get("nextToken")
        if not token:
            return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="全SKUを自動価格設定ルールに紐付ける")
    parser.add_argument("--costs", required=True, help="{asin: 1個あたり原価(円)}のJSON")
    parser.add_argument("--rule-id", help="省略時は利用可能なルールが1つならそれを使う")
    parser.add_argument("--apply", action="store_true", help="実際に反映する(省略時は検証のみ)")
    args = parser.parse_args()

    with open(args.costs, encoding="utf-8") as f:
        costs = json.load(f)
    rule_id = args.rule_id
    listings = list_listings()
    if not rule_id:
        available = client.get_pricing_rule_ids(next(l["productType"] for l in listings if l["productType"]))
        if len(available) != 1:
            sys.exit(f"ルールを1つに決められません: {available}。--rule-id を指定してください。")
        rule_id = available[0]
    print(f"ルール: {rule_id} / {'反映' if args.apply else '検証のみ'}\n")

    for l in sorted(listings, key=lambda x: x["sku"]):
        label = f"{l['sku']} {l['asin']}"
        cost = costs.get(l["asin"])
        if cost is None or l["price"] is None:
            print(f"SKIP {label}: {'原価なし' if cost is None else '現在価格なし'}")
            continue
        try:
            minimum, maximum = compute_bounds(cost, l["price"], make_fee_fn(l["asin"]))
            fee_note = "実額"
        except client.SpApiError as exc:
            minimum, maximum = compute_bounds(cost, l["price"])
            fee_note = f"概算(手数料API失敗: {str(exc)[:60]})"
        if minimum > l["price"]:
            note = f"（現在価格${l['price']:.2f}が最低価格を下回る → 現在価格も${minimum:.2f}に引き上げ）"
            current = minimum
        else:
            note, current = "", l["price"]
        try:
            result = client.enroll_pricing_rule(
                l["sku"], l["productType"], rule_id, current, minimum, maximum, validate_only=not args.apply)
            issues = [i.get("message") for i in result.get("issues", []) if i.get("severity") == "ERROR"]
            status = result.get("status")
        except client.SpApiError as exc:
            issues, status = [str(exc)[:200]], "ERROR"
        print(f"{status} {label}: 原価¥{cost:.0f} 現在${l['price']:.2f} → 最低${minimum:.2f} 最高${maximum:.2f} [{fee_note}] {note}{' ' + '; '.join(issues) if issues else ''}")


if __name__ == "__main__":
    main()
