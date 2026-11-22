#!/usr/bin/env python3
"""価格の振り返り(ドライラン)。価格は一切変更しない(読み取りのみ)。

1日1回(AWSのcron):
1. 出品中の全SKUの価格・最低/最高価格をAmazon(SP-API)から取り、相場(market_prices)・販売可能在庫と一緒に
   price_snapshots に1日1行で残す。
2. 前回のスナップショットと比べて最低価格が変わったSKUを price_changes に記録する。価格の判断は最低価格で
   行っている(全SKUが自動価格設定ルールに乗っており、価格そのものはルールが毎日動かす)ので、ルールによる
   価格の上下は記録しない。変更時の手数料の実額で、変更前後の1個の利益(見込み)も残す。
3. 変更から REVIEW_DAYS 日の売れ行きを、変更前の同じ日数と比べて判定する。売れた数は在庫があった日数で割る
   (在庫切れの日を含めると売れ行きが低く出る)。判定は「効果あり/効果なし/変化なし/判断保留」。
   競合の出品数やBuy Boxが大きく動いた期間は、価格の効果とは限らない旨を添える。

使い方:
    price_review.py                 # スナップショット→変更の記録→振り返りの表示
    price_review.py report          # 表示だけ(SP-APIを呼ばない)
    price_review.py note ID 理由    # 変更に理由を付ける
    price_review.py record --sku SKU --on YYYY-MM-DD --old-price P --new-price P --old-min P --new-min P [--reason ...]
                                    # スナップショットを取る前の変更を手で登録する
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import ops_finance as of
import pricing_rule as pr
from sp_api import client
from sp_api.config import settings

REVIEW_DAYS = 14          # 変更後この日数で判定する(変更前も同じ日数と比べる)
MIN_STOCK_DAYS = 7        # 変更前後どちらかの在庫あり日数がこれ未満なら判断保留
MIN_UNITS = 3             # 変更前後の販売数の合計がこれ未満なら判断保留
EFFECT_THRESHOLD = 0.10   # 1日あたり利益がこれ以上増えた/減ったら効果あり/なし
MARKET_SHIFT = 0.30       # 競合の出品数がこれ以上増減したら「競合の変化あり」
BUY_BOX_SHIFT = 0.05      # Buy Box平均がこれ以上動いたら「競合の変化あり」


def unit_profit(price: float, fees: dict, cost_jpy: float | None, shipping: bool = True,
                tax_included: bool = False) -> float | None:
    """1個の利益(USD)。式は market_prices.roi_rows と同じ(原価は仕入れ金額の平均、物流費・関税は pricing_rule の定数)。
    LINEのまとめ用(CEO 2026-11-16): shipping=False で国際送料を引かない、tax_included=True で原価を税込にする。"""
    if price is None or not cost_jpy:
        return None
    cost_usd = cost_jpy * (pr.JP_CONSUMPTION_TAX_FACTOR if tax_included else 1.0) / pr.EXCHANGE_RATE
    intl = pr.INTL_SHIPPING_USD if shipping else 0.0
    return round(price - fees["total"] - intl - cost_usd * pr.IMPORT_DUTY_RATE - cost_usd, 2)


def _costs(conn: sqlite3.Connection) -> dict:
    return {a: v for a, v in conn.execute(
        'SELECT asin, SUM(IFNULL(amount_jpy, 0)) / SUM(quantity) FROM jp_purchase_records '
        'WHERE asin IS NOT NULL AND quantity > 0 GROUP BY asin')}


def _schedule_value(offer: dict, key: str) -> float | None:
    try:
        return float(offer[key][0]["schedule"][0]["value_with_tax"])
    except (KeyError, IndexError, TypeError):
        return None


def fetch_listings() -> list[dict]:
    """出品中の全SKUの価格・最低/最高価格・商品名(読み取りのみ)。
    一覧(searchListingsItems)の価格は反映が遅れることがあるので、SKUごとに出品データを読む。"""
    rows = []
    for listing in pr.list_listings():
        time.sleep(0.25)
        data = client._request(
            f"/listings/2021-08-01/items/{settings.seller_id}/{listing['sku']}",
            {"marketplaceIds": settings.marketplace_id, "includedData": "attributes,summaries"},
        )
        offer = ((data.get("attributes") or {}).get("purchasable_offer") or [{}])[0]
        rows.append({
            "sku": listing["sku"],
            "asin": listing["asin"],
            "item_name": ((data.get("summaries") or [{}])[0].get("itemName") or "")[:80],
            "our_price": _schedule_value(offer, "our_price"),
            "min_price": _schedule_value(offer, "minimum_seller_allowed_price"),
            "max_price": _schedule_value(offer, "maximum_seller_allowed_price"),
        })
    return rows


def save_snapshot(conn: sqlite3.Connection, today: str, listings: list[dict]) -> None:
    market = {r[0]: r[1:] for r in conn.execute('SELECT asin, buy_box_usd, lowest_fba_usd, offer_count FROM market_prices')}
    stock = {a: q for a, q in conn.execute('SELECT asin, fulfillable_quantity FROM sp_fba_inventory')}
    for l in listings:
        bb, low, offers = market.get(l["asin"], (None, None, None))
        conn.execute(
            '''INSERT OR REPLACE INTO price_snapshots
               (snapshot_on, sku, asin, item_name, our_price, min_price, max_price, buy_box_usd, lowest_fba_usd, offer_count, fulfillable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (today, l["sku"], l["asin"], l["item_name"], l["our_price"], l["min_price"], l["max_price"],
             bb, low, offers, stock.get(l["asin"], 0)),
        )


def record_change(conn: sqlite3.Connection, sku: str, asin: str, changed_on: str, old_price, new_price,
                  old_min, new_min, fee_fn, reason: str | None = None) -> bool:
    """変更を1件記録する。同じSKU・同じ日の変更は1件だけ(2回目は無視)。"""
    cost = _costs(conn).get(asin)
    old_profit = unit_profit(old_price, fee_fn(asin, old_price), cost) if old_price is not None else None
    new_profit = unit_profit(new_price, fee_fn(asin, new_price), cost) if new_price is not None else None
    cur = conn.execute(
        '''INSERT OR IGNORE INTO price_changes
           (sku, asin, changed_on, old_price, new_price, old_min, new_min, old_unit_profit, new_unit_profit, reason, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (sku, asin, changed_on, old_price, new_price, old_min, new_min, old_profit, new_profit, reason,
         datetime.now(timezone.utc).isoformat()),
    )
    return cur.rowcount > 0


def detect_changes(conn: sqlite3.Connection, today: str, fee_fn) -> list[str]:
    """今日と前回のスナップショットで最低価格が変わったSKUを記録する。記録したSKUを返す。"""
    found = []
    for sku, asin, price, min_price in conn.execute(
        'SELECT sku, asin, our_price, min_price FROM price_snapshots WHERE snapshot_on = ?', (today,)
    ).fetchall():
        prev = conn.execute(
            '''SELECT our_price, min_price FROM price_snapshots WHERE sku = ? AND snapshot_on < ?
               ORDER BY snapshot_on DESC LIMIT 1''', (sku, today)
        ).fetchone()
        if prev is None or prev[1] is None or min_price is None or abs(prev[1] - min_price) < 0.005:
            continue
        if record_change(conn, sku, asin, today, prev[0], price, prev[1], min_price, fee_fn):
            found.append(sku)
    return found


def _units(conn: sqlite3.Connection, asin: str, first: date, last: date) -> int:
    row = conn.execute(
        '''SELECT SUM(oi.quantity) FROM sp_order_items oi JOIN sp_orders o ON o.order_id = oi.order_id
           WHERE oi.asin = ? AND IFNULL(o.order_status, '') != 'Canceled'
             AND substr(o.purchase_date, 1, 10) BETWEEN ? AND ?''',
        (asin, first.isoformat(), last.isoformat()),
    ).fetchone()
    return int(row[0] or 0)


def _market(conn: sqlite3.Connection, sku: str, first: date, last: date) -> tuple:
    """期間内のスナップショットの平均(出品数, Buy Box)。スナップショットが無ければ(None, None)。"""
    return conn.execute(
        '''SELECT AVG(offer_count), AVG(buy_box_usd) FROM price_snapshots
           WHERE sku = ? AND snapshot_on BETWEEN ? AND ?''', (sku, first.isoformat(), last.isoformat())
    ).fetchone()


def _window(conn: sqlite3.Connection, sku: str, asin: str, first: date, last: date) -> dict:
    stock_days = of.in_stock_days_by_asin(
        conn, (first - timedelta(days=1)).isoformat(), last.isoformat()
    ).get(asin, 0) if last >= first else 0
    offers, bb = _market(conn, sku, first, last)
    return {"units": _units(conn, asin, first, last), "stock_days": stock_days, "offers": offers, "buy_box": bb}


def _shifted(before, after, threshold: float) -> bool:
    return before is not None and after is not None and before > 0 and abs(after - before) / before >= threshold


def review_change(conn: sqlite3.Connection, change: dict, today: date) -> dict:
    """変更前後の売れ行き・利益を比べて判定する。変更後は昨日まで(今日は途中なので数えない)。"""
    changed = date.fromisoformat(change["changed_on"])
    after_last = min(changed + timedelta(days=REVIEW_DAYS - 1), today - timedelta(days=1))
    before = _window(conn, change["sku"], change["asin"], changed - timedelta(days=REVIEW_DAYS), changed - timedelta(days=1))
    after = _window(conn, change["sku"], change["asin"], changed, after_last)
    elapsed = max((after_last - changed).days + 1, 0)

    def per_day(w, profit):
        rate = w["units"] / w["stock_days"] if w["stock_days"] else None
        return rate, (rate * profit if rate is not None and profit is not None else None)

    before_rate, before_profit = per_day(before, change["old_unit_profit"])
    after_rate, after_profit = per_day(after, change["new_unit_profit"])
    market_note = None
    if _shifted(before["offers"], after["offers"], MARKET_SHIFT) or _shifted(before["buy_box"], after["buy_box"], BUY_BOX_SHIFT):
        market_note = "競合の変化あり(価格の効果とは限らない)"

    raised = (change["new_min"] or 0) > (change["old_min"] or 0)
    if raised and after["stock_days"] >= MIN_STOCK_DAYS and after["units"] == 0 and before["units"] > 0:
        verdict = "元に戻す候補(値上げ後、売れていない)"
    elif before["stock_days"] < MIN_STOCK_DAYS or after["stock_days"] < MIN_STOCK_DAYS:
        verdict = f"判断保留(在庫あり 前{before['stock_days']}日/後{after['stock_days']}日)"
    elif before["units"] + after["units"] < MIN_UNITS:
        verdict = "判断保留(売れた数が少ない)"
    elif before_profit is None or after_profit is None:
        verdict = "判断保留(原価が無い)"
    elif after_profit >= before_profit * (1 + EFFECT_THRESHOLD) and after_profit > 0:
        verdict = "効果あり"
    elif after_profit <= before_profit * (1 - EFFECT_THRESHOLD):
        verdict = "効果なし(元に戻す候補)" if elapsed >= REVIEW_DAYS else "効果なし(途中経過)"
    else:
        verdict = "変化なし"
    if elapsed < REVIEW_DAYS and not verdict.startswith(("効果なし", "元に戻す")):
        verdict = f"途中経過 {elapsed}/{REVIEW_DAYS}日: {verdict}"
    return {
        "before": before, "after": after, "elapsed": elapsed,
        "before_rate": before_rate, "after_rate": after_rate,
        "before_profit_per_day": before_profit, "after_profit_per_day": after_profit,
        "verdict": verdict, "market_note": market_note,
    }


def report(conn: sqlite3.Connection, today: date, days: int = 60) -> list[str]:
    names = {sku: name for sku, name in conn.execute(
        '''SELECT sku, item_name FROM price_snapshots s WHERE snapshot_on = (
               SELECT MAX(snapshot_on) FROM price_snapshots WHERE sku = s.sku)''')}
    conn.row_factory = sqlite3.Row
    changes = [dict(r) for r in conn.execute(
        'SELECT * FROM price_changes WHERE changed_on >= ? ORDER BY changed_on, id',
        ((today - timedelta(days=days)).isoformat(),))]
    conn.row_factory = None
    if not changes:
        return ["価格の変更はまだありません。"]

    def money(v):
        return f"${v:.2f}" if v is not None else "-"

    def rate(v):
        return f"{v:.2f}個/日" if v is not None else "-"

    lines = []
    for c in changes:
        r = review_change(conn, c, today)
        b, a = r["before"], r["after"]
        lines.append(f"[{c['id']}] {c['changed_on']} {names.get(c['sku']) or c['sku']}")
        lines.append(f"    最低価格 {money(c['old_min'])}→{money(c['new_min'])} / 価格 {money(c['old_price'])}→{money(c['new_price'])}"
                     f" / 1個の利益 {money(c['old_unit_profit'])}→{money(c['new_unit_profit'])}")
        lines.append(f"    売れ行き 前{b['units']}個(在庫あり{b['stock_days']}日, {rate(r['before_rate'])})"
                     f" → 後{a['units']}個(在庫あり{a['stock_days']}日, {rate(r['after_rate'])})"
                     f" / 1日の利益 {money(r['before_profit_per_day'])}→{money(r['after_profit_per_day'])}")
        lines.append(f"    判定: {r['verdict']}" + (f" ※{r['market_note']}" if r["market_note"] else ""))
        if c["reason"]:
            lines.append(f"    理由: {c['reason']}")
    return lines


def _fee_fn():
    cache: dict = {}

    def fee(asin: str, price: float) -> dict:
        key = (asin, round(price, 2))
        if key not in cache:
            time.sleep(1.1)
            cache[key] = client.get_fees_estimate(asin, round(price, 2))
        return cache[key]

    return fee


def main() -> None:
    parser = argparse.ArgumentParser(description="価格の振り返り(ドライラン。価格は変更しない)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("report", help="表示だけ(SP-APIを呼ばない)")
    note = sub.add_parser("note", help="変更に理由を付ける")
    note.add_argument("id", type=int)
    note.add_argument("reason")
    rec = sub.add_parser("record", help="スナップショットを取る前の変更を手で登録する")
    rec.add_argument("--sku", required=True)
    rec.add_argument("--on", required=True, help="新しい価格になった日 YYYY-MM-DD")
    for name in ("old-price", "new-price", "old-min", "new-min"):
        rec.add_argument(f"--{name}", type=float, required=True)
    rec.add_argument("--reason")
    args = parser.parse_args()

    of.init_ops_tables()
    today = datetime.now(timezone.utc).date()
    with sqlite3.connect(of.DB_PATH) as conn:
        if args.cmd == "note":
            conn.execute('UPDATE price_changes SET reason = ? WHERE id = ?', (args.reason, args.id))
        elif args.cmd == "record":
            asin = next((l["asin"] for l in pr.list_listings() if l["sku"] == args.sku), None)
            if not asin:
                raise SystemExit(f"出品にSKUがありません: {args.sku}")
            added = record_change(conn, args.sku, asin, args.on, args.old_price, args.new_price,
                                  args.old_min, args.new_min, _fee_fn(), args.reason)
            print("登録しました" if added else "同じSKU・同じ日の変更が登録済みです")
        elif args.cmd is None:
            save_snapshot(conn, today.isoformat(), fetch_listings())
            found = detect_changes(conn, today.isoformat(), _fee_fn())
            print(f"スナップショット {today} / 最低価格の変更 {len(found)}件 {found}")
        print("\n".join(report(conn, today)))


if __name__ == "__main__":
    main()
