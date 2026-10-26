#!/usr/bin/env python3
"""AWSのcronで5分ごとに動かす注文確認: Orders API(SP-API)を見て、新しい注文・状態の変化(出荷・キャンセル)をLINEで通知する。

Amazonの標準メール通知は、FBAの場合「出荷した後に2時間ごとにまとめて」届くだけで、売れた直後には来ない。
そのため注文そのものをポーリングして、売れた直後(最大5分遅れ)に気づけるようにする。保留中(支払い確認待ち)の注文も、
注文の数分後にはAPIに出る。通知は商品名と時間だけの短い文(価格・利益・注文番号は載せない)。

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
import stock_ledger
from sp_api import client as sp_client

JST = timezone(timedelta(hours=9))
STATE_KEY = 'order_watch_seen'
LOOKBACK_DAYS = 2  # LastUpdatedAfter。5分ごとに動かしても、取りこぼしと一時的なエラーを吸収できる余裕
BASELINE_DAYS = 14
MAX_SEEN = 2000
TITLE_LENGTH = 44
# 新規注文以外で通知する状態の表示名(これ以外の状態への変化は、通知せずに記録だけする)
STATUS_LABEL = {
    'Unshipped': '支払い確定',
    'PartiallyShipped': '一部出荷',
    'Shipped': '出荷',
    'Canceled': '⚠ キャンセル',
    'Unfulfillable': '⚠ 出荷不可',
}


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


def _names(items: list[dict]) -> str:
    parts = []
    for item in items:
        qty = int(item.get('QuantityOrdered') or 1)
        title = (item.get('Title') or item.get('ASIN') or '')[:TITLE_LENGTH]
        parts.append(f'{title} ×{qty}' if qty > 1 else title)
    return '\n'.join(parts)


def _jst(raw: str | None) -> str:
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(JST).strftime('%m/%d %H:%M')
    except Exception:
        return raw or ''


def format_new(order: dict, items: list[dict]) -> str:
    return f"【売れた {_jst(order.get('PurchaseDate'))}】\n{_names(items)}"


def format_change(order: dict, items: list[dict], now: datetime) -> str:
    label = STATUS_LABEL[order.get('OrderStatus')]
    return f"【{label} {now.astimezone(JST):%m/%d %H:%M}】\n{_names(items)}"


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
    if send is None and not dry_run:
        send = _line_sender()
    for order in orders:
        order_id, status = order['AmazonOrderId'], order.get('OrderStatus')
        if order_id not in seen:
            kind = 'new'
        elif seen[order_id] != status:
            if status not in STATUS_LABEL:  # 保留中に戻るなど、通知しない変化は記録だけする
                if not dry_run:
                    seen[order_id] = status
                    _save_state(seen)
                continue
            kind = 'changed'
        else:
            continue
        items = fetch_items(order_id)
        message = format_new(order, items) if kind == 'new' else format_change(order, items, now)
        result['new' if kind == 'new' else 'changed'].append(order_id)
        result.setdefault('messages', []).append(message)
        if dry_run:
            continue
        send(message)
        result['sent'] += 1
        seen[order_id] = status
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
