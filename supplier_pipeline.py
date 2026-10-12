#!/usr/bin/env python3
"""サプライヤー起点の候補検索(SD業者の商品 → Amazon.co.jp ASIN → Keepa → 候補登録)の後半処理。

ブラウザで行う前半(SD商品ページの品番・卸価格、Amazon.co.jpのASIN特定、Keepa製品ビューアのCSV出力)は
.claude/skills/supplier-search/SKILL.md の手順。ここでは、そのCSVから先を既存の自動判定と
同じ計算式・同じ保存経路(ops_finance)で処理する:

- 利益・ROI: ops_finance.calc_unit_profit(送料0円・関税12.5%・ROI=利益÷卸原価)をそのまま使う。
- 卸価格の税基準: ops_finance.normalize_jp_cost_for_tax で揃える(免税事業者の間は税込=SDの税抜価格×1.1)。
- Tier: ops_finance._apply_priority_fields(S/A+/…/D、完全除外の判定も同じ)。
- 保存: ops_finance.persist_agent_run(source_type='supplier')。既存ASINは調査の前に除外する。

使い方:
    supplier_pipeline.py existing --out existing.json        # AWSで実行。調査前に既存ASINを取り出す
    supplier_pipeline.py evaluate --map map.csv --keepa keepa.csv --supplier 丸進 \\
        --existing existing.json --out evaluation.json       # ローカルでもAWSでも可(DBは読まない)
    supplier_pipeline.py register --evaluation evaluation.json [--apply]   # AWSで実行。既定は予行演習
    supplier_pipeline.py refresh --keepa a.csv b.csv --asins asins.txt [--apply]  # AWSで実行。既存行を最新Keepaで再計算(原価はそのまま)\n    supplier_pipeline.py audit [--apply]                     # AWSで実行。旧方式の supplier 行を公式式で再計算(既定は報告のみ、--applyで反映)

map.csv の列: sd_id, part_no, wholesale_jpy(SDの税抜卸価格/点), asin, name  (任意: jan。あればKeepaのEANと照合し、不一致は足切り)
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import ops_finance as of

SANITY_MAX_SALES_RANK = 500_000      # これより悪い順位(または順位なし)は、実需の根拠がなく価格も信用できない
SANITY_MAX_PRICE_RATIO = 10.0        # US価格(円換算)÷卸原価がこれを超え、月間販売の実数値も無いものは相場の異常値とみなす
DEFAULT_RATE = 150.0
SUPPLIER_CATEGORY_SUFFIX = ' supplier-search'
# ops_finance._excluded_kind が拾えない英語タイトルの刃物(Cutter / Utility Knife など)。
# 刃物は完全除外のカテゴリ。コーナーカッター(紙の角を丸める工具)のような刃のない文具は除外しない。
_BLADE_TITLE = re.compile(r'\b(box cutter|cardboard cutter|utility knife|cutter knife|knife|scissors|shears?|blade)\b', re.IGNORECASE)
_NOT_BLADE_TITLE = re.compile(r'corner cutter|replacement blade for (?:eraser|tape)', re.IGNORECASE)


def _num(value) -> float | None:
    text = str(value or '').replace('$', '').replace('%', '').replace(',', '').strip()
    try:
        return float(text)
    except ValueError:
        return None


def load_keepa_csv(path: str | Path) -> dict:
    """Keepa製品ビューアのCSVを、ASIN → 行 の辞書にする。"""
    with open(path, encoding='utf-8-sig', newline='') as f:
        return {row['ASIN']: row for row in csv.DictReader(f) if row.get('ASIN')}


def load_map(path: str | Path) -> list[dict]:
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = []
        for row in csv.DictReader(f):
            row['wholesale_jpy'] = float(row['wholesale_jpy'])
            rows.append(row)
        return rows


def load_existing(db_path: str | Path | None = None) -> dict:
    """AWS DB(正本)の既存候補。ASINごとの最新行の Tier / 除外種別 を返す。"""
    of.init_ops_tables()
    with sqlite3.connect(db_path or of.DB_PATH) as conn:
        rows = conn.execute(
            '''SELECT asin, priority_tier, excluded_kind, source_type FROM agent_candidates a
               WHERE id = (SELECT MAX(id) FROM agent_candidates WHERE asin = a.asin)''').fetchall()
    return {asin: {'tier': tier, 'excluded_kind': kind, 'source_type': source}
            for asin, tier, kind, source in rows}


def market_sanity_flags(us_price_usd: float | None, sales_rank: float | None, jp_cost_jpy: float,
                        monthly_sold: float | None, rate: float = DEFAULT_RATE) -> list[str]:
    """ROIを計算する前の足切り。実需のない出品の異常価格(順位が無い・50万位以下、US価格が卸の10倍超など)
    を拾うと、ROIが数百〜1,000%超に見えてTierだけがDで残り、候補一覧を汚すため。
    戻り値のどれかがあれば登録しない(報告には載せる)。"""
    flags = []
    if not us_price_usd:
        flags.append('no_price')
        return flags
    if sales_rank is None:
        flags.append('no_rank')
    elif sales_rank > SANITY_MAX_SALES_RANK:
        flags.append('rank_over_500k')
    if monthly_sold is None and jp_cost_jpy and us_price_usd * rate / jp_cost_jpy > SANITY_MAX_PRICE_RATIO:
        flags.append('price_outlier')
    return flags


def evidence_kind(monthly_sold, rank_drops_30) -> str:
    """Tierの根拠が実数値か推定か。月間販売の実数値が無くランク変動だけならB+/B-は『推定』扱い。"""
    if monthly_sold is not None:
        return 'monthly_sold'
    if rank_drops_30 is not None and rank_drops_30 >= of.PRIORITY_MIN_RANK_DROPS_30:
        return 'rank_drops_estimate'
    return 'none'


def build_entry(map_row: dict, keepa_row: dict, supplier: str, rate: float = DEFAULT_RATE) -> dict | None:
    """1商品ぶんの候補entryを作る。足切りに掛かったら {'screened_out': [...], ...} を返す。"""
    asin = map_row['asin']
    us_price = _num(keepa_row.get('Buy Box: 現在')) or _num(keepa_row.get('Buy Box: 90 日平均'))
    sales_rank = _num(keepa_row.get('売れ筋ランキング: 現在'))
    drops_90 = _num(keepa_row.get('売れ筋ランキング: 過去90日間の減少'))
    rank_drops_30 = round(drops_90 / 3, 1) if drops_90 is not None else None
    monthly = _num(keepa_row.get('月間売上トレンド: 先月の購入'))
    monthly_sold = int(monthly) if monthly is not None else None
    wholesale_raw = float(map_row['wholesale_jpy'])
    jp_cost = of.normalize_jp_cost_for_tax(wholesale_raw, None)

    base = {'asin': asin, 'title': map_row.get('name') or keepa_row.get('商品名'), 'supplier': supplier, 'part_no': map_row.get('part_no'),
            'sd_id': map_row.get('sd_id'), 'wholesale_raw_jpy': wholesale_raw, 'jp_cost_jpy': jp_cost,
            'us_price_usd': us_price, 'sales_rank': int(sales_rank) if sales_rank is not None else None,
            'monthly_sold': monthly_sold, 'sales_rank_drops_30': rank_drops_30}
    flags = market_sanity_flags(us_price, sales_rank, jp_cost, monthly_sold, rate)
    jan, keepa_ean = (map_row.get('jan') or '').strip(), keepa_row.get('商品コード: EAN') or ''
    if jan and keepa_ean and jan not in keepa_ean:
        flags.append('ean_mismatch')   # JAN検索で別商品(まとめ買い等)を拾った可能性。SDの単品卸価格と合わない
    if flags:
        return {**base, 'screened_out': flags}

    weight_g = _num(keepa_row.get('パッケージ: 重さ (g)'))
    referral_pct = _num(keepa_row.get('紹介料％'))
    fba_fee = _num(keepa_row.get('FBA Pick&Pack 料金'))
    kwargs = {}
    if referral_pct is not None:
        kwargs['amazon_fee_rate'] = referral_pct / 100
    if fba_fee is not None:
        kwargs['fba_fee_usd'] = fba_fee
    weight_kg = weight_g / 1000 if weight_g else of.DEFAULT_WEIGHT_KG_FALLBACK
    profit = of.calc_unit_profit(us_price_usd=us_price, jp_cost_jpy=jp_cost, weight_kg=weight_kg,
                                 exchange_rate=rate, **kwargs)
    entry = {
        **base, **profit,
        'url': f'https://www.amazon.com/dp/{asin}',
        'jp_url': f'https://www.superdelivery.com/p/r/pd_p/{map_row.get("sd_id")}/' if map_row.get('sd_id') else None,
        'weight_kg': weight_kg,
        'weight_estimated': not weight_g,
        'fee_estimated': referral_pct is None or fba_fee is None,
        'competitor_seller_count': _num(keepa_row.get('新品アイテム数 FBA: 現在')),
        'ean': keepa_row.get('商品コード: EAN') or None,
        'evidence': evidence_kind(monthly_sold, rank_drops_30),
        'wholesale_cost_jpy': jp_cost,
        'source': f'supplier-search/{supplier}',
    }
    of._apply_priority_fields(entry)
    title = entry.get('title') or ''
    if not entry.get('excluded_kind') and _BLADE_TITLE.search(title) and not _NOT_BLADE_TITLE.search(title):
        entry['excluded_kind'] = 'knife'
        entry['priority_tier'] = None
    return entry


def evaluate(map_rows: list[dict], keepa: dict, supplier: str, existing: dict | None = None,
             rate: float = DEFAULT_RATE) -> dict:
    """map_rows × Keepa CSV を判定する。既存ASINは登録せず、既存のTierとの差を diffs に出す(上書きしない)。"""
    existing = existing or {}
    result = {'qualified': [], 'rejected': [], 'screened_out': [], 'skipped_existing': [],
              'diffs': [], 'no_keepa': []}
    for map_row in map_rows:
        asin = map_row['asin']
        keepa_row = keepa.get(asin)
        if keepa_row is None:
            result['no_keepa'].append(asin)  # USに存在しない/読み込めなかった
            continue
        entry = build_entry(map_row, keepa_row, supplier, rate)
        if 'screened_out' in entry:
            result['screened_out'].append(entry)
        elif asin in existing:
            result['skipped_existing'].append(asin)
            if entry['priority_tier'] != existing[asin]['tier']:
                result['diffs'].append({'asin': asin, 'title': entry['title'], 'existing_tier': existing[asin]['tier'],
                                        'new_tier': entry['priority_tier'], 'new_roi_pct': entry['roi_pct']})
        elif of.is_qualified_priority_tier(entry['priority_tier']):
            result['qualified'].append(entry)
        else:
            entry['reason'] = of._priority_rejection_reason(entry)
            result['rejected'].append(entry)
    result['qualified'].sort(key=of._qualified_sort_key)
    return result


def register(evaluation: dict, supplier: str, apply: bool = False, include_rejected: bool = False) -> dict:
    """合格候補を agent_candidates に保存する(既定は予行演習)。不合格は include_rejected のときだけ。"""
    to_save = {'qualified': evaluation['qualified'], 'rejected': evaluation['rejected'] if include_rejected else []}
    count = len(to_save['qualified']) + len(to_save['rejected'])
    if apply and count:
        run_id = of.persist_agent_run(f'{supplier}{SUPPLIER_CATEGORY_SUFFIX}', to_save, source_type='supplier')
    else:
        run_id = None
    return {'dry_run': not apply, 'count': count, 'run_id': run_id}


def audit_supplier_rows(db_path: str | Path | None = None) -> list[dict]:
    """旧方式(税抜の卸価格・独自のROI式)で登録された supplier 行を、公式式(税込・calc_unit_profit)で
    再計算して、Tierが変わるものを一覧にする。DBは書き換えない(食い違いの黙っての修正はしない方針)。"""
    with sqlite3.connect(db_path or of.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            '''SELECT id, asin, title, us_price_usd, jp_cost_jpy, weight_kg, monthly_sold, priority_tier, data_json
               FROM agent_candidates WHERE source_type = 'supplier' ORDER BY id''').fetchall()
    out = []
    for r in rows:
        try:
            data = json.loads(r['data_json'] or '{}')
        except ValueError:
            continue
        if data.get('wholesale_raw_jpy') is not None:
            continue  # 新方式(税込・公式式)で登録済み
        raw = data.get('jp_cost') or r['jp_cost_jpy']
        if not raw or not r['us_price_usd']:
            continue
        monthly = r['monthly_sold'] if r['monthly_sold'] is not None else data.get('monthly_sold')
        if data.get('ref_pct') is None or data.get('fba_fee') is None or (
                monthly is None and data.get('rank_drops_30') is None):
            # 手数料や実売の根拠が残っていない行は、既定値や「実売なし」で計算すると見かけの変化が出るので
            # 判定不能として報告する
            out.append({'id': r['id'], 'asin': r['asin'], 'title': r['title'], 'tier_before': r['priority_tier'],
                        'tier_after': None, 'roi_before': data.get('roi_pct'), 'roi_after': None, 'jp_cost_after': None})
            continue
        cost = of.normalize_jp_cost_for_tax(raw, None)
        profit = of.calc_unit_profit(
            us_price_usd=r['us_price_usd'], jp_cost_jpy=cost, weight_kg=r['weight_kg'] or of.DEFAULT_WEIGHT_KG_FALLBACK,
            amazon_fee_rate=data['ref_pct'], fba_fee_usd=data['fba_fee'])
        tier = of._classify_priority_tier(profit['roi_pct'], monthly, data.get('rank_drops_30'))
        out.append({'id': r['id'], 'asin': r['asin'], 'title': r['title'], 'tier_before': r['priority_tier'],
                    'tier_after': tier, 'roi_before': data.get('roi_pct'), 'roi_after': profit['roi_pct'],
                    'jp_cost_after': round(cost, 1)})
    return out


def apply_audit(rows: list[dict], db_path: str | Path | None = None) -> int:
    """audit_supplier_rows() の結果のうち判定可能な行を、公式式の値でDBに反映する。
    更新前の値は data_json['audit_before'] に残す。wholesale_raw_jpy を立てるので、再実行しても二重に更新されない。"""
    updated = 0
    with sqlite3.connect(db_path or of.DB_PATH) as conn:
        for r in rows:
            if r['tier_after'] is None:
                continue
            row = conn.execute('SELECT us_price_usd, data_json, jp_cost_jpy, priority_tier, margin_pct, unit_profit_usd '
                               'FROM agent_candidates WHERE id = ?', (r['id'],)).fetchone()
            data = json.loads(row[1] or '{}')
            raw = data.get('jp_cost') or row[2]
            profit = of.calc_unit_profit(
                us_price_usd=row[0], jp_cost_jpy=r['jp_cost_after'], weight_kg=data.get('weight_kg') or of.DEFAULT_WEIGHT_KG_FALLBACK,
                amazon_fee_rate=data['ref_pct'], fba_fee_usd=data['fba_fee'])
            data['audit_before'] = {'jp_cost_jpy': row[2], 'priority_tier': row[3], 'roi_pct': data.get('roi_pct'),
                                    'unit_profit_usd': row[5], 'margin_pct': row[4]}
            data.update(wholesale_raw_jpy=raw, roi_pct=profit['roi_pct'], unit_profit_jpy=round(profit['unit_profit_usd'] * DEFAULT_RATE))
            conn.execute(
                '''UPDATE agent_candidates SET jp_cost_jpy=?, unit_profit_usd=?, margin_pct=?, priority_tier=?, qualified=?, data_json=?
                   WHERE id=?''',
                (r['jp_cost_after'], profit['unit_profit_usd'], profit['margin_pct'], r['tier_after'],
                 1 if of.is_qualified_priority_tier(r['tier_after']) else 0, json.dumps(data, ensure_ascii=False), r['id']))
            updated += 1
    return updated


def add_restricted_supplier(asin: str, source: str, shop_name: str, price_jpy: float, note: str,
                            url: str | None = None, min_qty: int | None = None, db_path: str | Path | None = None) -> int:
    """US向けには使えない仕入れ先(国内販売のみ・海外販売×など)を、参考情報として data_json['manual_suppliers'] に
    記録する。ops_finance.add_manual_supplier と違い、卸原価・利益・Tierは変えない(使えない価格で
    ROIが良く見えるのを防ぐ)。restricted=True の印を付ける。同じsource+shop_nameは置き換える。更新した行数を返す。"""
    now = datetime.now(timezone.utc).isoformat()
    entry = {'source': source, 'shop_name': shop_name, 'url': url, 'price_jpy': float(price_jpy), 'min_qty': min_qty,
             'note': note, 'checked_at': now, 'restricted': True}
    updated = 0
    with sqlite3.connect(db_path or of.DB_PATH, timeout=30) as conn:
        for row_id, data_json in conn.execute('SELECT id, data_json FROM agent_candidates WHERE asin = ?', (asin,)).fetchall():
            data = json.loads(data_json or '{}')
            data['manual_suppliers'] = [x for x in (data.get('manual_suppliers') or [])
                                        if not (x.get('source') == source and x.get('shop_name') == shop_name)] + [entry]
            conn.execute('UPDATE agent_candidates SET data_json = ? WHERE id = ?', (json.dumps(data, ensure_ascii=False), row_id))
            updated += 1
    return updated


def refresh_rows(asins, keepa: dict, db_path: str | Path | None = None, apply: bool = False, rate: float = DEFAULT_RATE) -> list[dict]:
    """既存候補(ASINごとの最新行)を、その行の卸原価はそのままに、最新のKeepa(価格・手数料・月間販売・順位)と
    公式式で再計算する。Tierの食い違い(evaluateのdiffs)を解消する用。applyでDBを更新し、更新前の値は
    data_json['refresh_before'] に残す。Keepaに無い/価格が付かないASINは触らず結果に理由を出す。"""
    results = []
    with sqlite3.connect(db_path or of.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for asin in asins:
            row = conn.execute('SELECT * FROM agent_candidates WHERE asin = ? ORDER BY id DESC LIMIT 1', (asin,)).fetchone()
            k = keepa.get(asin)
            if row is None or k is None or not row['jp_cost_jpy']:
                results.append({'asin': asin, 'status': 'skip', 'reason': 'no row/keepa/cost'})
                continue
            price = _num(k.get('Buy Box: 現在')) or _num(k.get('Buy Box: 90 日平均'))
            if not price:
                results.append({'asin': asin, 'status': 'skip', 'reason': 'no US price'})
                continue
            referral = _num(k.get('紹介料％')); fba = _num(k.get('FBA Pick&Pack 料金'))
            kwargs = {}
            if referral is not None:
                kwargs['amazon_fee_rate'] = referral / 100
            if fba is not None:
                kwargs['fba_fee_usd'] = fba
            profit = of.calc_unit_profit(us_price_usd=price, jp_cost_jpy=row['jp_cost_jpy'],
                                         weight_kg=row['weight_kg'] or of.DEFAULT_WEIGHT_KG_FALLBACK, exchange_rate=rate, **kwargs)
            drops_90 = _num(k.get('売れ筋ランキング: 過去90日間の減少'))
            drops_30 = round(drops_90 / 3, 1) if drops_90 is not None else None
            monthly = _num(k.get('月間売上トレンド: 先月の購入'))
            monthly = int(monthly) if monthly is not None else None
            rank = _num(k.get('売れ筋ランキング: 現在'))
            tier = None if row['excluded_kind'] else of._classify_priority_tier(profit['roi_pct'], monthly, drops_30)
            qualified = 0 if row['excluded_kind'] else int(of.is_qualified_priority_tier(tier))
            data = json.loads(row['data_json'] or '{}')
            result = {'asin': asin, 'status': 'ok', 'title': row['title'], 'tier_before': row['priority_tier'], 'tier_after': tier,
                      'roi_before': data.get('roi_pct'), 'roi_after': profit['roi_pct']}
            results.append(result)
            if apply:
                data['refresh_before'] = {'us_price_usd': row['us_price_usd'], 'monthly_sold': row['monthly_sold'],
                                          'sales_rank': row['sales_rank'], 'priority_tier': row['priority_tier'],
                                          'roi_pct': data.get('roi_pct'), 'unit_profit_usd': row['unit_profit_usd']}
                data.update(roi_pct=profit['roi_pct'], monthly_sold=monthly, sales_rank_drops_30=drops_30, rank_drops_30=drops_30,
                            us_price_usd=price, refreshed_by='supplier_pipeline.refresh')
                conn.execute(
                    '''UPDATE agent_candidates SET us_price_usd=?, monthly_sold=?, sales_rank=?, unit_profit_usd=?, margin_pct=?,
                           priority_tier=?, qualified=?, data_json=? WHERE id=?''',
                    (price, monthly, int(rank) if rank is not None else row['sales_rank'], profit['unit_profit_usd'],
                     profit['margin_pct'], tier, qualified, json.dumps(data, ensure_ascii=False), row['id']))
    return results


def _print_report(ev: dict) -> None:
    print(f"合格 {len(ev['qualified'])} / 不合格 {len(ev['rejected'])} / 足切り {len(ev['screened_out'])} / "
          f"既存スキップ {len(ev['skipped_existing'])} / Keepaなし {len(ev['no_keepa'])}")
    for e in ev['qualified']:
        mark = '(推定)' if e['evidence'] == 'rank_drops_estimate' else ''
        print(f"  {e['priority_tier']:>2}{mark} {e['asin']} {e['title']} ROI{e['roi_pct']:.0%} 利益${e['unit_profit_usd']} "
              f"月間{e['monthly_sold']} 順位{e['sales_rank']}" + (' 重量推定' if e['weight_estimated'] else ''))
    if ev['screened_out']:
        print('足切り: ' + ', '.join(f"{e['title']}[{'/'.join(e['screened_out'])}]" for e in ev['screened_out']))
    for d in ev['diffs']:
        print(f"  既存と食い違い: {d['asin']} {d['title']} 既存{d['existing_tier']} → 今回{d['new_tier']}(ROI{d['new_roi_pct']:.0%})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('existing')
    p.add_argument('--out', required=True)
    p = sub.add_parser('evaluate')
    p.add_argument('--map', required=True)
    p.add_argument('--keepa', required=True)
    p.add_argument('--supplier', required=True)
    p.add_argument('--existing')
    p.add_argument('--rate', type=float, default=DEFAULT_RATE)
    p.add_argument('--out', required=True)
    p = sub.add_parser('register')
    p.add_argument('--evaluation', required=True)
    p.add_argument('--supplier', required=True)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--include-rejected', action='store_true')
    p = sub.add_parser('audit')
    p.add_argument('--apply', action='store_true')
    p = sub.add_parser('refresh')
    p.add_argument('--keepa', required=True, nargs='+')
    p.add_argument('--asins', required=True, help='ASINを1行1件で書いたファイル')
    p.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)

    if args.cmd == 'existing':
        data = load_existing()
        Path(args.out).write_text(json.dumps(data, ensure_ascii=False))
        print(f'既存ASIN {len(data)}件 → {args.out}')
    elif args.cmd == 'evaluate':
        existing = json.loads(Path(args.existing).read_text()) if args.existing else {}
        ev = evaluate(load_map(args.map), load_keepa_csv(args.keepa), args.supplier, existing, args.rate)
        Path(args.out).write_text(json.dumps(ev, ensure_ascii=False))
        _print_report(ev)
    elif args.cmd == 'register':
        ev = json.loads(Path(args.evaluation).read_text())
        res = register(ev, args.supplier, apply=args.apply, include_rejected=args.include_rejected)
        print(f"{'予行演習' if res['dry_run'] else '登録'}: {res['count']}件" + (f" run_id={res['run_id']}" if res['run_id'] else ''))
    elif args.cmd == 'refresh':
        keepa = {}
        for path in args.keepa:
            keepa.update(load_keepa_csv(path))
        asins = [a.strip() for a in Path(args.asins).read_text().split() if a.strip()]
        res = refresh_rows(asins, keepa, apply=args.apply)
        for r in res:
            if r['status'] != 'ok':
                print(f"  {r['asin']}: 変更なし({r['reason']})")
            else:
                print(f"  {r['asin']} {str(r['title'])[:40]}: {r['tier_before']} → {r['tier_after']} (ROI {r['roi_before']} → {r['roi_after']})")
        print(('DBを更新' if args.apply else '予行演習') + f": {sum(1 for r in res if r['status'] == 'ok')}件")
    elif args.cmd == 'audit':
        rows = audit_supplier_rows()
        unknown = [r for r in rows if r['tier_after'] is None]
        changed = [r for r in rows if r['tier_after'] is not None and r['tier_before'] != r['tier_after']]
        print(f'旧方式の行 {len(rows)}件、Tierが変わるもの {len(changed)}件、手数料の記録が無く判定不能 {len(unknown)}件')
        for r in rows:
            if r['tier_after'] is None:
                print(f"  {r['asin']} {r['title']}: {r['tier_before']} (判定不能: data_jsonに手数料なし)")
                continue
            flag = '→' if r['tier_before'] != r['tier_after'] else '=='
            print(f"  {r['asin']} {r['title']}: {r['tier_before']} {flag} {r['tier_after']} "
                  f"(ROI {r['roi_before']} → {r['roi_after']})")
        if args.apply:
            print(f'DBを更新: {apply_audit(rows)}行')
    return 0


if __name__ == '__main__':
    sys.exit(main())
