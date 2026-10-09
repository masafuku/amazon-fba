#!/usr/bin/env python3
"""在庫台帳。メールをイベントとして取り込み、ASINごとの在庫パイプラインを返す。

パイプライン: SD注文 -> 仕入先出荷 -> 自宅着 -> FBA納品プラン(TNK入庫/国際輸送) -> FBA在庫 -> 販売
- 仕入れ側は jp_purchase_records(SD受付番号単位)に状態列を持たせる
- FBA側は SP-API同期済みの sp_inbound_shipment(_items)/sp_fba_inventory/sp_order_items
- TNK・Amazonのメールは ops_events にだけ記録し、集計時に参照する
メールが来ないのは自宅着だけなので mark_received() で口頭報告を登録する。

使い方(AWS上で実行。DBはAWSが正本):
    stock_ledger.py ingest < emails.json   # [{id, subject, sender, date, body}, ...]
    stock_ledger.py stock [--json]
    stock_ledger.py received --tracking 140418920994 [--on 2026-10-10]
    stock_ledger.py map-sd 15913004S3 B0CBDKXTD2
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import ops_finance as of
import sd_email_parser as sdp

JST = timezone(timedelta(hours=9))

SD_ORDERED = 'sd_ordered'
SD_SHIP_SCHEDULED = 'sd_ship_scheduled'
SD_SHIPPED = 'sd_shipped'
HOME_RECEIVED = 'home_received'
TNK_ARRIVED = 'tnk_arrived'
TNK_SHIPPED = 'tnk_shipped'
BRAND_APPROVED = 'brand_approved'
BRAND_REJECTED = 'brand_rejected'
BRAND_PENDING = 'brand_pending'
LISTING_CREATED = 'listing_created'

# SP-APIのshipment statusのうち「FCが受け取った」もの
FC_RECEIVED_STATUSES = {'DELIVERED', 'CHECKED_IN', 'RECEIVING', 'CLOSED'}

ITEM_BLOCK_SPLIT = re.compile(r'(?=\[\s*発注日\s*\])')
SD_FIELDS = {
    'orderDate': r'発注日\s*\]\s*([0-9]{4})/([0-9]{2})/([0-9]{2})',
    'sdReceptionNo': r'受付番号\s*\]\s*([0-9]+)',
    'sdProductNo': r'SD品番\s*\]\s*(\S+)',
    'productName': r'商品名\s*\]\s*(.+)',
    'janCode': r'JANコード\s*\][ \t　]*([0-9]+)',
    'variant': r'内訳\s*\]\s*(.+)',
    'quantity': r'注文点数\s*\]\s*([0-9,]+)点',
    'unitPriceJpy': r'注文単価\s*\]\s*[\\¥]?([0-9,]+)',
    'amountJpy': r'注文(?:金額|合計)\s*\]\s*[\\¥]?([0-9,]+)',
    'expectedShipDate': r'出荷予定日\s*\]\s*([0-9]{4})/([0-9]{2})/([0-9]{2})',
}
SUPPLIER_NAME = re.compile(r'出展企業名\s*[：:]\s*(.+)')
CARRIER = re.compile(r'■配送業者\s*\n\s*(\S+)')
TRACKING = re.compile(r'■送り状番号\s*\n\s*([0-9-]+)')
ACTUAL_SHIPPING = re.compile(r'送\s*料\s*\]\s*[\\¥]?([0-9,]+)')
COUPON = re.compile(r'クーポン利用\s*\]\s*(-?[0-9,]+)')
TNK_FIELD = re.compile(r'^\s*([^:\n]+?)\s*:\s*(.+?)\s*$', re.MULTILINE)
BRAND_SUBJECT = re.compile(r'Brand Approval Request for (.+?)\s*$')
LISTING_SUBJECT = re.compile(r'Amazon Listing Created - (\S+)')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jst_date(iso: str | None) -> str | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST).date().isoformat()


def _num(value):
    return None if value is None else float(value.replace(',', ''))


def classify(subject: str) -> str | None:
    subject = subject or ''
    if 'ご注文内容控え' in subject:
        return SD_ORDERED
    if '出荷予定日のご連絡' in subject:
        return SD_SHIP_SCHEDULED
    if '出荷完了いたしました' in subject:
        return SD_SHIPPED
    if 'TNK Logistics' in subject and '到着しました' in subject:
        return TNK_ARRIVED
    if 'TNK Logistics' in subject and '発送しました' in subject:
        return TNK_SHIPPED
    if BRAND_SUBJECT.search(subject):
        return 'brand'
    if LISTING_SUBJECT.search(subject):
        return LISTING_CREATED
    return None


def parse_sd_items(body: str) -> list:
    """出荷予定日・出荷完了メールの商品行。各行は[発注日]から始まる。"""
    supplier_match = SUPPLIER_NAME.search(body)
    supplier = supplier_match.group(1).strip() if supplier_match else None
    items = []
    for block in ITEM_BLOCK_SPLIT.split(body):
        if '受付番号' not in block:
            continue
        fields = {}
        for key, pattern in SD_FIELDS.items():
            m = re.search(pattern, block)
            if not m:
                fields[key] = None
            elif key in ('orderDate', 'expectedShipDate'):
                fields[key] = '-'.join(m.groups())
            else:
                fields[key] = m.group(1).strip()
        items.append({
            'orderDate': fields['orderDate'],
            'sdReceptionNo': fields['sdReceptionNo'],
            'supplierName': supplier,
            'sdProductNo': fields['sdProductNo'],
            'productName': fields['productName'],
            'janCode': fields['janCode'],
            'variant': fields['variant'],
            'unitPriceJpy': _num(fields['unitPriceJpy']),
            'quantity': int(_num(fields['quantity'])) if fields['quantity'] else None,
            'amountJpy': _num(fields['amountJpy']),
            'expectedShipDate': fields['expectedShipDate'],
        })
    return items


def parse_sd_shipment(body: str) -> dict:
    carrier = CARRIER.search(body)
    tracking = TRACKING.search(body)
    shipping = ACTUAL_SHIPPING.search(body)
    coupon = COUPON.search(body)
    net_shipping = max((_num(shipping.group(1)) if shipping else 0.0) - (abs(_num(coupon.group(1))) if coupon else 0.0), 0.0)
    return {
        'carrier': carrier.group(1) if carrier else None,
        'trackingNo': tracking.group(1).replace('-', '') if tracking else None,
        'netShippingJpy': net_shipping,
    }


def parse_tnk(body: str) -> dict:
    fields = dict(TNK_FIELD.findall(body))

    def get(label):
        return fields.get(label)

    def kg(label):
        value = get(label)
        return float(value.replace('kg', '').strip()) if value else None

    return {
        'fbaShipmentId': get('FBAシップメントID'),
        'date': get('受付日') or get('発送日'),
        'boxes': int(get('荷受けした箱数') or get('発送した箱数') or 0) or None,
        'fc': get('宛先FC名'),
        'plannedShipDate': get('発送予定日'),
        'volumetricKg': kg('容積重量'),
        'actualKg': kg('実重量'),
        'courier': get('クーリエ'),
        'service': get('配送方法'),
        'trackingNo': get('追跡番号'),
    }


def parse_brand(subject: str, body: str) -> tuple[str, str]:
    brand = BRAND_SUBJECT.search(subject).group(1).strip()
    lowered = (body or '').lower()
    if 'not eligible' in lowered:
        return brand, BRAND_REJECTED
    if 'approved your application' in lowered or 'you are now approved' in lowered:
        return brand, BRAND_APPROVED
    return brand, BRAND_PENDING


def _record_event(conn, source_id, event_type, occurred_on, ref, detail) -> bool:
    cur = conn.execute(
        '''INSERT OR IGNORE INTO ops_events (source_id, event_type, occurred_on, ref, detail, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (source_id, event_type, occurred_on, ref, json.dumps(detail, ensure_ascii=False), _now()),
    )
    return cur.rowcount == 1


def _resolve_asins(conn) -> None:
    conn.execute(
        '''UPDATE jp_purchase_records SET asin = (
               SELECT asin FROM sd_product_asin_map m WHERE m.sd_product_no = jp_purchase_records.sd_product_no)
           WHERE asin IS NULL AND sd_product_no IN (SELECT sd_product_no FROM sd_product_asin_map)'''
    )


def _upsert_purchases(records: list) -> None:
    for record in records:
        of.upsert_jp_purchase_record({k: record.get(k) for k in (
            'orderDate', 'sdReceptionNo', 'supplierName', 'sdProductNo', 'productName',
            'janCode', 'variant', 'unitPriceJpy', 'quantity', 'amountJpy')})


def ingest_email(email: dict) -> list:
    """email: {id, subject, sender, date(ISO), body}。新しく記録したイベントの説明を返す。"""
    of.init_ops_tables()
    kind = classify(email.get('subject'))
    if not kind:
        return []
    body = email.get('body') or ''
    source_id = email['id']
    received_on = _jst_date(email.get('date'))
    new_events = []

    if kind == SD_ORDERED:
        order_date = sdp.extract_order_date(body) or received_on
        records = sdp.parse_email_body(body, order_date=order_date)
        _upsert_purchases(records)
        for (supplier, net) in {(r['supplierName'], r['netShippingJpy']) for r in records}:
            if net and net > 0:
                of.allocate_order_shipping(supplier, order_date, net)
        with sqlite3.connect(of.DB_PATH) as conn:
            for r in records:
                if _record_event(conn, source_id, SD_ORDERED, order_date, r['sdReceptionNo'], {'qty': r['quantity']}):
                    new_events.append(f"注文 {r['supplierName']} {r['productName']} {r.get('variant') or ''} ×{r['quantity']}")
            _resolve_asins(conn)

    elif kind in (SD_SHIP_SCHEDULED, SD_SHIPPED):
        items = parse_sd_items(body)
        _upsert_purchases(items)
        shipment = parse_sd_shipment(body) if kind == SD_SHIPPED else {}
        with sqlite3.connect(of.DB_PATH) as conn:
            for item in items:
                if kind == SD_SHIP_SCHEDULED:
                    conn.execute('UPDATE jp_purchase_records SET expected_ship_date = ? WHERE sd_reception_no = ?',
                                 (item['expectedShipDate'], item['sdReceptionNo']))
                    occurred = item['expectedShipDate']
                    label = f"出荷予定 {item['expectedShipDate']}"
                else:
                    conn.execute(
                        '''UPDATE jp_purchase_records SET supplier_shipped_at = ?, carrier = ?, tracking_no = ?
                           WHERE sd_reception_no = ?''',
                        (received_on, shipment['carrier'], shipment['trackingNo'], item['sdReceptionNo']),
                    )
                    occurred = received_on
                    label = f"仕入先出荷 {shipment['carrier']} {shipment['trackingNo']}"
                if _record_event(conn, source_id, kind, occurred, item['sdReceptionNo'], {'qty': item['quantity'], **shipment}):
                    new_events.append(f"{label} {item['supplierName']} {item['productName']} {item['variant'] or ''} ×{item['quantity']}")
            _resolve_asins(conn)
        if shipment.get('netShippingJpy'):
            for (supplier, order_date) in {(i['supplierName'], i['orderDate']) for i in items}:
                of.allocate_order_shipping(supplier, order_date, shipment['netShippingJpy'])

    elif kind in (TNK_ARRIVED, TNK_SHIPPED):
        info = parse_tnk(body)
        with sqlite3.connect(of.DB_PATH) as conn:
            if _record_event(conn, source_id, kind, info['date'] or received_on, info['fbaShipmentId'], info):
                verb = 'TNK入庫' if kind == TNK_ARRIVED else f"TNK発送 {info['courier']} {info['trackingNo']} 実重量{info['actualKg']}kg"
                new_events.append(f"{verb} {info['fbaShipmentId']} → {info['fc']}")

    elif kind == 'brand':
        brand, event_type = parse_brand(email['subject'], body)
        with sqlite3.connect(of.DB_PATH) as conn:
            if _record_event(conn, source_id, event_type, received_on, brand, {'subject': email['subject']}):
                new_events.append({BRAND_APPROVED: 'ブランド承認', BRAND_REJECTED: 'ブランド却下', BRAND_PENDING: 'ブランド審査中'}[event_type] + f' {brand}')

    elif kind == LISTING_CREATED:
        sku = LISTING_SUBJECT.search(email['subject']).group(1)
        with sqlite3.connect(of.DB_PATH) as conn:
            if _record_event(conn, source_id, LISTING_CREATED, received_on, sku, {}):
                new_events.append(f'出品作成 {sku}')

    return new_events


def mark_received(*, tracking_no: str | None = None, reception_nos: list | None = None, on: str | None = None,
                  undo: bool = False) -> int:
    """自宅着の口頭報告を登録する(undo=Trueで取り消し)。送り状番号か受付番号で指定。"""
    of.init_ops_tables()
    on = on or datetime.now(JST).date().isoformat()
    with sqlite3.connect(of.DB_PATH) as conn:
        if tracking_no:
            rows = conn.execute('SELECT sd_reception_no FROM jp_purchase_records WHERE tracking_no = ?',
                                (tracking_no.replace('-', ''),)).fetchall()
        else:
            rows = [(r,) for r in reception_nos or []]
        for (reception_no,) in rows:
            conn.execute('UPDATE jp_purchase_records SET received_at = ? WHERE sd_reception_no = ?',
                         (None if undo else on, reception_no))
            if undo:
                conn.execute('DELETE FROM ops_events WHERE event_type = ? AND ref = ?', (HOME_RECEIVED, reception_no))
            else:
                _record_event(conn, f'manual:{_now()}', HOME_RECEIVED, on, reception_no, {'tracking_no': tracking_no})
    return len(rows)


def set_sd_product_asin(sd_product_no: str, asin: str) -> None:
    of.init_ops_tables()
    with sqlite3.connect(of.DB_PATH) as conn:
        conn.execute('''INSERT INTO sd_product_asin_map (sd_product_no, asin) VALUES (?, ?)
                        ON CONFLICT(sd_product_no) DO UPDATE SET asin = excluded.asin''', (sd_product_no, asin))
        _resolve_asins(conn)


def _shipment_stage(conn, confirmation_id: str | None, sp_status: str | None) -> str:
    if (sp_status or '').upper() in FC_RECEIVED_STATUSES:
        return 'fc_received'
    events = {row[0] for row in conn.execute('SELECT event_type FROM ops_events WHERE ref = ?', (confirmation_id,))}
    if TNK_SHIPPED in events:
        return 'intl_transit'
    if TNK_ARRIVED in events:
        return 'at_tnk'
    return 'planned'


def stock_pipeline(today: str | None = None) -> list:
    """ASINごとの在庫パイプライン。数量の意味:
    awaiting_supplier=未出荷 / domestic_transit=仕入先出荷済み・自宅未着 / at_home=自宅にあり納品プラン未割当 /
    planned=納品プラン済み・TNK未着 / at_tnk / intl_transit / fba_available=FBA販売可能 / sold_30d"""
    of.init_ops_tables()
    today_date = datetime.fromisoformat(today).date() if today else datetime.now(JST).date()
    since = (today_date - timedelta(days=30)).isoformat()
    rows = {}

    def row_for(asin, name=None):
        if asin not in rows:
            rows[asin] = {'asin': asin, 'name': name, 'awaiting_supplier': 0, 'domestic_transit': 0, 'received': 0,
                          'planned': 0, 'at_tnk': 0, 'intl_transit': 0, 'fc_received_inbound': 0,
                          'fba_available': 0, 'sold_30d': 0, 'shipments': []}
        if name and not rows[asin]['name']:
            rows[asin]['name'] = name
        return rows[asin]

    with sqlite3.connect(of.DB_PATH) as conn:
        for asin, sd_no, name, variant, qty, shipped, received in conn.execute(
            '''SELECT asin, sd_product_no, product_name, variant, quantity, supplier_shipped_at, received_at
               FROM jp_purchase_records ORDER BY order_date DESC'''
        ):
            label = ' '.join(x for x in (name, variant) if x)
            row = row_for(asin or f'(未紐付け:{sd_no})', label)
            if received:
                row['received'] += qty or 0
            elif shipped:
                row['domestic_transit'] += qty or 0
            else:
                row['awaiting_supplier'] += qty or 0

        for confirmation_id, sp_status, asin, sku, qty in conn.execute(
            '''SELECT s.shipment_confirmation_id, s.status, i.asin, i.sku, i.quantity
               FROM sp_inbound_shipment_items i JOIN sp_inbound_shipments s ON s.shipment_id = i.shipment_id'''
        ):
            stage = _shipment_stage(conn, confirmation_id, sp_status)
            row = row_for(asin, sku)
            row['fc_received_inbound' if stage == 'fc_received' else stage] += qty or 0
            row['shipments'].append({'id': confirmation_id, 'stage': stage, 'qty': qty})

        for asin, sku, qty in conn.execute('SELECT asin, sku, fulfillable_quantity FROM sp_fba_inventory'):
            row_for(asin, sku)['fba_available'] = qty or 0

        for asin, qty in conn.execute(
            '''SELECT i.asin, SUM(i.quantity) FROM sp_order_items i JOIN sp_orders o ON o.order_id = i.order_id
               WHERE o.purchase_date >= ? AND IFNULL(o.order_status, '') != 'Canceled' GROUP BY i.asin''', (since,)
        ):
            row_for(asin)['sold_30d'] = qty or 0

    result = []
    for row in rows.values():
        sent = row['planned'] + row['at_tnk'] + row['intl_transit'] + row['fc_received_inbound']
        row['at_home'] = max(row['received'] - sent, 0)
        pipeline_total = row['at_home'] + row['planned'] + row['at_tnk'] + row['intl_transit'] + row['fba_available']
        daily = row['sold_30d'] / 30
        row['days_of_cover'] = round(pipeline_total / daily, 1) if daily else None
        result.append(row)
    result.sort(key=lambda r: (r['days_of_cover'] is None, r['days_of_cover'] or 0, r['asin']))
    return result


def format_pipeline(rows: list) -> str:
    columns = [('awaiting_supplier', '未出荷'), ('domestic_transit', '国内輸送'), ('at_home', '自宅'),
               ('planned', '納品プラン'), ('at_tnk', 'TNK'), ('intl_transit', '国際輸送'),
               ('fba_available', 'FBA在庫'), ('sold_30d', '30日販売')]
    lines = []
    for row in rows:
        parts = [f'{label}{row[key]}' for key, label in columns if row[key]]
        cover = f" 在庫日数{row['days_of_cover']}" if row['days_of_cover'] is not None else ''
        lines.append(f"{row['asin']} {row['name'] or ''}: {' / '.join(parts) or '在庫なし'}{cover}")
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description='在庫台帳')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('ingest', help='標準入力のメールJSON配列を取り込む')
    stock = sub.add_parser('stock')
    stock.add_argument('--json', action='store_true')
    received = sub.add_parser('received')
    received.add_argument('--tracking')
    received.add_argument('--reception', nargs='*')
    received.add_argument('--on')
    received.add_argument('--undo', action='store_true')
    mapping = sub.add_parser('map-sd')
    mapping.add_argument('sd_product_no')
    mapping.add_argument('asin')
    args = parser.parse_args()

    if args.command == 'ingest':
        for email in json.load(sys.stdin):
            for line in ingest_email(email):
                print(line)
    elif args.command == 'stock':
        rows = stock_pipeline()
        print(json.dumps(rows, ensure_ascii=False, indent=1) if args.json else format_pipeline(rows))
    elif args.command == 'received':
        count = mark_received(tracking_no=args.tracking, reception_nos=args.reception, on=args.on, undo=args.undo)
        print(f"{count}行の自宅着を{'取り消しました' if args.undo else '登録しました'}")
    elif args.command == 'map-sd':
        set_sd_product_asin(args.sd_product_no, args.asin)
        print('ok')


if __name__ == '__main__':
    main()
