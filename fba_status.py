#!/usr/bin/env python3
"""在庫・入庫の今の状況を、商品名で段階ごとにまとめる(読み取りのみ。DBも出品も変えない)。

「在庫状況」「入庫は進んでる？」「どれが買える状態？」に答えるためのもの(スキル fba-status)。
- FBAの数はAmazonの在庫API(今の値、details=true)から取る。DBの sp_fba_inventory は3時間ごとの同期なので使わない。
- 自宅・発注済み・TNK・国際輸送の数は在庫台帳(stock_ledger.stock_pipeline、AWSのDB)から取る。
- 前回の実行結果を ~/fba_status/YYYY-MM-DD.json(JST)に保存し、それより前の日の結果と比べて「前回から」を出す。
  DBのバックアップではない(在庫APIの数だけの小さなファイル)。14日より古いものは消す。
- --listings で出品の状態(BUYABLE=購入できる / DISCOVERABLE=検索に出るだけ)も見る(1SKU約1秒)。

使い方(AWSの作業ディレクトリで):
    PYTHONPATH=. .venv/bin/python fba_status.py [--listings] [--json]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import daily_report
import stock_ledger
from sp_api import client as sp_client
from sp_api.config import settings

SNAPSHOT_DIR = Path.home() / 'fba_status'
KEEP_DAYS = 14
FIELDS = ('fulfillable', 'inbound_receiving', 'transshipment', 'customer_order', 'fc_processing',
          'researching', 'research_short', 'research_mid', 'research_long', 'unfulfillable')


def fetch_inventory() -> dict:
    """SKUごとのFBA在庫の内訳(今の値)。ページ送りでも details=true を付ける
    (sp_client.get_inventory_summaries は2ページ目以降で details を落とすので使わない)。"""
    out, token = {}, None
    while True:
        params = {'granularityType': 'Marketplace', 'granularityId': settings.marketplace_id,
                  'marketplaceIds': settings.marketplace_id, 'details': 'true'}
        if token:
            params['nextToken'] = token
        data = sp_client._request('/fba/inventory/v1/summaries', params)
        for s in (data.get('payload') or {}).get('inventorySummaries') or []:
            d = s.get('inventoryDetails') or {}
            reserved = d.get('reservedQuantity') or {}
            research = d.get('researchingQuantity') or {}
            breakdown = {b.get('name'): b.get('quantity', 0) for b in research.get('researchingQuantityBreakdown') or []}
            out[s['sellerSku']] = {
                'asin': s.get('asin'),
                'fulfillable': d.get('fulfillableQuantity', 0),
                'inbound_receiving': d.get('inboundReceivingQuantity', 0),
                'transshipment': reserved.get('pendingTransshipmentQuantity', 0),
                'customer_order': reserved.get('pendingCustomerOrderQuantity', 0),
                'fc_processing': reserved.get('fcProcessingQuantity', 0),
                'researching': research.get('totalResearchingQuantity', 0),
                'research_short': breakdown.get('researchingQuantityInShortTerm', 0),
                'research_mid': breakdown.get('researchingQuantityInMidTerm', 0),
                'research_long': breakdown.get('researchingQuantityInLongTerm', 0),
                'unfulfillable': (d.get('unfulfillableQuantity') or {}).get('totalUnfulfillableQuantity', 0),
            }
        token = (data.get('pagination') or {}).get('nextToken')
        if not token:
            return out
        time.sleep(1)


def fetch_listing(sku: str) -> dict:
    time.sleep(1.0)
    path = f'/listings/2021-08-01/items/{settings.seller_id}/{urllib.parse.quote(sku, safe="")}'
    try:
        data = sp_client._request(path, {'marketplaceIds': settings.marketplace_id, 'includedData': 'summaries,offers'})
    except Exception as e:   # 404 = 出品が無い(古いSKU)
        return {'status': None, 'price': None, 'error': str(e)[:80]}
    summary = (data.get('summaries') or [{}])[0]
    prices = [o.get('price', {}).get('amount') for o in data.get('offers') or [] if o.get('price')]
    return {'status': ','.join(summary.get('status') or []) or None, 'price': float(prices[0]) if prices else None}


def _load_previous(today: str) -> tuple[str | None, dict]:
    files = sorted(p for p in SNAPSHOT_DIR.glob('*.json') if p.stem < today)
    if not files:
        return None, {}
    return files[-1].stem, json.loads(files[-1].read_text())


def _save(today: str, inventory: dict) -> None:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    (SNAPSHOT_DIR / f'{today}.json').write_text(json.dumps(inventory, ensure_ascii=False))
    for p in sorted(SNAPSHOT_DIR.glob('*.json'))[:-KEEP_DAYS]:
        p.unlink()


def build(listings: bool = False, save: bool = True) -> dict:
    today = datetime.now(stock_ledger.JST).date().isoformat()
    inventory = fetch_inventory()
    prev_day, prev = _load_previous(today)
    pipeline = stock_ledger.stock_pipeline()
    by_asin = {r['asin']: r for r in pipeline}
    names = {r['asin']: daily_report.display_name(r['name']) for r in pipeline}

    def name(asin, sku):
        found = names.get(asin)
        return found if found and found != '(名前なし)' else sku

    skus = []
    for sku, v in inventory.items():
        row = dict(v, sku=sku, name=name(v['asin'], sku),
                   prev={k: prev[sku].get(k, 0) for k in FIELDS} if sku in prev else None)
        if listings:
            row['listing'] = fetch_listing(sku)
        skus.append(row)
    skus.sort(key=lambda r: (-r['fulfillable'], r['name']))

    def pick(key):
        return [r for r in skus if r[key]]

    ledger = []
    for r in pipeline:
        parts = {k: r[k] for k in ('awaiting_supplier', 'domestic_transit', 'at_home', 'planned', 'at_tnk', 'intl_transit') if r[k]}
        if parts:
            ledger.append(dict(parts, asin=r['asin'], name=names.get(r['asin']) or r['asin'],
                               sold_30d=r['sold_30d'], days_of_cover=r['days_of_cover']))

    result = {
        'date': today, 'previous_date': prev_day,
        'sellable': pick('fulfillable'),
        'transshipment': pick('transshipment'),
        'receiving': pick('inbound_receiving'),
        'orders_waiting_stock': [r for r in skus if r['customer_order'] and not r['fulfillable']],
        'researching': pick('researching'),
        'unfulfillable': pick('unfulfillable'),
        'ledger': ledger,
        'sold_30d': {a: by_asin[a]['sold_30d'] for a in by_asin if by_asin[a]['sold_30d']},
    }
    if listings:
        result['buyable_no_stock'] = [r for r in skus if (r['listing']['status'] or '').find('BUYABLE') >= 0 and not r['fulfillable']]
        result['not_buyable'] = [r for r in skus if r['listing']['status'] and 'BUYABLE' not in r['listing']['status']]
        result['no_listing'] = [r for r in skus if not r['listing']['status']]
    if save:
        _save(today, inventory)
    return result


def _delta(row, key) -> str:
    if row['prev'] is None:
        return ''
    d = row[key] - row['prev'][key]
    return f'(前回{d:+d})' if d else ''


def format_text(res: dict) -> str:
    out = [f"== FBA在庫 {res['date']}(前回 {res['previous_date'] or 'なし'})"]
    out.append('-- 今売れる(販売可能)')
    for r in res['sellable']:
        extra = f" 出荷待ちの注文{r['customer_order']}" if r['customer_order'] else ''
        out.append(f"  {r['name']}: {r['fulfillable']}{_delta(r, 'fulfillable')}{extra}")
    for key, title in [('transshipment', '倉庫の間を移動中'), ('receiving', 'FCで受領待ち・受領中')]:
        out.append(f'-- {title}')
        for r in res[key]:
            field = 'inbound_receiving' if key == 'receiving' else key
            out.append(f"  {r['name']}: {r[field]}{_delta(r, field)}")
    if res['orders_waiting_stock']:
        out.append('-- 注文済みで、移動中・受領中の在庫から出荷を待っている')
        for r in res['orders_waiting_stock']:
            out.append(f"  {r['name']}: {r['customer_order']}件分")
    out.append('-- 調査中(短期1〜10日/中期11〜20日/長期21日〜)')
    for r in res['researching']:
        out.append(f"  {r['name']}: {r['researching']}{_delta(r, 'researching')} "
                   f"(短{r['research_short']}/中{r['research_mid']}/長{r['research_long']})")
    if res['unfulfillable']:
        out.append('-- 販売不可(破損など)')
        for r in res['unfulfillable']:
            out.append(f"  {r['name']}: {r['unfulfillable']}{_delta(r, 'unfulfillable')}")
    labels = [('intl_transit', '国際輸送'), ('at_tnk', 'TNK'), ('planned', '納品プラン済み'), ('at_home', '自宅'),
              ('domestic_transit', '仕入先出荷済み・未着'), ('awaiting_supplier', '仕入先未出荷')]
    out.append('== 台帳(FBAに着く前)')
    for key, title in labels:
        rows = [r for r in res['ledger'] if r.get(key)]
        if rows:
            out.append(f'-- {title}: ' + '、'.join(f"{r['name']} {r[key]}" for r in rows))
    if 'buyable_no_stock' in res:
        out.append('== 出品の状態')
        out.append('-- 購入できるが販売可能0(移動中・受領中から出荷): ' + '、'.join(
            f"{r['name']} ${r['listing']['price']}" for r in res['buyable_no_stock']))
        out.append('-- 検索に出るだけ(購入ボタンなし): ' + '、'.join(r['name'] for r in res['not_buyable']))
        if res['no_listing']:
            out.append('-- 出品なし(古いSKU): ' + '、'.join(f"{r['name']}({r['sku']})" for r in res['no_listing']))
    return '\n'.join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description='在庫・入庫の今の状況(読み取りのみ)')
    parser.add_argument('--listings', action='store_true', help='出品の状態(購入できるか)も見る。1SKU約1秒')
    parser.add_argument('--json', action='store_true', help='JSONで出す')
    parser.add_argument('--no-save', action='store_true', help='今回の結果を前回比較用に保存しない')
    args = parser.parse_args()
    res = build(listings=args.listings, save=not args.no_save)
    print(json.dumps(res, ensure_ascii=False, default=str) if args.json else format_text(res))


if __name__ == '__main__':
    main()
