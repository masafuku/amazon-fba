#!/usr/bin/env python3
"""出荷済みの注文に、Amazon公式の「レビューをリクエスト」(商品レビューとセラー評価のお願い)を送る。

セラー評価が0件だと、同じ価格に並んだときにカート(Buy Box)が実績のある出品者に回り、ほとんど売れない
(2026-11-10、キティ定規で確認)。自動では送らない(CEO 2026-11-11)。送るときは手動でこのスクリプトを実行する。

- 送れるかどうかはAmazonが決める(配達の5〜30日後のみ。Solicitations APIのactionsに出る注文だけ送る)。
- 文面はAmazonの定型で、こちらでは変えられない。1注文に1回だけ送れる。
- 返金のあった注文には送らない(低評価を招くため)。返金はFinances APIのRefundEventListで確かめる。
- 送った・見送った注文は ledger_state(キー review_request_done: {注文番号: 'sent'|'refunded'})に記録し、二度と見ない。

使い方(AWSで手動実行。同期には組み込まない):
    review_request.py --dry-run   # 送れる注文・送れない注文・これから送れる注文の一覧(送らない・記録しない)
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


def run(dry_run: bool = False, now: datetime | None = None, only: set | None = None) -> dict:
    """only を渡すと、その注文番号だけを対象にする(ほかは送らず、記録もしない)。"""
    now = now or datetime.now(timezone.utc)
    done = _state()
    sent, refunded = [], []
    for order in shipped_orders(now):
        order_id = order['order_id']
        if only is not None and order_id not in only:
            continue
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


# Amazonが「送れない」と言う注文のうち、注文からこの日数を過ぎたものは理由不明として報告する
# (配達5日後から送れるので、普通は注文の1〜2週間後には送れるようになる)
UNEXPLAINED_AFTER_DAYS = 12


def _names() -> dict:
    import daily_report
    names = {r['asin']: daily_report.display_name(r['name']) for r in stock_ledger.stock_pipeline()}
    return {a: n for a, n in names.items() if n != '(名前なし)'}


def _order_products(conn, order_id: str, names: dict) -> str:
    rows = conn.execute('SELECT asin, sku, quantity FROM sp_order_items WHERE order_id = ?', (order_id,)).fetchall()
    return '、'.join(f"{names.get(a) or s}{'×' + str(q) if (q or 1) > 1 else ''}" for a, s, q in rows) or '(明細なし)'


def preview(now: datetime | None = None) -> dict:
    """送る前の確認用(何も送らず、記録もしない)。
    eligible=今送れる(返金なし) / refunded=送れるが返金ありで送らない /
    unavailable=Amazonが送れないと言う注文(注文からUNEXPLAINED_AFTER_DAYS日以上は理由不明として印を付ける) /
    upcoming=まだ注文から5日たっていない注文(未出荷を含む)。これから送れるようになる見込み。"""
    now = now or datetime.now(timezone.utc)
    done = _state()
    names = _names()
    result = {'sent_so_far': sum(1 for v in done.values() if v == 'sent'),
              'eligible': [], 'refunded': [], 'unavailable': [], 'upcoming': []}
    with sqlite3.connect(of.DB_PATH) as conn:
        for order in shipped_orders(now):
            order_id = order['order_id']
            if order_id in done:
                continue
            age = (now - datetime.fromisoformat(order['purchase_date'].replace('Z', '+00:00'))).days
            entry = {'order_id': order_id, 'date': order['purchase_date'][:10], 'age_days': age,
                     'products': _order_products(conn, order_id, names)}
            if not can_request(order_id):
                entry['unexplained'] = age >= UNEXPLAINED_AFTER_DAYS
                result['unavailable'].append(entry)
            elif has_refund(order_id):
                result['refunded'].append(entry)
            else:
                result['eligible'].append(entry)
        since = (now - timedelta(days=MIN_AGE_DAYS)).isoformat()
        for order_id, purchase_date, status in conn.execute(
                "SELECT order_id, purchase_date, order_status FROM sp_orders WHERE purchase_date > ? "
                "AND IFNULL(order_status, '') != 'Canceled' ORDER BY purchase_date", (since,)):
            result['upcoming'].append({'order_id': order_id, 'date': purchase_date[:10], 'status': status,
                                       'products': _order_products(conn, order_id, names)})
    return result


def format_preview(res: dict) -> str:
    out = [f"これまでに送った数: {res['sent_so_far']}件",
           f"== 今送れる: {len(res['eligible'])}件"]
    out += [f"  {e['date']} {e['products']} ({e['order_id']})" for e in res['eligible']]
    if res['refunded']:
        out.append(f"== 返金ありのため送らない: {len(res['refunded'])}件(送信時に記録される)")
        out += [f"  {e['date']} {e['products']} ({e['order_id']})" for e in res['refunded']]
    out.append(f"== Amazonが送れないと言う注文: {len(res['unavailable'])}件(配達5日前か、理由不明)")
    out += [f"  {e['date']} {e['products']} 注文から{e['age_days']}日{' ←理由不明' if e['unexplained'] else ''} ({e['order_id']})"
            for e in res['unavailable']]
    out.append(f"== これから(注文から5日未満): {len(res['upcoming'])}件")
    out += [f"  {e['date']} {e['products']} [{e['status']}]" for e in res['upcoming']]
    return '\n'.join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description='出荷済みの注文にレビューとセラー評価のお願いを送る')
    parser.add_argument('--dry-run', action='store_true', help='送れる注文の一覧だけ(送らない・記録しない)')
    parser.add_argument('--json', action='store_true', help='--dry-run の結果をJSONで出す')
    parser.add_argument('--only', nargs='+', metavar='注文番号', help='指定した注文番号だけに送る(CEOが一部を見送るとき)')
    args = parser.parse_args()
    if args.dry_run:
        res = preview()
        print(json.dumps(res, ensure_ascii=False) if args.json else format_preview(res))
    else:
        print(json.dumps(run(only=set(args.only) if args.only else None), ensure_ascii=False))


if __name__ == '__main__':
    main()
