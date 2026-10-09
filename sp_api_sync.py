#!/usr/bin/env python3
"""sp_api_sync.py — 1サイクル実行して終了するスクリプト(daily_scan.py/
send_daily_digest.pyと同じ設計)。cron等で定期実行する想定(例: 3時間おき)。

処理の流れ:
  1. sp_sync_state から前回同期時刻(なければ7日前)を読む
  2. Orders API: LastUpdatedAfter以降の注文を取得 -> sp_orders にupsert
  3. Orders API (getOrderItems): 直近取得した注文ごとに商品明細(ASIN/数量/単価)を
     取得 -> sp_order_items にupsert(商品ごとのP&Lに必要)
  4. Finances API: 直近取得した注文ごとに実手数料を取得 -> sp_financial_events にupsert
  5. FBA Inventory API: 現在の在庫スナップショットを取得 -> sp_fba_inventory を洗い替え
  6. Fulfillment Inbound API(v2024-03-20): 納品プラン一覧・便ごとの詳細・商品明細を
     取得 -> sp_inbound_shipments/sp_inbound_shipment_items にupsert(納品便ごとの
     P&Lに必要)
  7. sp_sync_state を更新

認証情報(LWA_CLIENT_ID/LWA_CLIENT_SECRET/SP_API_REFRESH_TOKEN)が未設定の場合は
何もせずログだけ出して正常終了する(daily_scan.py側の自動巡回やダッシュボードの
起動を妨げないため、ここでは例外を投げない)。

Keepaには一切依存しない(sp_api/, ops_financeの新設テーブル関数のみを使う)。
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone

import ops_finance as of
import market_prices
import stock_ledger
from sp_api import client as sp_client
from sp_api.config import settings as sp_settings

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("sp_api_sync")

DEFAULT_LOOKBACK_DAYS = 7


def sync_orders(since_iso: str) -> list:
    """Orders APIを全ページ取得し、sp_ordersにupsertする。取得したorder_idのリストを返す。"""
    collected = []
    next_token = None
    while True:
        payload = sp_client.get_orders(since_iso, next_token=next_token)
        orders = payload.get("payload", {}).get("Orders", [])
        for o in orders:
            items_total_usd = None
            amount = o.get("OrderTotal", {}).get("Amount")
            if amount is not None:
                items_total_usd = float(amount)
            collected.append({
                "orderId": o["AmazonOrderId"],
                "purchaseDate": o.get("PurchaseDate"),
                "asin": None,  # OrdersAPIのOrders結果自体はASINを含まない(OrderItemsは別API、MVPではSKUのみ扱う)
                "sku": None,
                "quantity": None,
                "itemPriceUsd": items_total_usd,
                "orderStatus": o.get("OrderStatus"),
            })
        next_token = payload.get("payload", {}).get("NextToken")
        if not next_token:
            break
    if collected:
        of.upsert_sp_orders(collected)
    return [o["orderId"] for o in collected]


def sync_order_items(order_ids: list) -> None:
    """注文ごとにgetOrderItemsを呼び、sp_order_itemsにupsertする(商品ごとのP&Lに必要)。
    呼び出し量がgetOrders呼び出しの上に注文数だけ増えるため、SP-APIの操作ごとの
    レート制限(429)を避ける軽いディレイを挟む(フルのトークンバケット制御は
    過剰と判断、429が頻発するようなら見直す)。"""
    for order_id in order_ids:
        payload = sp_client.get_order_items(order_id)
        order_items = payload.get("payload", {}).get("OrderItems", [])
        items = []
        for oi in order_items:
            qty = oi.get("QuantityOrdered")
            item_price = oi.get("ItemPrice", {}).get("Amount")
            # ItemPriceはその明細行の合計額(単価×数量)なので、単価に換算して保存
            # (compute_finance_summary側はitem_price_usd×quantityで売上を出すため)。
            unit_price_usd = None
            if item_price is not None and qty:
                unit_price_usd = float(item_price) / qty
            items.append({
                "asin": oi.get("ASIN"),
                "sku": oi.get("SellerSKU"),
                "quantity": qty,
                "itemPriceUsd": unit_price_usd,
            })
        if items:
            of.upsert_sp_order_items(order_id, items)
        time.sleep(0.5)


def sync_inbound_shipments() -> None:
    """Fulfillment Inbound API(v2024-03-20)。全納品プランを取得し、プランごとに
    埋め込みのshipments一覧・商品明細(items)・便ごとの詳細(destination/納品期間/
    FBA Shipment ID)を取得してsp_inbound_shipments/sp_inbound_shipment_itemsに
    upsertする。v0系(Orders/Finances)とはレスポンス形式が異なりpayloadでラップ
    されない点に注意。"""
    plan_ids = []
    next_token = None
    while True:
        payload = sp_client.get_inbound_plans(next_token=next_token)
        plan_ids.extend(p["inboundPlanId"] for p in payload.get("inboundPlans", []))
        next_token = payload.get("pagination", {}).get("nextToken")
        if not next_token:
            break

    for plan_id in plan_ids:
        plan = sp_client.get_inbound_plan(plan_id)
        items_payload = sp_client.get_inbound_plan_items(plan_id)
        items = [
            {"asin": i.get("asin"), "sku": i.get("msku"), "quantity": i.get("quantity")}
            for i in items_payload.get("items", [])
        ]
        for shipment_summary in plan.get("shipments", []) or []:
            shipment_id = shipment_summary["shipmentId"]
            detail = sp_client.get_inbound_shipment(plan_id, shipment_id)
            window = detail.get("selectedDeliveryWindow", {}) or {}
            destination = detail.get("destination", {}) or {}
            of.upsert_sp_inbound_shipment(
                {
                    "shipmentId": shipment_id,
                    "planId": plan_id,
                    "shipmentConfirmationId": detail.get("shipmentConfirmationId"),
                    "status": detail.get("status"),
                    "destinationFc": destination.get("warehouseId"),
                    "deliveryWindowStart": window.get("startDate"),
                    "deliveryWindowEnd": window.get("endDate"),
                    "createdAt": plan.get("createdAt"),
                },
                items,
            )
        time.sleep(0.5)


def sync_finances(order_ids: list) -> None:
    for order_id in order_ids:
        payload = sp_client.list_financial_events_by_order(order_id)
        events_group = payload.get("payload", {}).get("FinancialEvents", {})
        events = []
        for shipment_event in events_group.get("ShipmentEventList", []) or []:
            for item in shipment_event.get("ShipmentItemList", []) or []:
                for fee in item.get("ItemFeeList", []) or []:
                    amount = fee.get("FeeAmount", {}).get("CurrencyAmount")
                    if amount is not None:
                        events.append({
                            "eventType": fee.get("FeeType", "Unknown"),
                            "amountUsd": float(amount),
                            "postedDate": shipment_event.get("PostedDate"),
                        })
        if events:
            of.upsert_sp_financial_events(order_id, events)


def sync_inventory() -> None:
    collected = []
    next_token = None
    while True:
        payload = sp_client.get_inventory_summaries(next_token=next_token)
        summaries = payload.get("payload", {}).get("inventorySummaries", [])
        for s in summaries:
            details = s.get("inventoryDetails", {})
            collected.append({
                "asin": s.get("asin"),
                "sku": s.get("sellerSku"),
                "fnsku": s.get("fnSku"),
                "fulfillableQuantity": details.get("fulfillableQuantity", 0),
            })
        next_token = payload.get("payload", {}).get("nextToken")
        if not next_token:
            break
    of.replace_sp_fba_inventory(collected)


def run_once(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> None:
    if not sp_settings.configured:
        logger.info("SP-API認証情報が未設定のため、同期をスキップします(.envにLWA_CLIENT_ID等を設定してください)。")
        return

    state = of.get_sp_sync_state()
    since_iso = state["ordersSyncedAt"] or (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(f"Orders同期開始(LastUpdatedAfter={since_iso})")
    order_ids = sync_orders(since_iso)
    logger.info(f"Orders同期完了: {len(order_ids)}件")
    of.set_sp_sync_state(orders_synced_at=now_iso)

    if order_ids:
        logger.info("OrderItems同期開始")
        sync_order_items(order_ids)
        logger.info("OrderItems同期完了")

        logger.info("Finances同期開始")
        sync_finances(order_ids)
        of.set_sp_sync_state(finances_synced_at=now_iso)
        logger.info("Finances同期完了")

    logger.info("FBA在庫スナップショット取得開始")
    sync_inventory()
    of.set_sp_sync_state(inventory_synced_at=now_iso)
    logger.info("FBA在庫スナップショット取得完了")

    logger.info("FBA納品便(Inbound Shipments)同期開始")
    sync_inbound_shipments()
    of.set_sp_sync_state(inbound_synced_at=now_iso)
    logger.info("FBA納品便(Inbound Shipments)同期完了")

    changes = stock_ledger.record_sp_changes()
    logger.info(f"在庫台帳イベント: {changes or 'なし'}")

    # 相場(バイボックス・手数料)は読み取りだけ。失敗しても他の同期結果は残す。
    try:
        logger.info(f"相場取得: {market_prices.refresh()}")
    except Exception as exc:
        logger.warning(f"相場取得に失敗: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SP-API(Orders/Finances/FBA Inventory)を1サイクル同期する")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="初回同期時の遡り日数")
    args = parser.parse_args()
    run_once(lookback_days=args.lookback_days)


if __name__ == "__main__":
    main()
