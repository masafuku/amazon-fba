#!/usr/bin/env python3
"""「Amazonへ納品」のプラン(下書き)をSP-APIで作る。作るのはプランまで。梱包・配送の確定はしない。

対象のSKUは、(1) FBAになっている (2) 出品にエラーがない(--include-errors で、カタログ側のエラーが残るSKUも含める) (3) 数量が1以上(手元＋輸送中)。
数量は在庫台帳(AWSの stock_ledger.py stock --json)の「手元＋国内輸送中」。FNSKUが未割当のSKUも含めて
試し、Amazonが受け付けなければ、その理由を表示する(--only-with-fnsku でFNSKUがあるものだけにできる)。
出荷元は板橋区の住所(郵便番号173-0003)を標準にする。住所の中身は、その郵便番号の既存プランから取る。

使い方(SP-APIの認証情報があるマシンで):
    inbound_draft.py --stock stock.json                  # 対象を表示するだけ
    inbound_draft.py --stock stock.json --create --name "第二便(仮)"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

import pricing_rule as pr
from sp_api import client


def eligible_items(stock: dict, only_with_fnsku: bool = False, include_errors: bool = False,
                   include_planned: bool = False) -> tuple[list, list]:
    """(対象[{msku, asin, quantity, fnsku}], 除外[(msku, 理由)])"""
    seller, mp = client.settings.seller_id, client.settings.marketplace_id
    items, skipped = [], []
    for listing in sorted(pr.list_listings(), key=lambda x: x["sku"]):
        time.sleep(0.4)
        r = client._request(f"/listings/2021-08-01/items/{seller}/{listing['sku']}",
                            {"marketplaceIds": mp, "includedData": "summaries,fulfillmentAvailability,issues", "issueLocale": "ja_JP"})
        summary = r["summaries"][0]
        fba = any(f.get("fulfillmentChannelCode") == "AMAZON_NA" for f in r.get("fulfillmentAvailability", []))
        errors = sum(i["severity"] == "ERROR" for i in r.get("issues", []))
        row = stock.get(listing["asin"], {})
        quantity = (row.get("at_home") or 0) + (row.get("domestic_transit") or 0)
        if include_planned:   # すでに納品プランに入れた分(ラベルは、プラン済みの商品にも必要)
            quantity += row.get("planned") or 0
        reason = (None if fba else "FBAではない") or (f"出品にエラー{errors}件" if errors and not include_errors else None) \
            or (None if quantity > 0 else "数量0") or ("FNSKU未割当" if only_with_fnsku and not summary.get("fnSku") else None)
        if reason:
            skipped.append((listing["sku"], reason))
        else:
            items.append({"msku": listing["sku"], "asin": listing["asin"], "quantity": quantity, "fnsku": summary.get("fnSku")})
    return items, skipped


# 出荷元は板橋区の住所を標準にする(CEO指示 2026-10-10: 「今後も発送元は板橋区の住所」)。
# 住所の中身(電話番号なども)は、その郵便番号の既存プラン(第一便など)から取る。
DEFAULT_SOURCE_POSTAL_CODE = "173-0003"


# 第一便(出荷済み)のプラン。出荷済みのプランは一覧には出ないが、IDを指定すれば読める。
# 下書きをすべて取り消した後でも、板橋区の住所(電話番号などを含む)を取れるようにする予備の参照先。
FIRST_SHIPMENT_PLAN_ID = "wfa1b90a69-cc3e-412d-a641-5ef2bb15fda8"


def source_address(postal_code: str = DEFAULT_SOURCE_POSTAL_CODE) -> dict:
    plans = sorted(client.get_inbound_plans().get("inboundPlans", []), key=lambda p: p.get("createdAt", ""), reverse=True)
    for plan_id in [p["inboundPlanId"] for p in plans] + [FIRST_SHIPMENT_PLAN_ID]:
        try:
            address = client.get_inbound_plan(plan_id).get("sourceAddress", {})
        except client.SpApiError:
            continue
        if address.get("postalCode") == postal_code:
            return address
    raise SystemExit(f"郵便番号{postal_code}を出荷元にした既存プランが見つかりません。--source-postal で指定してください。")


def wait_for_operation(operation_id: str, timeout_seconds: int = 120) -> dict:
    deadline = time.time() + timeout_seconds
    while True:
        status = client.get_inbound_operation(operation_id)
        if status.get("operationStatus") in ("SUCCESS", "FAILED") or time.time() > deadline:
            return status
        time.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Amazonへ納品のプラン(下書き)を作る")
    parser.add_argument("--stock", required=True, help="stock_ledger.py stock --json の出力ファイル")
    parser.add_argument("--create", action="store_true", help="実際にプランを作る(省略時は対象の表示のみ)")
    parser.add_argument("--name", default="第二便(仮)")
    parser.add_argument("--only-with-fnsku", action="store_true")
    parser.add_argument("--include-errors", action="store_true", help="商品情報のエラーが残るSKUも含める(カタログ側のエラーは納品を止めない。Amazonが拒否すれば作成時に分かる)")
    parser.add_argument("--source-postal", default=DEFAULT_SOURCE_POSTAL_CODE, help="出荷元の郵便番号(既定: 板橋区 173-0003)")
    args = parser.parse_args()

    with open(args.stock, encoding="utf-8") as f:
        stock = {r["asin"]: r for r in json.load(f)}
    items, skipped = eligible_items(stock, args.only_with_fnsku, args.include_errors)
    print(f"対象 {len(items)}件 / 合計{sum(i['quantity'] for i in items)}個")
    for i in items:
        print(f"  {i['msku']:<22} ×{i['quantity']:<3} FNSKU={i['fnsku'] or '未割当'}")
    for sku, reason in skipped:
        print(f"  除外 {sku}: {reason}")
    if not args.create or not items:
        return

    source = source_address(args.source_postal)
    print(f"出荷元: {source.get('name')} / {source.get('addressLine1')} {source.get('city')} {source.get('postalCode')}")
    rejected_all = []
    for _ in range(6):
        try:
            created = client.create_inbound_plan(items, args.name, source)
            break
        except client.SpApiError as exc:
            # 「納品にまだ使えないMSKU」だけが理由なら、それを除いて作り直す
            match = re.search(r"not available for inbound.*?MSKUs: \[([^\]]*)\]", str(exc).replace("\\n", " "))
            rejected = [m.strip() for m in match.group(1).split(",")] if match else []
            # 梱包作業が不要なSKUは、梱包担当をNONEにするよう求められる
            no_prep = re.findall(r"(\S+) does not require prepOwner", str(exc))
            if no_prep:
                for item in items:
                    if item["msku"] in no_prep:
                        item["prep_owner"] = "NONE"
                print(f"梱包作業が不要なため梱包担当をNONEにして再試行: {len(no_prep)}件")
                continue
            if not rejected:
                raise
            rejected_all += rejected
            items = [i for i in items if i["msku"] not in rejected]
            print(f"納品にまだ使えないため除外して再試行: {rejected}")
            if not items:
                sys.exit("対象が残りませんでした。")
    else:
        sys.exit("作成できませんでした。")
    if rejected_all:
        print("除外したSKU:", rejected_all)
    print("\nプラン作成の依頼:", created)
    result = wait_for_operation(created["operationId"])
    print("結果:", result.get("operationStatus"))
    for problem in result.get("operationProblems", []):
        print("  ", problem.get("severity"), problem.get("code"), "|", (problem.get("message") or "")[:200], "|", problem.get("details"))
    if result.get("operationStatus") != "SUCCESS":
        sys.exit(1)
    print("プランID:", created["inboundPlanId"])


if __name__ == "__main__":
    main()
