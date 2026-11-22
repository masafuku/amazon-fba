"""アーティファクト「FBA在庫と販売」の3つの表(CSV)を作り直す。読み取りのみ(DBもAmazonも変えない)。

AWSで実行する(DBはAWSが正本。SP-APIの認証情報もAWSの.envにある):
    PYTHONPATH=. .venv/bin/python dashboard_snapshot.py --out /home/ubuntu/dashboard_out

出力:
  stock.csv      商品ごとの 送った数・売れた数・在庫・今の価格・1個の利益・売れた分の利益
  shipments.csv  便ごとの状況と収支(国際送料・関税は最後にまとめて引く)
  ship_items.csv 便ごと・商品ごとの内訳

計算の決まり(2026-11-14〜15 CEOと確認):
  - 売れた分: 注文の実際の販売価格。価格がまだ確定していない注文は相場のBuy Box(収支ページと同じ)。
    手数料は確定分(sp_financial_events)、無ければ見込み。
  - 残り(「全部売れたら」): 今の出品価格。入荷前で値上げしている商品は元の価格(meta の orig_prices)で計算。
  - 利益は送料前(手数料と原価だけ引く)。国際送料・関税は便ごとに最後にまとめて引く。
  - どの便から売れたかは分からないので、古い便から売れたとみなす(収支ページと同じ)。
  - 手数料(Product Fees API)は (ASIN, 価格) ごとにキャッシュし、価格が変わったときだけ問い合わせる。

便の目標(2026-11-15 CEO):
  - TNKから国際発送した便は、その時点の見込み(全部売れたときの送料後の利益とROI)を目標として
    dashboard_targets.json に1回だけ記録し、以後は変えない。
  - 発送時の価格: 発送日から3日以内の価格記録(price_snapshots)があればそれ、無ければ今の出品価格。
    価格記録が始まる前に発送した便(第1・2便)は、各商品が最初に売れたときの実際の価格で後から復元(A案)。
    入荷前の値上げ中の価格は元の価格に置き換える。
  - 今の見込みと目標の差を「価格の変化・手数料・紛失・送料の確定」に分けて出す(足すと差に一致する)。
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ops_finance as of
import stock_ledger as sl
from sp_api import client as sp
from sp_api.config import settings

JST = timezone(timedelta(hours=9))
USD_JPY = 150.0
DUTY_RATE = 0.125
FEE_CACHE_DAYS = 30
DEFAULT_META = Path(__file__).with_name('dashboard_meta.json')
DEFAULT_FEE_CACHE = Path.home() / '.cache' / 'dashboard_fees.json'
DEFAULT_TARGETS = Path(__file__).with_name('dashboard_targets.json')
SNAPSHOT_WINDOW_DAYS = 3


# ---------- 商品名 ----------
CHAR = {'ハローキティ': 'キティ', 'キティ：ピンク': 'キティ'}


def short_name(name: str | None, variant: str | None) -> str:
    n = name or ''
    v = CHAR.get((variant or '').strip(), (variant or '').strip())
    if 'パタップ' in n:
        if 'CB' in v:
            return 'パタップ クリアブルー'
        if 'CY' in v:
            return 'パタップ クリアイエロー'
        return 'パタップ クリア'
    if 'ウカンムリ' in n:
        return 'ウカンムリ ' + (v or n).replace('ウカンムリクリップ', '').strip('　 ')
    if 'シール' in n:
        return 'シール&ケース ' + (v or 'キティ')
    if '定規' in n:
        return 'スリム定規 ' + (v or 'キティ')
    if 'フリクション3本' in n or 'FriXion' in n:
        return 'フリクション3本セット ' + (v or 'キティ')
    if 'フリクションイレーザー' in n:
        return 'フリクションイレーザー ' + v
    if 'エナージェル' in n:
        return 'エナージェル ' + v
    if 'リングノート' in n:
        return 'B6リングノート ' + v
    if 'ボトル' in n:
        return 'ステンレスボトル ' + v
    if '前髪' in n:
        return '前髪クリップ ' + v
    if 'マスコット' in n:
        return 'マスコットホルダー ' + v
    for key, label in (('毎日香', '毎日香'), ('サラサーティ', 'サラサーティ'), ('ルーズノート', 'コクヨ ルーズノート中紙'),
                       ('かどまる', 'かどまるPRO'), ('ジェットストリーム', 'ジェットストリーム替芯'),
                       ('はさみ', '万能分別はさみ'), ('ホルベイン', 'ホルベイン ガッシュ')):
        if key in n:
            return label
    return n


def md(day: str | None) -> str:
    if not day:
        return ''
    d = datetime.fromisoformat(day[:10])
    return f'{d.month}/{d.day}'


def r2(x):
    return None if x is None else round(x, 2)


# ---------- 手数料(キャッシュ付き) ----------
class Fees:
    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        self.calls = 0
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except ValueError:
                self.data = {}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=FEE_CACHE_DAYS)).isoformat()
        self.data = {k: v for k, v in self.data.items() if v.get('at', '') >= cutoff}

    def total(self, asin: str, price: float) -> float | None:
        key = f'{asin}|{price:.2f}'
        if key not in self.data:
            try:
                time.sleep(1.1)   # Product Fees API は1秒に1回まで
                f = sp.get_fees_estimate(asin, round(price, 2))
                self.calls += 1
            except Exception as e:   # noqa: BLE001 - 1商品の失敗で全体を止めない
                print(f'手数料取得失敗 {asin} ${price}: {e}')
                return None
            self.data[key] = {'total': f['total'], 'at': datetime.now(timezone.utc).isoformat()}
        return self.data[key]['total']

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))


# ---------- データ集め ----------
def listing_prices() -> tuple[dict, dict]:
    """(SKU -> 今の出品価格, ASIN -> SKUの集合)。一覧に価格が無い(在庫なしのFBA等)SKUは price_snapshots の最新で補う。"""
    out, asin_skus, tok = {}, defaultdict(set), None
    while True:
        r = sp.search_listings_items(tok, 20)
        for it in r.get('items') or []:
            summ = (it.get('summaries') or [{}])[0]
            if summ.get('asin'):
                asin_skus[summ['asin']].add(it['sku'])
            for o in it.get('offers') or []:
                if o.get('offerType') == 'B2C' and o.get('price'):
                    out[it['sku']] = float(o['price']['amount'])
        tok = (r.get('pagination') or {}).get('nextToken')
        if not tok:
            break
        time.sleep(0.6)
    with sqlite3.connect(of.DB_PATH) as conn:
        try:
            for sku, price in conn.execute(
                    '''SELECT sku, our_price FROM price_snapshots p WHERE our_price IS NOT NULL AND snapshot_on =
                       (SELECT MAX(snapshot_on) FROM price_snapshots q WHERE q.sku = p.sku AND q.our_price IS NOT NULL)'''):
                out.setdefault(sku, price)
        except sqlite3.OperationalError:
            pass
    return out, asin_skus


def received_by_sku(confirmation_id: str) -> dict:
    try:
        r = sp._request(f'/fba/inbound/v0/shipments/{confirmation_id}/items', {'MarketplaceId': settings.marketplace_id})
    except Exception as e:   # noqa: BLE001
        print(f'受領数の取得失敗 {confirmation_id}: {e}')
        return {}
    time.sleep(1)
    return {it['SellerSKU']: it.get('QuantityReceived') or 0 for it in (r.get('payload') or {}).get('ItemData') or []}


def next_shipping_per_unit(asins) -> dict:
    """次の便の商品ごとの1個あたり国際送料(本番の送料モデル: ops_finance.estimate_intl_shipping_usd)。
    重さ・寸法は候補の記録(agent_candidates.data_json)から取る。無ければ一律額になる。"""
    out = {}
    with sqlite3.connect(of.DB_PATH) as conn:
        for asin in asins:
            row = conn.execute(
                'SELECT data_json FROM agent_candidates WHERE asin = ? AND data_json IS NOT NULL '
                'ORDER BY created_at DESC LIMIT 1', (asin,)).fetchone()
            data = json.loads(row[0]) if row and row[0] else {}
            out[asin] = of.estimate_intl_shipping_usd(data.get('weight_kg'), data.get('dims_cm'))['usd']
    return out


def build(meta: dict, fees: Fees, today: str, targets: dict) -> tuple[list, list, list]:
    orig_prices = meta.get('orig_prices') or {}
    intl_est = float(meta.get('intl_per_unit_estimate_usd', 0.742))
    ship_meta = meta.get('shipments') or {}

    ships = sorted(of.get_shipment_pnl(), key=lambda s: s['deliveryWindowStart'] or '')
    prices, listed_skus = listing_prices()

    conn = sqlite3.connect(of.DB_PATH)
    conn.row_factory = sqlite3.Row
    created = {r[0]: r[1] for r in conn.execute('SELECT shipment_confirmation_id, created_at FROM sp_inbound_shipments')}
    purchases = conn.execute(
        '''SELECT order_date, supplier_name, sd_reception_no, sd_product_no, product_name, variant, quantity, amount_jpy, asin,
                  unit_price_jpy, COALESCE(shipping_cost_jpy, 0) AS ship_jpy, supplier_shipped_at, carrier, tracking_no,
                  received_at FROM jp_purchase_records ORDER BY order_date''').fetchall()

    names, hist = {}, defaultdict(list)
    unlinked_names = {}   # SD品番 -> 商品名(ASIN未紐付けの仕入れ)
    for p in conn.execute('SELECT sd_product_no, product_name, variant FROM jp_purchase_records WHERE asin IS NULL'):
        unlinked_names[p['sd_product_no']] = short_name(p['product_name'], p['variant'])
    for p in purchases:
        if p['asin']:
            names[p['asin']] = short_name(p['product_name'], p['variant'])
            q = p['quantity'] or 0
            hist[p['asin']].append((p['order_date'] or '', (p['unit_price_jpy'] or 0) + (p['ship_jpy'] / q if q else 0)))

    def landed(asin, asof):
        h = hist.get(asin)
        if not h:
            return 0.0
        c = [x for d, x in h if d <= (asof or '9')]
        return c[-1] if c else h[0][1]

    avg_cost = {a: v for a, v in conn.execute(
        'SELECT asin, SUM(amount_jpy) * 1.0 / SUM(quantity) FROM jp_purchase_records WHERE asin IS NOT NULL AND quantity > 0 GROUP BY asin')}

    # 注文(売れた分): 実際の価格、未確定はBuy Box。手数料は確定分か見込み
    sold_items = conn.execute(
        '''SELECT oi.order_id, oi.asin, oi.sku, oi.quantity, oi.item_price_usd, o.purchase_date, o.order_status
           FROM sp_order_items oi JOIN sp_orders o ON o.order_id = oi.order_id
           WHERE oi.asin IS NOT NULL AND UPPER(IFNULL(o.order_status, '')) NOT IN ('CANCELED', 'CANCELLED')
           ORDER BY o.purchase_date''').fetchall()
    bb = {r[0]: r[1] for r in conn.execute('SELECT asin, buy_box_usd FROM market_prices')}
    actual = {r[0]: r[1] or 0.0 for r in conn.execute('SELECT order_id, SUM(amount_usd) FROM sp_financial_events GROUP BY order_id')}
    est = of._estimated_fees_by_order(conn, '2000-01-01', set(actual))
    qty_by_order = defaultdict(int)
    for r in sold_items:
        qty_by_order[r['order_id']] += r['quantity'] or 0
    queue = defaultdict(list)
    sales_by_sku = defaultdict(int)
    for r in sold_items:
        fee = actual.get(r['order_id'], est.get(r['order_id'], 0.0))
        q = qty_by_order[r['order_id']] or 1
        price, _ = of._estimated_price(r, bb)
        queue[r['asin']].append({'qty': r['quantity'] or 0, 'rev': price or 0.0, 'fee': fee / q,
                                 'pending': (r['order_status'] or '') != 'Shipped', 'date': (r['purchase_date'] or '')[:10]})
        sales_by_sku[r['sku']] += r['quantity'] or 0

    events = defaultdict(dict)
    for t, d, ref, det in conn.execute(
            '''SELECT event_type, occurred_on, ref, detail FROM ops_events
               WHERE event_type IN ('tnk_arrived', 'tnk_shipped', 'fba_checked_in', 'fba_receiving', 'fba_closed')
               ORDER BY occurred_on'''):
        events[ref].setdefault(t, {**json.loads(det or '{}'), 'date': d})
    lost = defaultdict(int)          # (確認番号, ASIN) -> 個数
    lost_notes = defaultdict(list)
    for ref, d, det in conn.execute("SELECT ref, occurred_on, detail FROM ops_events WHERE event_type = 'fba_lost'"):
        x = json.loads(det or '{}')
        lost[(ref, x.get('asin'))] += x.get('qty') or 0
        lost_notes[ref].append(f"{x.get('product') or names.get(x.get('asin'), '')}は{x.get('sent')}個送ってAmazonの受け取りが"
                               f"{x.get('received')}個。足りない{x.get('qty')}個は紛失として処理（{md(d)}）")
    intl_costs = {r[0]: r[1] for r in conn.execute('SELECT shipment_confirmation_id, shipping_jpy FROM shipment_intl_costs')}
    inv = defaultdict(int)
    for asin, qty in conn.execute('SELECT asin, fulfillable_quantity FROM sp_fba_inventory'):
        inv[asin] += qty or 0
    since = (datetime.fromisoformat(today) - timedelta(days=365)).date().isoformat()
    in_stock_days = of.in_stock_days_by_asin(conn, since, today)
    first_sale = {}   # ASIN -> 最初に売れたときの1個の価格
    for r in sold_items:
        if r['item_price_usd'] and r['quantity'] and r['asin'] not in first_sale:
            first_sale[r['asin']] = r['item_price_usd']   # item_price_usd は1個あたり(ops_finance と同じ)
    snapshots = defaultdict(list)   # SKU -> [(日付, 価格)]
    try:
        for sku, day, price in conn.execute(
                'SELECT sku, snapshot_on, our_price FROM price_snapshots WHERE our_price IS NOT NULL ORDER BY snapshot_on'):
            snapshots[sku].append((day, price))
    except sqlite3.OperationalError:
        pass
    first_snapshot_day = min((v[0][0] for v in snapshots.values()), default=None)
    conn.close()

    # 1個の利益(送料前) = 価格 − 手数料 − 原価
    def unit_profit(asin, price, cost_jpy):
        if not price or cost_jpy is None:
            return None
        f = fees.total(asin, price)
        return None if f is None else price - f - cost_jpy / USD_JPY

    def main_sku(asin, skus):
        return sorted(skus, key=lambda s: (-sales_by_sku.get(s, 0), prices.get(s) is None))[0]

    # ---------- 便ごと・商品ごと ----------
    item_rows = []
    ship_rows = []
    alloc = []   # 売れた分を便に割り振った記録(日付ごとの推移に使う)
    sent_by_asin = defaultdict(int)
    first_ship = {}
    for n, s in enumerate(ships, 1):
        cid = s['shipmentConfirmationId']
        label = f'第{n}便'
        units = sum(i['quantity'] or 0 for i in s['items'])
        ipu = (s['intlShippingUsd'] or 0) / units if units else 0
        rec = received_by_sku(cid)
        by_asin = {}
        for it in s['items']:
            a = it['asin']
            sent_by_asin[a] += it['quantity'] or 0
            first_ship.setdefault(a, n)
            x = by_asin.setdefault(a, {'qty': 0, 'received': 0, 'skus': []})
            x['qty'] += it['quantity'] or 0
            x['received'] += rec.get(it['sku'], 0)
            x['skus'].append(it['sku'])
        for a, x in by_asin.items():
            rem, sold, pend, rev, fee = x['qty'], 0, 0, 0.0, 0.0
            while rem > 0 and queue[a]:
                q = queue[a][0]
                tk = min(rem, q['qty'])
                sold += tk
                rev += tk * q['rev']
                fee += tk * q['fee']
                pend += tk if q['pending'] else 0
                alloc.append({'n': n, 'asin': a, 'date': q['date'], 'qty': tk, 'net': tk * (q['rev'] - q['fee']), 'rev': tk * q['rev']})
                q['qty'] -= tk
                rem -= tk
                if q['qty'] <= 0:
                    queue[a].pop(0)
            item_rows.append(dict(ship=label, n=n, cid=cid, asin=a, product=names.get(a, a), sent=x['qty'],
                                  received=x['received'], sold=sold, pending=pend, revenue=rev, fees=fee,
                                  cost_jpy=landed(a, s['deliveryWindowStart']), ipu=ipu,
                                  lost=lost.get((cid, a), 0), sku=main_sku(a, x['skus'])))
        ship_rows.append(dict(n=n, ship=label, s=s, units=units))

    # 次の便: 最後の納品プランを作った後に注文した仕入れ(まだ納品プランに入っていない)
    last_created = max((created.get(s['shipmentConfirmationId']) or '' for s in ships), default='')[:10]
    nxt = [p for p in purchases if p['asin'] and (p['order_date'] or '') > last_created]
    next_n = len(ships) + 1
    next_by_asin = defaultdict(lambda: {'qty': 0, 'home': 0, 'amount': 0.0})
    for p in nxt:
        x = next_by_asin[p['asin']]
        x['qty'] += p['quantity'] or 0
        x['home'] += (p['quantity'] or 0) if p['received_at'] else 0
        x['amount'] += p['amount_jpy'] or 0
    next_ipu = next_shipping_per_unit(next_by_asin.keys())
    for a, x in next_by_asin.items():
        item_rows.append(dict(ship=f'第{next_n}便', n=next_n, cid='', asin=a, product=names.get(a, a), sent=x['qty'],
                              received=None, sold=0, pending=0, revenue=0.0, fees=0.0,
                              cost_jpy=x['amount'] / x['qty'] if x['qty'] else 0, ipu=next_ipu.get(a, intl_est), lost=0, sku=None))

    # SKUの対応(ASIN -> 出品SKU)。次の便の商品は出品一覧から
    asin_skus = defaultdict(set)
    for s in ships:
        for it in s['items']:
            asin_skus[it['asin']].add(it['sku'])
    for a, skus in listed_skus.items():
        asin_skus[a] |= skus

    def price_of(asin):
        skus = list(asin_skus.get(asin) or [])
        if not skus:
            return None, None
        sku = main_sku(asin, skus)
        return sku, prices.get(sku)

    # 残り1個の利益(送料前)。値上げ中は元の価格で
    def capped(sku, price):
        o = orig_prices.get(sku)
        return o if o and price and price > o else price

    def base_price(asin):
        """残りを見込む価格(値上げ中は元の価格)と、その手数料。"""
        sku, price = price_of(asin)
        if price is None:
            return None, None
        base = capped(sku, price)
        return base, fees.total(asin, base)

    def remaining_unit_profit(asin, cost_jpy):
        base, _ = base_price(asin)
        return None if base is None else unit_profit(asin, base, cost_jpy)

    def target_price(asin, shipped_on):
        """発送時の価格と、その根拠。"""
        sku, now = price_of(asin)
        if first_snapshot_day and shipped_on < first_snapshot_day and \
                (datetime.fromisoformat(first_snapshot_day) - datetime.fromisoformat(shipped_on)).days > SNAPSHOT_WINDOW_DAYS:
            if asin in first_sale:
                return capped(sku, first_sale[asin]), 'first_sale'
            snap = snapshots.get(sku) or []
            return (capped(sku, snap[0][1]), 'first_snapshot') if snap else (capped(sku, now), 'now')
        limit = (datetime.fromisoformat(shipped_on) + timedelta(days=SNAPSHOT_WINDOW_DAYS)).date().isoformat()
        for day, price in snapshots.get(sku) or []:
            if shipped_on <= day <= limit:
                return capped(sku, price), 'snapshot'
        return capped(sku, now), 'now'

    # 販売可能数を古い便から割り振る
    sell_left = dict(inv)
    for r in sorted(item_rows, key=lambda r: r['n']):
        c = r['cost_jpy'] / USD_JPY
        proceeds = r['revenue'] - r['fees']
        left = r['sent'] - r['sold'] - r['lost']
        u = remaining_unit_profit(r['asin'], r['cost_jpy'])
        r['p_now'], r['f_now'] = base_price(r['asin'])
        ship_cost = r['sent'] * (r['ipu'] + c * DUTY_RATE)
        proj_b = None if u is None else proceeds - r['sold'] * c + left * u - r['lost'] * c
        if r['cid']:
            take = min(sell_left.get(r['asin'], 0), max(left, 0))
            sell_left[r['asin']] = sell_left.get(r['asin'], 0) - take
            r['sellable'] = take
        else:
            r['sellable'] = ''
        r.update(left=left, cost_usd=r['sent'] * c, proceeds_usd=proceeds, profit_now=proceeds - r['sent'] * c,
                 ship_usd=ship_cost, unit_profit=u, proj_before=proj_b,
                 proj_after=None if proj_b is None else proj_b - ship_cost)

    # ---------- 便ごと ----------
    def tot(n, f):
        vals = [r[f] for r in item_rows if r['n'] == n]
        return None if any(v is None for v in vals) else sum(vals)

    # ---------- 便の目標(発送時に固定)と、今の見込みとの差 ----------
    for x in ship_rows:
        s, n, cid = x['s'], x['n'], x['s']['shipmentConfirmationId']
        shipped_on = (events.get(cid, {}).get('tnk_shipped') or {}).get('date')
        if cid in targets or not shipped_on:
            continue
        items_t, bases = {}, set()
        for r in item_rows:
            if r['n'] != n:
                continue
            price, basis = target_price(r['asin'], shipped_on)
            fee = fees.total(r['asin'], price) if price else None
            if price is None or fee is None:
                items_t = None
                break
            bases.add(basis)
            t = items_t.setdefault(r['asin'], {'qty': 0, 'price': price, 'fee': round(fee, 4), 'cost_usd': 0.0})
            t['qty'] += r['sent']
            t['cost_usd'] += r['sent'] * r['cost_jpy'] / USD_JPY
        if not items_t:
            print(f'{x["ship"]}の目標は価格か手数料が取れず、今回は記録しない')
            continue
        ship_t = (s['intlShippingUsd'] or 0) + (s['dutyUsd'] or 0)
        cost_t = sum(t['cost_usd'] for t in items_t.values())
        proceeds_t = sum(t['qty'] * (t['price'] - t['fee']) for t in items_t.values())
        profit_t = proceeds_t - cost_t - ship_t
        targets[cid] = {
            'frozen_on': today, 'shipped_on': shipped_on,
            'basis': 'restored_first_sale' if 'first_sale' in bases or 'first_snapshot' in bases else 'at_shipping',
            'ship_usd': round(ship_t, 2), 'cost_usd': round(cost_t, 2), 'proceeds_usd': round(proceeds_t, 2),
            'profit_usd': round(profit_t, 2), 'roi': round(profit_t / (cost_t + ship_t), 4) if cost_t + ship_t else None,
            'items': {a: {**t, 'cost_usd': round(t['cost_usd'], 4)} for a, t in items_t.items()}}
        print(f'{x["ship"]}の目標を記録: 利益${profit_t:.2f}')

    def variance(n, cid, ship_now):
        t = targets.get(cid)
        if not t:
            return None
        v = {'price': 0.0, 'fee': 0.0, 'lost': 0.0}
        for r in item_rows:
            if r['n'] != n:
                continue
            ti = t['items'].get(r['asin'])
            if not ti:
                continue
            q_s, q_l = r['sold'], r['lost']
            q_r = r['sent'] - q_s - q_l
            p_now = r['p_now'] if r['p_now'] is not None else ti['price']
            f_now = r['f_now'] if r['f_now'] is not None else ti['fee']
            vp = (r['revenue'] + q_r * p_now) - (q_s + q_r) * ti['price']
            vf = (q_s + q_r) * ti['fee'] - (r['fees'] + q_r * f_now)
            vl = -q_l * (ti['price'] - ti['fee'])
            v['price'] += vp
            v['fee'] += vf
            v['lost'] += vl
            # 商品ごと: 目標の価格、今の平均価格(売れた分の実際の価格と残りの見込み価格の平均)、差の内訳
            r.update(t_price=ti['price'], t_units=q_s + q_r, t_rev=(q_s + q_r) * ti['price'],
                     now_rev=r['revenue'] + q_r * p_now, var_price=vp, var_fee=vf, var_lost=vl)
        v['ship'] = t['ship_usd'] - ship_now
        return v

    def target_cols(n, cid, ship_now, cost_now, proj_before):
        t = targets.get(cid)
        empty = dict(target_profit='', target_roi='', target_proceeds='', target_cost='', target_on='', target_basis='',
                     proj_roi='', var_price='', var_fee='', var_lost='', var_ship='')
        if not t:
            return empty
        v = variance(n, cid, ship_now)
        proj = None if proj_before is None else proj_before - ship_now
        return dict(target_profit=t['profit_usd'], target_roi=t['roi'], target_proceeds=t['proceeds_usd'],
                    target_cost=round(t['cost_usd'] + t['ship_usd'], 2), target_on=md(t['shipped_on']),
                    target_basis='後から復元（最初に売れた価格）' if t['basis'] == 'restored_first_sale' else '発送時の価格',
                    proj_roi='' if proj is None or not cost_now + ship_now else round(proj / (cost_now + ship_now), 4),
                    var_price=r2(v['price']), var_fee=r2(v['fee']), var_lost=r2(v['lost']), var_ship=r2(v['ship']))

    shipments_out = []
    for x in ship_rows:
        s, n, cid = x['s'], x['n'], x['s']['shipmentConfirmationId']
        ev = events.get(cid, {})
        m = ship_meta.get(cid, {})
        if 'fba_closed' in ev or (s['status'] or '').upper() == 'CLOSED':
            stage, status = 6, '受領完了'
        elif 'fba_checked_in' in ev or 'fba_receiving' in ev:
            stage, status = 5, '受領中'
        elif 'tnk_shipped' in ev:
            stage, status = 4, '国際輸送中'
        elif 'tnk_arrived' in ev:
            stage, status = 3, 'TNK着'
        else:
            stage, status = 2, '発送準備中'
        units = x['units']
        received = sum(r['received'] or 0 for r in item_rows if r['n'] == n)
        sold = sum(r['sold'] for r in item_rows if r['n'] == n)
        sellable = sum(r['sellable'] or 0 for r in item_rows if r['n'] == n)
        nlost = sum(r['lost'] for r in item_rows if r['n'] == n)
        cost = s['costUsd'] or 0
        proceeds = tot(n, 'proceeds_usd') or 0
        intl_only = s['intlShippingUsd'] or 0
        duty = s['dutyUsd'] or 0
        intl = intl_only + duty
        notes = list(lost_notes.get(cid, []))
        if stage == 5:
            if received < units:
                notes.append(f'Amazonが受け取ったのは{units}個中{received}個')
            elif sellable == 0:
                notes.append(f'{units}個すべて受け取り済みで、販売可能になるのを待っている')
        if m.get('note'):
            notes.append(m['note'])
        shipped = ev.get('tnk_shipped') or {}
        pb = tot(n, 'proj_before')
        shipments_out.append(dict(
            ship=x['ship'], fba_id=cid, fc=s['destinationFc'] or '', status=status, stage=stage,
            d_order=m.get('d_order', ''), d_home=m.get('d_home', ''), d_tnk=md((ev.get('tnk_arrived') or {}).get('date')),
            d_intl=md(shipped.get('date')),
            d_fc=md((ev.get('fba_checked_in') or ev.get('fba_receiving') or {}).get('date')),
            d_done=md((ev.get('fba_closed') or {}).get('date')),
            items=len({r['asin'] for r in item_rows if r['n'] == n}), units=units, received=received, sold=sold,
            cost_usd=r2(cost), intl_usd=r2(intl), proceeds_usd=r2(proceeds), profit_now=r2(proceeds - cost - intl),
            recovery=round(proceeds / (cost + intl), 3) if cost + intl else '', projected=r2(None if pb is None else pb - intl),
            note='。'.join(notes), intl_only=r2(intl_only), duty=r2(duty),
            track_intl=('FedEx ' + str(shipped.get('courierTrackingNo') or shipped.get('trackingNo') or '')).strip(),
            kg=shipped.get('actualKg') or '', vol_kg=shipped.get('volumetricKg') or '', track_dom='',
            proj_before=r2(pb),
            intl_fixed='' if s.get('intlCostEstimated') or cid not in intl_costs else f'TNKの請求書の実額（¥{intl_costs[cid]:,.0f}）',
            sellable=sellable, lost=nlost, **target_cols(n, cid, intl, cost, pb)))
        # 手入力の上書き(日付など。note は上で足している)
        shipments_out[-1].update({k: v for k, v in m.items() if k in shipments_out[-1] and k != 'note'})

    if nxt:
        n = next_n
        by_sup = defaultdict(list)
        for p in nxt:
            by_sup[(p['supplier_name'] or '').replace('株式会社', '').strip()].append(p)
        dom, note = [], []
        all_home = all(p['received_at'] for p in nxt)
        any_shipped = any(p['supplier_shipped_at'] or p['received_at'] for p in nxt)
        for sup, ps in by_sup.items():
            q = sum(p['quantity'] or 0 for p in ps)
            if all(p['received_at'] for p in ps):
                note.append(f'{sup}の{q}個は{md(max(p["received_at"] for p in ps))}に自宅着')
            elif any(p['supplier_shipped_at'] for p in ps):
                note.append(f'{sup}の{q}個は{md(max(p["supplier_shipped_at"] for p in ps if p["supplier_shipped_at"]))}に発送')
            else:
                note.append(f'{sup}の{q}個はまだ発送の連絡なし')
            trk = sorted({(p['carrier'] or '', p['tracking_no'] or '') for p in ps if p['tracking_no']})
            dom.append(f'{sup} ' + ' '.join(f'{c} {t}'.strip() for c, t in trk) if trk else f'{sup} 未発送')
        units = sum(p['quantity'] or 0 for p in nxt)
        cost = sum(p['amount_jpy'] or 0 for p in nxt) / USD_JPY
        intl_only = units * intl_est
        duty = cost * DUTY_RATE
        pb = tot(n, 'proj_before')
        stage, status = (2, '自宅着（納品プラン前）') if all_home else (1, '国内輸送中' if any_shipped else '仕入れ先の発送待ち')
        shipments_out.append(dict(
            ship=f'第{n}便', fba_id='', fc='', status=status, stage=stage,
            d_order=md(min(p['order_date'] for p in nxt)), d_home=md(max(p['received_at'] for p in nxt)) if all_home else '',
            d_tnk='', d_intl='', d_fc='', d_done='', items=len(next_by_asin), units=units, received=0, sold=0,
            cost_usd=r2(cost), intl_usd=r2(intl_only + duty), proceeds_usd=0, profit_now='', recovery='',
            projected=r2(None if pb is None else pb - intl_only - duty), note='。'.join(note), intl_only=r2(intl_only),
            duty=r2(duty), track_intl='', kg='', vol_kg='', track_dom='／'.join(dom), proj_before=r2(pb), intl_fixed='',
            sellable='', lost=0, **target_cols(n, '', 0, 0, None)))

    # 同じ便・同じ商品名は1行に
    merged = {}
    for r in item_rows:
        k = (r['n'], r['product'])
        if k not in merged:
            merged[k] = dict(r)
            continue
        m = merged[k]
        for f in ('sent', 'sold', 'pending', 'left', 'lost', 'cost_usd', 'proceeds_usd', 'profit_now', 'ship_usd'):
            m[f] += r[f]
        for f in ('proj_before', 'proj_after'):
            m[f] = None if m[f] is None or r[f] is None else m[f] + r[f]
        if m['sellable'] != '' and r['sellable'] != '':
            m['sellable'] += r['sellable']
        for f in ('t_units', 't_rev', 'now_rev', 'var_price', 'var_fee', 'var_lost'):
            if f in r:
                m[f] = m.get(f, 0) + r[f]
    items_out = []
    for r in sorted(merged.values(), key=lambda r: (r['n'], r['product'])):
        items_out.append(dict(ship=r['ship'], product=r['product'], sent=r['sent'], sold=r['sold'], pending=r['pending'],
                              left=r['left'], sellable=r['sellable'], lost=r['lost'], cost_usd=r2(r['cost_usd']),
                              proceeds_usd=r2(r['proceeds_usd']), profit_now=r2(r['profit_now']), ship_usd=r2(r['ship_usd']),
                              unit_profit=r2(r['unit_profit']), proj_before=r2(r['proj_before']),
                              proj_after=r2(r['proj_after']),
                              # 目標との差(目標のある便だけ)。価格は数量で割った平均
                              target_price=r2(r['t_rev'] / r['t_units']) if r.get('t_units') else '',
                              price_now=r2(r['now_rev'] / r['t_units']) if r.get('t_units') else '',
                              var_price=r2(r['var_price']) if 'var_price' in r else '',
                              var_fee=r2(r['var_fee']) if 'var_fee' in r else '',
                              var_lost=r2(r['var_lost']) if 'var_lost' in r else ''))


    # ---------- 商品ごと ----------
    pipe = {r['asin']: r for r in sl.stock_pipeline(today)}
    prod = {}

    def prow(product):
        return prod.setdefault(product, dict(product=product, sent=0, sold=0, sold_pending=0, sellable=0, home=0,
                                             incoming=0, price=None, unit_profit=None, sold_profit=0.0, has_sold=False,
                                             cost_jpy=None, in_stock_days=0, ship_per_unit=None, lost=0, order=(99, ''),
                                             asins=set()))
    for r in item_rows:
        p = prow(r['product'])
        p['asins'].add(r['asin'])
        if r['cid']:
            p['sent'] += r['sent']
            p['sold'] += r['sold']
            p['sold_pending'] += r['pending']
            p['lost'] += r['lost']
            if r['sold']:
                p['sold_profit'] += r['proceeds_usd'] - r['sold'] * r['cost_jpy'] / USD_JPY
                p['has_sold'] = True
        p['order'] = min(p['order'], (r['n'], r['product']))
    for a, x in pipe.items():
        if a.startswith('(未紐付け'):
            name = unlinked_names.get(a[len('(未紐付け:'):-1], x['name'] or a)
            if x['at_home'] or x['awaiting_supplier'] or x['domestic_transit']:
                p = prow(name)
                p['home'] += x['at_home']
                p['incoming'] += x['awaiting_supplier'] + x['domestic_transit']
            continue
        nh = next_by_asin.get(a, {}).get('home', 0) if a in next_by_asin else 0
        home_left = max(x['at_home'] - nh, 0)
        incoming = x['awaiting_supplier'] + x['domestic_transit'] + min(nh, x['at_home'])
        if not (home_left or incoming or a in names):
            continue
        p = prow(names.get(a, x['name'] or a))
        p['asins'].add(a)
        p['home'] += home_left
        p['incoming'] += incoming
    for p in prod.values():
        for a in p['asins']:
            p['sellable'] += inv.get(a, 0)
            p['in_stock_days'] = max(p['in_stock_days'], in_stock_days.get(a, 0))
        a = sorted(p['asins'], key=lambda a: -sent_by_asin.get(a, 0))[0] if p['asins'] else None
        if a:
            sku, price = price_of(a)
            cj = avg_cost.get(a)
            p['price'] = price
            p['cost_jpy'] = None if cj is None else round(cj)
            p['unit_profit'] = unit_profit(a, price, cj) if price and cj is not None else None
            p['ship_per_unit'] = None if cj is None else intl_est + cj / USD_JPY * DUTY_RATE
    # FBA在庫の内訳(倉庫間移動中・FC受領中・調査中)。積み上げグラフの区分(CEO 11/21)
    import fba_status
    sku_inv = fba_status.fetch_inventory()
    by_asin = defaultdict(lambda: {'transship': 0, 'receiving': 0, 'research': 0})
    for x in sku_inv.values():
        if x.get('asin'):
            by_asin[x['asin']]['transship'] += x['transshipment'] or 0
            by_asin[x['asin']]['receiving'] += x['inbound_receiving'] or 0
            by_asin[x['asin']]['research'] += x['researching'] or 0
    for p in prod.values():
        for k in ('transship', 'receiving', 'research'):
            p[k] = sum(by_asin[a][k] for a in p['asins'])
    # 仕入れた数(これまでに注文した数の合計。CEO 11/15)
    bought_asin, bought_unlinked = defaultdict(int), defaultdict(int)
    for x in purchases:
        if x['asin']:
            bought_asin[x['asin']] += x['quantity'] or 0
        else:
            bought_unlinked[unlinked_names.get(x['sd_product_no'], '')] += x['quantity'] or 0
    for p in prod.values():
        p['bought'] = sum(bought_asin.get(a, 0) for a in p['asins']) + bought_unlinked.get(p['product'], 0)
    stock_out = []
    for p in sorted(prod.values(), key=lambda p: (p['sent'] == 0 and p['incoming'] == 0, p['sent'] == 0, p['order'])):
        stock_out.append(dict(product=p['product'], bought=p['bought'], sent=p['sent'], sold=p['sold'], sold_pending=p['sold_pending'],
                              sellable=p['sellable'], home=p['home'], incoming=p['incoming'],
                              price='' if p['price'] is None else f"{p['price']:.2f}",
                              unit_profit='' if p['unit_profit'] is None else f"{p['unit_profit']:.2f}",
                              sold_profit=f"{p['sold_profit']:.2f}" if p['has_sold'] else '',
                              cost_jpy='' if p['cost_jpy'] is None else p['cost_jpy'],
                              in_stock_days=p['in_stock_days'] if p['price'] is not None else '',
                              ship_per_unit='' if p['ship_per_unit'] is None else f"{p['ship_per_unit']:.3f}",
                              lost=p['lost'], transship=p['transship'], receiving=p['receiving'], research=p['research']))
    ctx = dict(today=today, item_rows=item_rows, ship_rows=ship_rows, shipments_out=shipments_out, alloc=alloc,
               targets=targets, events=events, snapshots=snapshots, bb=bb, price_of=price_of, capped=capped,
               base_price=base_price, fees=fees, sold_items=sold_items, intl_est=intl_est)
    return stock_out, shipments_out, items_out, ctx


def write_csv(path: Path, rows: list):
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: '' if v is None else v for k, v in r.items()})


# Safariでは添付ファイル(/_blob/...)が読めない(別ドメインのためログインが渡らない)ので、同じCSVを
# ページと同じ置き場所(ストアの files/)にも置く。ページは添付ファイルが読めないときにこちらを使う(CEO 11/15)。
# 1文書256KBまでなので、大きい表は行で分けて決まった数のファイルにする(余った分は空)。
EMBED_PARTS = {'stock': 1, 'shipments': 1, 'ship_items': 1, 'plans': 1, 'price_trend': 2, 'ship_trend': 4, 'sales_trend': 1}
EMBED_MAX = 200_000


def write_embeds(out: Path):
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    for name, n in EMBED_PARTS.items():
        lines = (out / f'{name}.csv').read_text().splitlines(keepends=True)
        parts, cur = [], ''
        for ln in lines:
            if cur and len((cur + ln).encode()) > EMBED_MAX:
                parts.append(cur)
                cur = ''
            cur += ln
        parts.append(cur)
        if len(parts) > n:
            raise SystemExit(f'{name}.csv が埋め込み用の{n}ファイルに収まらない({len(parts)}個必要)。EMBED_PARTS とページの<script src>を増やす')
        parts += [''] * (n - len(parts))
        for i, text in enumerate(parts, 1):
            js = ('window.FS_CSV = window.FS_CSV || {};\n'
                  f'window.FS_CSV[{json.dumps(name)}] = (window.FS_CSV[{json.dumps(name)}] || []).concat([{json.dumps(text, ensure_ascii=False)}]);\n')
            (out / f'fs_data_{name}_{i}.json').write_text(
                json.dumps({'text': js, 'edit': {'via': 'claude', 'at': now}}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', default='/home/ubuntu/dashboard_out')
    ap.add_argument('--meta', default=str(DEFAULT_META))
    ap.add_argument('--fee-cache', default=str(DEFAULT_FEE_CACHE))
    ap.add_argument('--targets', default=str(DEFAULT_TARGETS))
    ap.add_argument('--today', default=datetime.now(JST).date().isoformat())
    args = ap.parse_args()
    meta = json.loads(Path(args.meta).read_text()) if Path(args.meta).exists() else {}
    fees = Fees(Path(args.fee_cache))
    tpath = Path(args.targets)
    targets = json.loads(tpath.read_text()) if tpath.exists() else {}
    n_before = len(targets)
    try:
        stock, ships, items, ctx = build(meta, fees, args.today, targets)
        import dashboard_forecast
        trend = dashboard_forecast.compute(ctx, meta)
        trend_pre = dashboard_forecast.compute(ctx, meta, pre_shipping=True)   # AWS側の定義(送料前)
    finally:
        fees.save()
    if len(targets) != n_before:   # 目標は追記だけ(一度記録した便は書き換えない)
        tpath.write_text(json.dumps(targets, ensure_ascii=False, indent=1))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / 'stock.csv', stock)
    write_csv(out / 'shipments.csv', ships)
    write_csv(out / 'ship_items.csv', items)
    for name, rows in trend.items():
        write_csv(out / f'{name}.csv', rows)
    for name in ('ship_trend', 'price_trend', 'plans'):   # 送料前の版。artifact(送料後)は読まない。AWSの画面用
        write_csv(out / f'{name}_pre.csv', trend_pre[name])
    write_embeds(out)
    print(json.dumps({'today': args.today, 'products': len(stock), 'shipments': len(ships), 'items': len(items),
                      'fee_api_calls': fees.calls, 'out': str(out)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
