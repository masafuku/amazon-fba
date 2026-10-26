#!/usr/bin/env python3
"""AWSのcronで5分ごとに動かす注文確認: Orders API(SP-API)を見て、新しい注文・状態の変化(出荷・キャンセルなど)をLINEで通知する。

Amazonの標準メール通知は、FBAの場合「出荷した後に2時間ごとにまとめて」届くだけで、売れた直後には来ない。
そのため注文そのものをポーリングして、売れた直後(最大5分遅れ)に気づけるようにする。保留中(支払い確認待ち)の注文も、
注文の数分後にはAPIに出る。

使い方(AWSで実行):
    order_watch.py --dry-run     # 検索と通知文の表示だけ(LINEは送らない。状態も更新しない)
    order_watch.py               # 新規・状態変化があればLINEに通知
    order_watch.py --baseline    # いまある注文を通知せずに「確認済み」として記録する(初回に自動でも行う)
前提: .env に SP-API の認証情報とLINEの設定(LINE_CHANNEL_ACCESS_TOKEN)。
状態は ledger_state(キー order_watch_seen: {注文番号: 状態})に持つ。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import ops_finance as of
import pricing_rule
import stock_ledger
from sp_api import client as sp_client

JST = timezone(timedelta(hours=9))
STATE_KEY = 'order_watch_seen'
LOOKBACK_DAYS = 2  # LastUpdatedAfter。5分ごとに動かしても、取りこぼしと一時的なエラーを吸収できる余裕
BASELINE_DAYS = 14
MAX_SEEN = 2000
STATUS_JA = {
    'Pending': '保留中(支払い確認待ち)',
    'Unshipped': '未出荷',
    'PartiallyShipped': '一部出荷済み',
    'Shipped': '出荷済み',
    'Canceled': 'キャンセル',
    'Unfulfillable': '出荷不可',
}


def status_ja(status: str | None) -> str:
    return STATUS_JA.get(status or '', status or '不明')


def _state() -> dict:
    with sqlite3.connect(of.DB_PATH) as conn:
        raw = stock_ledger._get_state(conn, STATE_KEY)
    return json.loads(raw) if raw else {}


def _save_state(seen: dict) -> None:
    items = list(seen.items())[-MAX_SEEN:]
    with sqlite3.connect(of.DB_PATH) as conn:
        stock_ledger._set_state(conn, STATE_KEY, json.dumps(dict(items)))


def fetch_orders(since_iso: str) -> list[dict]:
    orders, token = [], None
    while True:
        payload = sp_client.get_orders(since_iso, next_token=token).get('payload', {})
        orders += payload.get('Orders', [])
        token = payload.get('NextToken')
        if not token:
            return orders


def fetch_items(order_id: str) -> list[dict]:
    return sp_client.get_order_items(order_id).get('payload', {}).get('OrderItems', [])


def fetch_stock() -> dict:
    """ASIN -> FBAの販売可能数。取れなければ空(通知の補足情報なので、失敗しても通知は止めない)。"""
    stock, token = {}, None
    try:
        while True:
            payload = sp_client.get_inventory_summaries(token).get('payload', {})
            for s in payload.get('inventorySummaries', []):
                stock[s['asin']] = s.get('inventoryDetails', {}).get('fulfillableQuantity', 0)
            token = payload.get('nextToken') or payload.get('NextToken')
            if not token:
                return stock
    except Exception:
        return stock


def unit_cost_jpy(asin: str) -> float | None:
    """ASINの仕入れ単価(税抜・円)。購入記録の合計金額÷合計数量。"""
    with sqlite3.connect(of.DB_PATH) as conn:
        row = conn.execute(
            'SELECT SUM(IFNULL(amount_jpy, 0)), SUM(quantity) FROM jp_purchase_records WHERE asin = ? AND quantity > 0',
            (asin,)).fetchone()
    return row[0] / row[1] if row and row[1] and row[0] else None


def estimate_profit(asin: str, price_usd: float, quantity: int = 1) -> float | None:
    """Product Fees APIの手数料+原価+関税+国際物流費(概算)から出す1注文あたりの概算利益(USD)。出せなければNone。"""
    cost = unit_cost_jpy(asin)
    if cost is None:
        return None
    try:
        fees = sp_client.get_fees_estimate(asin, price_usd)['total']
    except Exception:
        return None
    per_unit = cost / pricing_rule.EXCHANGE_RATE * (1 + pricing_rule.IMPORT_DUTY_RATE) + pricing_rule.INTL_SHIPPING_USD
    return (price_usd - fees - per_unit) * quantity if quantity else None


def _item_price(item: dict) -> float | None:
    amount = (item.get('ItemPrice') or {}).get('Amount')
    return float(amount) if amount else None


def _item_lines(items: list[dict], stock: dict, listing_price) -> list[str]:
    lines = []
    for item in items:
        qty = int(item.get('QuantityOrdered') or 1)
        total = _item_price(item)
        asin = item.get('ASIN')
        unit = total / qty if total else None
        assumed = False
        if unit is None and listing_price and asin:
            unit, assumed = listing_price(asin), True
        title = (item.get('Title') or asin or '')[:44]
        price_text = '価格未確定' if unit is None else f"${unit:.2f}{'(出品価格から推定)' if assumed else ''}"
        profit = estimate_profit(asin, unit, qty) if unit and asin else None
        profit_text = f' / 概算利益 約${profit:.2f}' if profit is not None else ''
        stock_text = f' / FC在庫 {stock[asin]}点' if asin in stock else ''
        lines.append(f'・{title} ×{qty} {price_text}{profit_text}{stock_text}')
    return lines


def format_new(order: dict, items: list[dict], stock: dict, listing_price=None) -> str:
    when = _purchase_time(order)
    lines = [f"【新規注文 {when}】{status_ja(order.get('OrderStatus'))}"]
    lines += _item_lines(items, stock, listing_price)
    lines.append(f"注文番号 {order['AmazonOrderId']}")
    return '\n'.join(lines)


def format_change(order: dict, items: list[dict], old_status: str) -> str:
    new_status = order.get('OrderStatus')
    mark = '⚠ ' if new_status == 'Canceled' else ''
    lines = [f"【注文の状態が変わりました】{mark}{status_ja(old_status)} → {status_ja(new_status)}"]
    for item in items:
        lines.append(f"・{(item.get('Title') or item.get('ASIN') or '')[:44]} ×{item.get('QuantityOrdered') or 1}")
    lines.append(f"注文番号 {order['AmazonOrderId']}")
    return '\n'.join(lines)


def _purchase_time(order: dict) -> str:
    raw = order.get('PurchaseDate')
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(JST).strftime('%m/%d %H:%M')
    except Exception:
        return raw or ''


def _listing_price(asin: str) -> float | None:
    try:
        return sp_client.get_offers_summary(asin)['buy_box']
    except Exception:
        return None


def _line_sender():
    from config import Settings
    from notify_line import send_line_message
    settings = Settings.load()
    if not settings.line_channel_access_token:
        raise RuntimeError('LINE_CHANNEL_ACCESS_TOKEN が未設定のため通知できません')
    return lambda m: send_line_message(channel_access_token=settings.line_channel_access_token, message=m,
                                       user_id=settings.line_user_id or None)


def run(dry_run: bool = False, baseline: bool = False, send=None, now: datetime | None = None) -> dict:
    """新規・状態変化を検出して通知する。通知に成功した注文だけ「確認済み」に記録する(失敗した分は次回また通知する)。"""
    of.init_ops_tables()
    now = now or datetime.now(timezone.utc)
    seen = _state()
    first_run = not seen and not baseline
    days = BASELINE_DAYS if (baseline or first_run) else LOOKBACK_DAYS
    orders = fetch_orders((now - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ'))
    result = {'fetched': len(orders), 'new': [], 'changed': [], 'sent': 0, 'baseline': baseline or first_run}
    if baseline or first_run:
        if not dry_run:
            _save_state({**seen, **{o['AmazonOrderId']: o.get('OrderStatus') for o in orders}})
        return result
    todo = []
    for order in orders:
        order_id, status = order['AmazonOrderId'], order.get('OrderStatus')
        if order_id not in seen:
            todo.append(('new', order))
        elif seen[order_id] != status:
            todo.append(('changed', order))
    if not todo:
        return result
    stock = fetch_stock() if any(kind == 'new' for kind, _ in todo) else {}
    if send is None and not dry_run:
        send = _line_sender()
    for kind, order in todo:
        order_id = order['AmazonOrderId']
        items = fetch_items(order_id)
        if kind == 'new':
            message = format_new(order, items, stock, _listing_price)
            result['new'].append(order_id)
        else:
            message = format_change(order, items, seen[order_id])
            result['changed'].append(order_id)
        result.setdefault('messages', []).append(message)
        if dry_run:
            continue
        send(message)
        result['sent'] += 1
        seen[order_id] = order.get('OrderStatus')
        _save_state(seen)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args(argv)
    result = run(dry_run=args.dry_run, baseline=args.baseline)
    print(f"[INFO] 取得 {result['fetched']}件 / 新規 {len(result['new'])}件 / 状態変化 {len(result['changed'])}件 / "
          f"LINE送信 {result['sent']}件{' (基準登録)' if result['baseline'] else ''}")
    for message in result.get('messages', []):
        print(message)
        print('---')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
