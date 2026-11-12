#!/usr/bin/env python3
"""出荷済みの注文に、Amazon公式の「レビューをリクエスト」(商品レビューとセラー評価のお願い)を送る。

セラー評価が0件だと、同じ価格に並んだときにカート(Buy Box)が実績のある出品者に回り、ほとんど売れない
(2026-11-10、キティ定規で確認)。自動では送らない(CEO 2026-11-11)。送るときは手動でこのスクリプトを実行する。

- 送れるかどうかはAmazonが決める(配達の5〜30日後のみ。Solicitations APIのactionsに出る注文だけ送る)。
- 文面はAmazonの定型で、こちらでは変えられない。1注文に1回だけ送れる。
- 返金のあった注文には送らない(低評価を招くため)。返金はFinances APIのRefundEventListで確かめる。
- 送った・見送った注文は ledger_state(キー review_request_done: {注文番号: 'sent'|'refunded'})に記録し、二度と見ない。

使い方(AWSで手動実行。同期には組み込まない):
    review_request.py --dry-run   # 送れる注文の一覧だけ(送らない・記録しない)
    review_request.py             # 送る
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import ops_finance as of
import stock_ledger
from sp_api import client as sp_client
from sp_api.config import settings

STATE_KEY = 'review_request_done'
# 配達の30日後まで送れる。出荷・配達のずれを見込んで、注文日から40日まで遡る
LOOKBACK_DAYS = 40
# 配達の5日後から送れる。それより新しい注文はAPIを呼ぶだけ無駄なので見ない
MIN_AGE_DAYS = 5
# Solicitations APIは毎秒1回まで(続けて呼ぶと429)
API_INTERVAL_SEC = 1.1


def _state() -> dict:
    with sqlite3.connect(of.DB_PATH) as conn:
        raw = stock_ledger._get_state(conn, STATE_KEY)
    return json.loads(raw) if raw else {}


def _save_state(done: dict) -> None:
    with sqlite3.connect(of.DB_PATH) as conn:
        stock_ledger._set_state(conn, STATE_KEY, json.dumps(done))


def shipped_orders(now: datetime) -> list[dict]:
    """注文日がLOOKBACK_DAYS〜MIN_AGE_DAYS前の出荷済み注文(AWSのDB。3時間ごとの同期で入る)。"""
    since = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    until = (now - timedelta(days=MIN_AGE_DAYS)).isoformat()
    with sqlite3.connect(of.DB_PATH) as conn:
        return [
            {'order_id': order_id, 'purchase_date': purchase_date}
            for order_id, purchase_date in conn.execute(
                "SELECT order_id, purchase_date FROM sp_orders WHERE order_status = 'Shipped' "
                'AND purchase_date >= ? AND purchase_date <= ? ORDER BY purchase_date',
                (since, until),
            )
        ]


def can_request(order_id: str) -> bool:
    time.sleep(API_INTERVAL_SEC)
    data =sp_client._request(f'/solicitations/v1/orders/{order_id}', {'marketplaceIds': settings.marketplace_id})
    return any(link.get('name') == 'productReviewAndSellerFeedback'
               for link in (data.get('_links') or {}).get('actions') or [])


def has_refund(order_id: str) -> bool:
    time.sleep(API_INTERVAL_SEC)
    events =sp_client._request(f'/finances/v0/orders/{order_id}/financialEvents').get('payload', {})
    return bool((events.get('FinancialEvents') or {}).get('RefundEventList'))


def send_request(order_id: str) -> None:
    time.sleep(API_INTERVAL_SEC)
    sp_client._request(f'/solicitations/v1/orders/{order_id}/solicitations/productReviewAndSellerFeedback',
                       {'marketplaceIds': settings.marketplace_id}, 'POST')


def run(dry_run: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    done = _state()
    sent, refunded = [], []
    for order in shipped_orders(now):
        order_id = order['order_id']
        if order_id in done or not can_request(order_id):
            continue
        if has_refund(order_id):
            refunded.append(order_id)
            if not dry_run:
                done[order_id] = 'refunded'
                _save_state(done)
            continue
        sent.append(order_id)
        if not dry_run:
            send_request(order_id)
            done[order_id] = 'sent'
            _save_state(done)
    return {'sent': sent, 'skipped_refunded': refunded}


def main() -> None:
    parser = argparse.ArgumentParser(description='出荷済みの注文にレビューとセラー評価のお願いを送る')
    parser.add_argument('--dry-run', action='store_true', help='送れる注文の一覧だけ(送らない・記録しない)')
    args = parser.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), ensure_ascii=False))


if __name__ == '__main__':
    main()
