#!/usr/bin/env python3
"""価格の提案(ドライラン)。価格は一切変更しない。反映はCEOの承認後に手で行う(pricing-rule スキル)。

1日1回(AWSのcron):
- fetch(検索ループ scripts/run_all_day.sh の中で朝4時、セラーマイニングの後): 在庫がある/輸送中のASINについて、Keepaで競合の出品(価格・在庫)を取り、価格帯ごとに
  まとめて competitor_layers に保存する(自社の出品は除く。約11トークン/ASIN。トークンが足りなければ待つ)。
- propose(朝、price_review.py の後): 最新の競合の価格帯・自社の売れ行き(在庫あり日数で割る)・在庫・
  手数料の実額での利益から、SKUごとに値上げ/値下げ/据え置きを決めて price_proposals に保存する。

判断(decide):
- 値上げ: 今の最低価格より上の各価格帯の1セント下を候補にし、それより安い他社在庫が市場の CLEAR_DAYS 日分
  以内で売り切れ、1個の利益が MIN_GAIN 以上増えるもののうち、利益が最大の価格。上げ幅は MAX_RAISE まで。
  手数料が上がって利益が減る価格帯(例: $10.00〜$10.85)は、利益で比べるので自然に避けられる。
- 値下げ: 在庫(輸送中を含む)が OVERSTOCK_DAYS 日分を超え、LOW_STOCK_UNITS 個以上あるとき、最安の他社の
  1セント下(下げ幅は MAX_CUT まで)。損益分岐点を下回るなら出さない。30日の見込み利益(売れる見込み×1個の利益)
  が今の価格より多いときだけ。値下げ後の売れ行きは「最安層の出品者と同じ割合で市場の販売を分ける」と仮定する。
- 評価の問題: 在庫ありで NO_SALE_DAYS 日以上売れず、すでに最安(同額含む)なら、値下げせず「評価の問題」とする。
- 在庫が少ない商品は値下げしない(CEO 2026-11-10)。

使い方:
    price_strategy.py fetch      # 競合の価格帯をKeepaから取る
    price_strategy.py propose    # 提案を作って表示する
    price_strategy.py report     # 保存済みの最新の提案を表示する
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import market_prices
import ops_finance as of
import price_review as prv
import stock_ledger

CLEAR_DAYS = 7            # 値上げ先より安い他社在庫が、市場のこの日数分以内なら値上げ
UNKNOWN_MARKET_UNITS = 10 # 市場の販売数が分からないときは、安い他社在庫がこの個数以下なら値上げ
MIN_GAIN = 0.10           # 1個の利益がこれ以上増える値上げだけ提案する
MAX_RAISE = 0.20          # 1回の値上げ幅の上限
OVERSTOCK_DAYS = 60       # 在庫がこの日数分を超えたら値下げを検討
LOW_STOCK_UNITS = 10      # 在庫がこれ未満なら値下げしない
MAX_CUT = 0.10            # 1回の値下げ幅の上限
NO_SALE_DAYS = 14         # 在庫ありでこの日数売れなければ「売れない」とみなす
KEEPA_TOKENS_PER_ASIN = 11
STATE_KEY = 'price_digest_sent_on'
FETCH_STATE_KEY = 'competitor_fetched_on_jst'
JST = timezone(timedelta(hours=9))
ALREADY_FETCHED_EXIT = 10   # fetch --once-per-day で今日の分が取得済みのときの終了コード(検索ループが見る)


def keep(reason: str) -> dict:
    return {"action": "keep", "target": None, "reason": reason}


def decide(cur_min: float, cur_price: float | None, layers: list, monthly_sold: int | None,
           stock: int, sold_30d: int, in_stock_days: int, profit) -> dict:
    """1SKUの提案。layers=[[価格, 出品者数, 在庫], ...](安い順、自社を除く)。profit(価格)=1個の利益(原価が無ければNone)。"""
    now = profit(cur_min)
    if now is None:
        return keep("原価が無い")
    market_daily = monthly_sold / 30 if monthly_sold else None
    our_rate = sold_30d / in_stock_days if in_stock_days else None
    cur_price = cur_price or cur_min
    lowest = layers[0] if layers else None

    if in_stock_days >= NO_SALE_DAYS and sold_30d == 0 and (lowest is None or cur_price <= lowest[0]):
        return {"action": "feedback", "target": None,
                "reason": f"在庫あり{in_stock_days}日で0個、すでに最安。値下げではなくセラー評価の問題"}

    cover = (stock / our_rate if our_rate else math.inf) if our_rate is not None and in_stock_days >= NO_SALE_DAYS else None
    if cover is not None and cover > OVERSTOCK_DAYS and stock >= LOW_STOCK_UNITS and lowest is not None:
        if cur_price <= lowest[0]:
            return keep(f"在庫{stock}個(約{cover:.0f}日分)だが、すでに最安")
        target = round(max(lowest[0] - 0.01, cur_price * (1 - MAX_CUT)), 2)
        cut_profit = profit(target)
        if cut_profit is None or cut_profit <= 0:
            return keep(f"在庫{stock}個(約{cover:.0f}日分)だが、${target:.2f}では赤字")
        share_rate = market_daily / (lowest[1] + 1) if market_daily else 0
        rate_cut = max(our_rate or 0, share_rate)
        keep30 = min(stock, (our_rate or 0) * 30) * profit(cur_price)
        cut30 = min(stock, rate_cut * 30) * cut_profit
        if cut30 > keep30:
            cover_text = f"約{cover:.0f}日分" if cover != math.inf else "売れていない"
            return {"action": "cut", "target": target,
                    "reason": f"在庫{stock}個({cover_text})。最安${lowest[0]:.2f}({lowest[1]}社{lowest[2]}個)の下へ。"
                              f"30日の見込み利益 ${keep30:.0f}→${cut30:.0f}"}
        return keep(f"在庫{stock}個(約{cover:.0f}日分)だが、値下げしても見込み利益が増えない")

    best = None
    for price, sellers, units in layers:
        target = round(min(price - 0.01, cur_min * (1 + MAX_RAISE)), 2)
        if target <= cur_min + 0.005:
            continue
        cheaper = sum(l[2] for l in layers if l[0] < target)
        ok = cheaper / market_daily <= CLEAR_DAYS if market_daily else cheaper <= UNKNOWN_MARKET_UNITS
        if not ok:
            break   # これより上の候補は、安い在庫がさらに多い
        gain = profit(target)
        if gain is not None and gain >= now + MIN_GAIN and (best is None or gain > best[1]):
            above = next((l for l in layers if l[0] > target), None)
            best = (target, gain, cheaper, above)
    if best:
        target, gain, cheaper, above = best
        clear = f"市場の約{cheaper / market_daily:.0f}日分" if market_daily else "市場の販売数は不明"
        above_text = f"、その上は${above[0]:.2f}に{above[2]}個" if above else ""
        return {"action": "raise", "target": target,
                "reason": f"${target:.2f}より安い他社在庫は{cheaper}個({clear}){above_text}"}
    return keep("値上げ・値下げの余地なし")


def _offer_layers(product: dict, my_seller_id: str) -> list:
    live = set(product.get("liveOffersOrder") or [])
    layers: dict = {}
    for i, offer in enumerate(product.get("offers") or []):
        if i not in live or offer.get("sellerId") == my_seller_id or offer.get("condition") != 1:
            continue
        csv = offer.get("offerCSV") or []
        if len(csv) < 3:
            continue
        price = round(csv[-2] / 100 + (csv[-1] / 100 if csv[-1] > 0 else 0), 2)
        stock = offer.get("stockCSV") or []
        layer = layers.setdefault(price, [price, 0, 0])
        layer[1] += 1
        layer[2] += stock[-1] if len(stock) >= 2 else 0
    return [layers[p] for p in sorted(layers)]


def _stock_rows() -> dict:
    rows = {}
    for r in stock_ledger.stock_pipeline():
        total = (r['at_home'] + r['planned'] + r['at_tnk'] + r['intl_transit'] + r['fc_received_inbound'] + r['fba_available'])
        rows[r['asin']] = {**r, 'total': total}
    return rows


def _jst_today() -> str:
    return datetime.now(JST).date().isoformat()


def fetched_today() -> bool:
    of.init_ops_tables()
    with sqlite3.connect(of.DB_PATH) as conn:
        return stock_ledger._get_state(conn, FETCH_STATE_KEY) == _jst_today()


def fetch() -> list:
    """Keepaのトークンは検索ループ(scripts/run_all_day.sh)と共有なので、ループの中から1日1回呼ぶ(朝4時、
    セラーマイニングの後)。ループの外から並行して呼ぶと、トークンを取り合って両方が遅くなる。"""
    from keepa_mcp import keepa_client as k
    from keepa_mcp.config import settings as keepa_settings
    from keepa_mcp.server import TOKEN_RESERVE
    from sp_api.config import settings as sp_settings

    of.init_ops_tables()
    asins = sorted(a for a, r in _stock_rows().items() if r['total'] > 0 and not a.startswith('('))
    today = datetime.now(timezone.utc).date().isoformat()
    done = []
    for i in range(0, len(asins), 3):
        batch = asins[i:i + 3]
        # 検索ループと同じく、手動の調査用の予備(TOKEN_RESERVE)は残す
        while k.get_token_status(keepa_settings.keepa_api_key)['tokens_left'] < KEEPA_TOKENS_PER_ASIN * len(batch) + TOKEN_RESERVE:
            time.sleep(60)
        for p in k.get_products(keepa_settings.keepa_api_key, 'US', asins=batch, offers_limit=20, include_stock=True):
            with sqlite3.connect(of.DB_PATH) as conn:
                conn.execute('INSERT OR REPLACE INTO competitor_layers (fetched_on, asin, monthly_sold, layers_json) VALUES (?, ?, ?, ?)',
                             (today, p['asin'], p.get('monthlySold'), json.dumps(_offer_layers(p, sp_settings.seller_id))))
            done.append(p['asin'])
    with sqlite3.connect(of.DB_PATH) as conn:
        stock_ledger._set_state(conn, FETCH_STATE_KEY, _jst_today())
    return done


def propose(fee_fn=None) -> list:
    of.init_ops_tables()
    fee_fn = fee_fn or prv._fee_fn()
    stock = _stock_rows()
    today = datetime.now(timezone.utc).date().isoformat()
    with sqlite3.connect(of.DB_PATH) as conn:
        costs = prv._costs(conn)
        snaps = conn.execute(
            '''SELECT sku, asin, our_price, min_price FROM price_snapshots
               WHERE snapshot_on = (SELECT MAX(snapshot_on) FROM price_snapshots)''').fetchall()
        layers = {a: (m, json.loads(j)) for a, m, j in conn.execute(
            '''SELECT asin, monthly_sold, layers_json FROM competitor_layers c WHERE fetched_on = (
                   SELECT MAX(fetched_on) FROM competitor_layers WHERE asin = c.asin) AND fetched_on >= ?''',
            ((datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat(),))}
        results = []
        for sku, asin, price, min_price in snaps:
            row = stock.get(asin)
            if asin not in layers or not row or not row['total'] or min_price is None:
                continue
            monthly, lay = layers[asin]
            p = decide(min_price, price, lay, monthly, row['total'], row['sold_30d'], row['in_stock_days'],
                       lambda x: prv.unit_profit(x, fee_fn(asin, x), costs.get(asin)))
            profit_now = prv.unit_profit(min_price, fee_fn(asin, min_price), costs.get(asin))
            profit_target = prv.unit_profit(p['target'], fee_fn(asin, p['target']), costs.get(asin)) if p['target'] else None
            conn.execute(
                '''INSERT OR REPLACE INTO price_proposals
                   (proposed_on, sku, asin, action, current_min, target, profit_now, profit_target, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (today, sku, asin, p['action'], min_price, p['target'], profit_now, profit_target, p['reason']))
            results.append({**p, 'sku': sku, 'name': row.get('name') or sku, 'current_min': min_price,
                            'profit_now': profit_now, 'profit_target': profit_target})
    return results


def _short_name(name: str) -> str:
    """在庫台帳の商品名(商品名+内訳)で重なる語を1回にする(例: 「ウカンムリクリップ ウカンムリクリップ　ピーチ」)。"""
    words = []
    for w in re.split(r'[\s　]+', name or ''):
        if w and w not in words:
            words.append(w)
    return ' '.join(words)


def latest_proposals(conn: sqlite3.Connection) -> tuple[str | None, list]:
    day = conn.execute('SELECT MAX(proposed_on) FROM price_proposals').fetchone()[0]
    if not day:
        return None, []
    names = {r['asin']: _short_name(r['name']) for r in stock_ledger.stock_pipeline()}
    rows = []
    for sku, asin, action, cur, target, pn, pt, reason in conn.execute(
        'SELECT sku, asin, action, current_min, target, profit_now, profit_target, reason FROM price_proposals WHERE proposed_on = ?', (day,)
    ):
        rows.append({'sku': sku, 'name': names.get(asin) or sku, 'action': action, 'current_min': cur, 'target': target,
                     'profit_now': pn, 'profit_target': pt, 'reason': reason})
    return day, rows


def format_lines(rows: list) -> list:
    label = {'raise': '値上げ', 'cut': '値下げ', 'feedback': '評価の問題'}
    lines = []
    for r in sorted(rows, key=lambda r: ['raise', 'cut', 'feedback', 'keep'].index(r['action'])):
        if r['action'] == 'keep':
            continue
        price = f" 最低${r['current_min']:.2f}→${r['target']:.2f}" if r['target'] else ''
        profit = (f" 利益${r['profit_now']:.2f}→${r['profit_target']:.2f}"
                  if r['target'] and r['profit_now'] is not None and r['profit_target'] is not None else '')
        lines.append(f"・{label[r['action']]} {r['name']}{price}{profit}")
        lines.append(f"  {r['reason']}")
    return lines


def build_price_digest() -> str | None:
    """LINEの朝のまとめ用。まだ送っていない日の提案と振り返りを1通にする(送ったら mark_digest_sent)。"""
    of.init_ops_tables()
    with sqlite3.connect(of.DB_PATH) as conn:
        day, rows = latest_proposals(conn)
        if not day or stock_ledger._get_state(conn, STATE_KEY) == day:
            return None
        today = datetime.now(timezone.utc).date()
        names = {r['asin']: _short_name(r['name']) for r in stock_ledger.stock_pipeline()}
        conn.row_factory = sqlite3.Row
        changes = [dict(r) for r in conn.execute('SELECT * FROM price_changes WHERE changed_on >= ? ORDER BY changed_on',
                                                 ((today - timedelta(days=60)).isoformat(),))]
        conn.row_factory = None
        judged = []
        for c in changes:
            verdict = prv.review_change(conn, c, today)
            if not verdict['verdict'].startswith('途中経過'):
                note = f" ※{verdict['market_note']}" if verdict['market_note'] else ''
                judged.append(f"・{names.get(c['asin']) or c['sku']}({c['changed_on'][5:]} 最低${c['old_min']:.2f}→${c['new_min']:.2f}):"
                              f" {verdict['verdict']}{note}")
    counts = {a: sum(1 for r in rows if r['action'] == a) for a in ('raise', 'cut', 'feedback')}
    lines = [f"【価格の提案(ドライラン・価格は変えていません)】{day}",
             f"値上げ{counts['raise']}件 / 値下げ{counts['cut']}件 / 評価の問題{counts['feedback']}件"]
    lines += format_lines(rows) or ["提案はありません。"]
    lines.append(f"振り返り中の価格変更: {len(changes)}件(判定が出たもの {len(judged)}件)")
    lines += judged
    low = [r for r in market_prices.roi_rows() if r['roi'] is not None and r['roi'] < market_prices.ROI_ALERT_THRESHOLD]
    if low:
        lines.append(f"相場での利益率が{market_prices.ROI_ALERT_THRESHOLD:.0%}未満:")
        lines += [f"・{names.get(r['asin']) or r['name'] or r['asin']} 相場${r['buy_box']:.2f}で利益${r['profit']:.2f}(利益率{r['roi']:.0%})"
                  for r in low]
    return "\n".join(lines)


def mark_digest_sent() -> None:
    with sqlite3.connect(of.DB_PATH) as conn:
        day = conn.execute('SELECT MAX(proposed_on) FROM price_proposals').fetchone()[0]
        if day:
            stock_ledger._set_state(conn, STATE_KEY, day)


def main() -> None:
    parser = argparse.ArgumentParser(description="価格の提案(ドライラン。価格は変更しない)")
    parser.add_argument("cmd", choices=["fetch", "propose", "report"])
    parser.add_argument("--once-per-day", action="store_true", help="fetch: 今日(JST)の分が取得済みなら何もせず終了コード10")
    args = parser.parse_args()
    if args.cmd == "fetch":
        if args.once_per_day and fetched_today():
            raise SystemExit(ALREADY_FETCHED_EXIT)
        print(f"競合の価格帯を取得: {fetch()}")
        return
    if args.cmd == "propose":
        propose()
    with sqlite3.connect(of.DB_PATH) as conn:
        day, rows = latest_proposals(conn)
    print(f"提案 {day}")
    print("\n".join(format_lines(rows) or ["提案はありません。"]))


if __name__ == "__main__":
    main()
