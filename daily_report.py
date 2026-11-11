#!/usr/bin/env python3
"""LINEのまとめ(朝・夜)の本文。要点だけを短く(CEO 2026-11-11「長くて見る気がしない」)。

- 売上: 前回のまとめ以降の注文を、商品ごとに「単価×個数＝小計 / 利益」。Amazonが金額を返さない支払い確定前の
  注文は、注文した日のうちの出品価格(price_snapshots)で、手数料は確定していなければ相場の手数料率で計算する
  (ops_finance._estimated_fees_by_order と同じ考え方)。見込みである旨は書かない(CEO)。
- ★やること: 対応が必要なものをすべて、急ぎの順に1行ずつ。前回のまとめに無かったものに「新」。
- 在庫の変化: 変化があった商品だけを「起きたこと → 今の状態」で。発注目安を下回った商品は別に。
- 新しい動き: 発注・出荷・入庫・ブランド等(便・企業ごと)。
- 価格: price_strategy の提案(利益の増える順に2件まで)、振り返りの判定、相場での利益率の低い商品。

使い方: daily_report.py [--since ISO日時] [--evening]   # 送らずに本文を表示する
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import market_prices
import ops_finance as of
import price_review as prv
import price_strategy
import stock_ledger as sl

JST = timezone(timedelta(hours=9))
ALERTS_STATE_KEY = 'report_alerts_sent'
MAX_PRICE_ITEMS = 2

# alerts() の文にはCLIの使い方(台帳の更新コマンド)が入っているが、LINEでは要らないので外す
_HINTS = [
    re.compile(r'（[^（）]*(paid --month|map-sd|登録)[^（）]*）'),
    re.compile(r'。[^。]*(map-sd|paid --month|納品プランに入れない|再申請か販路変更)[^。]*$'),
]


_MODEL_NO = re.compile(r'^(?=.*[-\d])[A-Z0-9-]{5,}$')   # DSB-PTP-CB のような型番


def display_name(name: str | None) -> str:
    name = re.sub(r'【[^】]*】', '', name or '').strip()
    name = ' '.join(w for w in price_strategy._short_name(name).split(' ') if not _MODEL_NO.match(w))
    return name or '(名前なし)'


def short_alert(text: str) -> str:
    for pattern in _HINTS:
        text = pattern.sub('', text)
    return text.strip()


def sales(conn: sqlite3.Connection, since_iso: str) -> list:
    """前回以降の注文を商品ごとに。[{asin, qty, price(単価), revenue, profit}](小計の大きい順)"""
    costs = prv._costs(conn)
    actual_fees = {o: f for o, f in conn.execute('SELECT order_id, SUM(amount_usd) FROM sp_financial_events GROUP BY order_id')}
    market = {r[0]: r[1:] for r in conn.execute('SELECT asin, buy_box_usd, referral_fee_usd, fba_fee_usd, other_fee_usd FROM market_prices')}
    by_asin: dict = {}
    for order_id, asin, qty, price, purchase_date in conn.execute(
        '''SELECT oi.order_id, oi.asin, oi.quantity, oi.item_price_usd, o.purchase_date FROM sp_order_items oi
           JOIN sp_orders o ON o.order_id = oi.order_id
           WHERE o.purchase_date > ? AND UPPER(IFNULL(o.order_status, '')) NOT IN ('CANCELED', 'CANCELLED')''', (since_iso,)
    ).fetchall():
        qty = qty or 1
        if price is None:   # 支払い確定前: 注文した日のうちの出品価格
            row = conn.execute(
                '''SELECT our_price FROM price_snapshots WHERE asin = ? AND snapshot_on <= ? AND our_price IS NOT NULL
                   ORDER BY snapshot_on DESC LIMIT 1''', (asin, (purchase_date or '')[:10])).fetchone()
            price = row[0] if row else (market.get(asin) or [None])[0]
        if price is None:
            continue
        if order_id in actual_fees:
            fees = actual_fees[order_id] / qty
        else:
            bb, referral, fba, other = market.get(asin, (None, None, None, None))
            fees = referral / bb * price + fba + (other or 0.0) if bb and referral is not None and fba is not None else None
        unit = prv.unit_profit(price, {'total': fees}, costs.get(asin)) if fees is not None else None
        item = by_asin.setdefault(asin, {'asin': asin, 'qty': 0, 'revenue': 0.0, 'profit': 0.0, 'prices': set(), 'profit_known': True})
        item['qty'] += qty
        item['revenue'] += price * qty
        item['prices'].add(round(price, 2))
        if unit is None:
            item['profit_known'] = False
        else:
            item['profit'] += unit * qty
    return sorted(by_asin.values(), key=lambda r: -r['revenue'])


def _money(v: float) -> str:
    return f"${v:,.2f}"


def sales_lines(items: list, names: dict) -> list:
    if not items:
        return ['売上: なし']
    total = sum(i['revenue'] for i in items)
    profit = sum(i['profit'] for i in items if i['profit_known'])
    lines = [f"売上: {sum(i['qty'] for i in items)}個 {_money(total)} / 利益 {_money(profit)}"]
    for i in items[:5]:
        price = f"{_money(min(i['prices']))}" if len(i['prices']) == 1 else f"{_money(min(i['prices']))}〜{_money(max(i['prices']))}"
        profit_text = f" / 利益 {_money(i['profit'])}" if i['profit_known'] else ' / 利益 -(原価なし)'
        lines.append(f"・{display_name(names.get(i['asin']))}  {price}×{i['qty']} = {_money(i['revenue'])}{profit_text}")
    if len(items) > 5:
        rest = items[5:]
        lines.append(f"・ほか{len(rest)}品 {_money(sum(i['revenue'] for i in rest))}")
    return lines


def _remaining(row: dict) -> int:
    return row['fba_available'] + row['fc_received_inbound']


def _in_transit(row: dict) -> int:
    return row['at_home'] + row['planned'] + row['at_tnk'] + row['intl_transit']


def stock_change_lines(conn: sqlite3.Connection, since_iso: str, pipeline: list, sold: list) -> list:
    """変化があった商品だけ「起きたこと → 今の状態」。"""
    rows = {r['asin']: r for r in pipeline}

    def state(row: dict) -> str:
        left = _remaining(row)
        if left:
            cover = f"（約{row['days_of_cover']:.0f}日分）" if row['days_of_cover'] else ''
            return f'残り{left}個{cover}'
        transit = _in_transit(row)
        return f'在庫は輸送中（{transit}個）' if transit else '在庫0（輸送中なし）'

    lines, seen = [], set()
    for item in sold:
        row = rows.get(item['asin'])
        if not row:
            continue
        what = f"{item['qty']}個売れた" if _remaining(row) else f"予約注文{item['qty']}個"
        lines.append(f"・{display_name(row['name'])}: {what} → {state(row)}")
        seen.add(item['asin'])
    for event_type, asin, detail in conn.execute(
        'SELECT event_type, ref, detail FROM ops_events WHERE recorded_at > ? AND event_type IN (?, ?) ORDER BY id',
        (since_iso, sl.FBA_IN_STOCK, sl.FBA_STOCK_OUT),
    ).fetchall():
        row = rows.get(asin)
        if not row or asin in seen:
            continue
        seen.add(asin)
        what = '販売開始' if event_type == sl.FBA_IN_STOCK else '売り切れ'
        lines.append(f"・{display_name(row['name'])}: {what} → {state(row)}")
    return lines


def todo_lines(conn: sqlite3.Connection, pipeline: list, price_count: int) -> tuple[list, list]:
    """(行, 今回の要対応の一覧)。前回のまとめに無かったものに「新」。"""
    found = [short_alert(a) for a in sl.alerts()]
    previous = set(json.loads(sl._get_state(conn, ALERTS_STATE_KEY) or '[]'))
    reorder = [f"{display_name(r['name'])}: 残り{_remaining(r)}個（約{r['days_of_cover']:.0f}日分）→ 発注の検討"
               for r in pipeline if r['days_of_cover'] is not None and r['days_of_cover'] < sl.REORDER_COVER_DAYS]
    items = found + reorder
    ordered = [a for a in items if a not in previous] + [a for a in items if a in previous]
    lines = [f"{n}. {'新 ' if a not in previous else ''}{a}" for n, a in enumerate(ordered, 1)]
    if price_count:
        lines.append(f"{len(lines) + 1}. 価格の提案 {price_count}件（下）")
    return lines, items


def price_lines(conn: sqlite3.Connection, names: dict) -> tuple[list, int]:
    day, rows = price_strategy.latest_proposals(conn)
    proposals = sorted((r for r in rows if r['action'] in ('raise', 'cut') and r['profit_target'] is not None and r['profit_now'] is not None),
                       key=lambda r: -(r['profit_target'] - r['profit_now']))
    lines = []
    label = {'raise': '値上げ', 'cut': '値下げ'}
    for r in proposals[:MAX_PRICE_ITEMS]:
        gain = r['profit_target'] - r['profit_now']
        lines.append(f"・{label[r['action']]} {display_name(r['name'])} 最低{_money(r['current_min'])}→{_money(r['target'])}"
                     f"（1個の利益{'+' if gain >= 0 else ''}{_money(gain)}）")
    if len(proposals) > MAX_PRICE_ITEMS:
        lines.append(f"・ほか{len(proposals) - MAX_PRICE_ITEMS}件（ダッシュボード）")
    feedback = [display_name(r['name']) for r in rows if r['action'] == 'feedback']
    if feedback:
        lines.append(f"・売れないのは評価の問題: {'、'.join(feedback)}")
    low = [r for r in market_prices.roi_rows() if r['roi'] is not None and r['roi'] < market_prices.ROI_ALERT_THRESHOLD]
    if low:
        first = low[0]
        profit = f"赤字" if first['profit'] < 0 else f"利益{_money(first['profit'])}"
        more = f"ほか{len(low) - 1}件" if len(low) > 1 else ''
        lines.append(f"・利益率{market_prices.ROI_ALERT_THRESHOLD:.0%}未満: {display_name(names.get(first['asin']) or first['name'])}"
                     f"（相場{_money(first['buy_box'])}で{profit}）{more}")
    today = datetime.now(timezone.utc).date()
    conn.row_factory = sqlite3.Row
    changes = [dict(r) for r in conn.execute('SELECT * FROM price_changes WHERE changed_on >= ?', ((today - timedelta(days=60)).isoformat(),))]
    conn.row_factory = None
    for c in changes:
        verdict = prv.review_change(conn, c, today)
        if not verdict['verdict'].startswith('途中経過'):
            lines.append(f"・振り返り {display_name(names.get(c['asin']) or c['sku'])}"
                         f"（{c['changed_on'][5:]} {_money(c['old_min'])}→{_money(c['new_min'])}）: {verdict['verdict']}")
    return lines, len(proposals)


def build_report(since_iso: str | None, evening: bool = False, now: datetime | None = None) -> tuple[str | None, list]:
    """(本文, 今回の要対応)。夜は、売上・動き・在庫の変化・新しい要対応が何も無ければ None(送らない)。"""
    of.init_ops_tables()
    now = now or datetime.now(timezone.utc)
    since_iso = since_iso or (now - timedelta(days=1)).isoformat()
    pipeline = sl.stock_pipeline()
    names = {r['asin']: r['name'] for r in pipeline}
    moved = sl.movements(since_iso, names, include_stock_changes=False)
    with sqlite3.connect(of.DB_PATH) as conn:
        sold = sales(conn, since_iso)
        changes = stock_change_lines(conn, since_iso, pipeline, sold)
        prices, price_count = price_lines(conn, names) if not evening else ([], 0)
        todo, alerts_now = todo_lines(conn, pipeline, price_count)
        previous = set(json.loads(sl._get_state(conn, ALERTS_STATE_KEY) or '[]'))
    if evening and not (sold or moved or changes or set(alerts_now) - previous):
        return None, alerts_now

    lines = [f"【{'夜' if evening else '朝'}のまとめ {now.astimezone(JST):%-m/%-d}】"]
    lines += sales_lines(sold, names)
    lines += ['', '★やること'] + (todo or ['なし'])
    lines += ['', '在庫の変化'] + (changes or ['・なし'])
    lines += ['', '新しい動き'] + ([f'・{m}' for m in moved] or ['・なし'])
    if prices:
        lines += ['', '価格（ドライラン・価格は変えていません）'] + prices
    return '\n'.join(lines), alerts_now


def mark_sent(alerts_now: list) -> None:
    with sqlite3.connect(of.DB_PATH) as conn:
        sl._set_state(conn, ALERTS_STATE_KEY, json.dumps(alerts_now, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="LINEのまとめの本文を表示する(送らない)")
    parser.add_argument("--since", help="ISO日時(省略時は前回の送信時刻)")
    parser.add_argument("--evening", action="store_true")
    args = parser.parse_args()
    text, _ = build_report(args.since or of.get_last_digest_sent_at(), evening=args.evening)
    print(text or "(夜: 新しいことが無いので送りません)")


if __name__ == "__main__":
    main()
