#!/usr/bin/env python3
"""
backfill_priority_tier.py — agent_candidatesの既存行に、発注の優先度Tier
(priority_tier: S/A/B+/B-/C)・完全除外の種別(excluded_kind: food/drug_cosmetic/knife)・
フィギュアのフラグ(is_figure)を一括で付ける、手動実行専用のスクリプト。

CEO: 「AWS版にTierの分類を加えたい」「食品、医薬品、刃物は輸出に課題があるので
完全除外」「フィギアは、Tierの分類はする(表示はオン/オフ)」。判定ロジックは
ops_finance.py の _classify_priority_tier / _excluded_kind を使う(定義元は
_shared/fba-sourcing-candidates.md の「Tier 確定版」)。保存済みの列・data_json・
タイトルだけから計算するので、Keepaは呼ばない(トークン消費なし)。

**既定は dry-run(何も書き換えない)。** 実際に書き込むときは --apply を付ける
(書き込む前に、DBファイルをバックアップする)。ローカルDBとAWS本番DBは別
ファイルなので、それぞれで実行する。

使い方:
    .venv/bin/python scripts/backfill_priority_tier.py                 # 件数の確認のみ
    .venv/bin/python scripts/backfill_priority_tier.py --list-excluded # 除外タイトルの一覧(誤検知の確認用)
    .venv/bin/python scripts/backfill_priority_tier.py --apply         # 実際に書き込む
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops_finance import (  # noqa: E402
    DB_PATH, _classify_priority_tier, _excluded_kind, _is_figure_or_collectible_keyword,
    _is_media, init_ops_tables,
)

TIER_ORDER = ['S', 'A', 'B+', 'B-', 'C']


def classify_row(asin, title, monthly_sold, sales_rank, unit_profit_usd, data_json):
    """(priority_tier, excluded_kind, is_figure) を返す。"""
    try:
        data = json.loads(data_json) if data_json else {}
    except Exception:
        data = {}
    if monthly_sold is None:
        monthly_sold = data.get('monthly_sold')
    if sales_rank is None:
        sales_rank = data.get('sales_rank')
    title = title or ''
    kind = _excluded_kind(title, is_title=True)
    is_figure = 1 if _is_figure_or_collectible_keyword(title) else 0
    tier = None if kind else _classify_priority_tier(
        unit_profit_usd, data.get('roi_pct'), monthly_sold, sales_rank,
        is_media=_is_media(asin, title), sales_rank_drops_30=data.get('sales_rank_drops_30'),
    )
    return tier, kind, is_figure


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='実際に書き込む(既定はdry-run)')
    parser.add_argument('--list-excluded', action='store_true', help='除外に当たったタイトルを、ASINごとに全件表示する')
    args = parser.parse_args()

    # dry-runで列が未作成のDB(本番のマイグレーション前)でも動くよう、読み取りに使うのは
    # 既存の列だけにする。
    with sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True) as conn:
        rows = conn.execute(
            'SELECT id, asin, title, monthly_sold, sales_rank, unit_profit_usd, data_json, created_at '
            'FROM agent_candidates ORDER BY id'
        ).fetchall()

    updates = []
    latest = {}  # asin -> (created_at, id, tier, kind, is_figure, title)
    for row_id, asin, title, monthly_sold, sales_rank, unit_profit_usd, data_json, created_at in rows:
        tier, kind, is_figure = classify_row(asin, title, monthly_sold, sales_rank, unit_profit_usd, data_json)
        updates.append((tier, kind, is_figure, row_id))
        prev = latest.get(asin)
        if prev is None or (created_at, row_id) > (prev[0], prev[1]):
            latest[asin] = (created_at, row_id, tier, kind, is_figure, title)

    def summarize(label, items):
        tiers, kinds, figs = Counter(), Counter(), Counter()
        for tier, kind, is_figure in items:
            if kind:
                kinds[kind] += 1
            else:
                tiers[tier] += 1
                if is_figure:
                    figs[tier] += 1
        print(f'{label}:')
        print('  Tier: ' + ', '.join(f'{t}={tiers[t]}' for t in TIER_ORDER))
        print('  完全除外: ' + (', '.join(f'{k}={n}' for k, n in sorted(kinds.items())) or 'なし')
              + f' (計{sum(kinds.values())})')
        print('  うちフィギュア(Tier付き): ' + (', '.join(f'{t}={figs[t]}' for t in TIER_ORDER if figs[t]) or 'なし'))

    print(f'DB: {DB_PATH}')
    print(f'対象: {len(rows)}行 / {len(latest)} ASIN')
    summarize('全行', [(t, k, f) for t, k, f, _ in updates])
    summarize('ASIN重複除外(最新の行)', [(v[2], v[3], v[4]) for v in latest.values()])

    excluded = [(v[3], asin, v[5]) for asin, v in latest.items() if v[3]]
    limit = None if args.list_excluded else 5
    for kind in ('food', 'drug_cosmetic', 'knife', 'hazmat'):
        titles = [(asin, title) for k, asin, title in excluded if k == kind]
        print(f'\n[{kind}] {len(titles)}件' + ('' if args.list_excluded else f'(先頭{limit}件。全件は --list-excluded)'))
        for asin, title in (titles if limit is None else titles[:limit]):
            print(f'  {asin}  {(title or "")[:90]}')

    if not args.apply:
        print('\n(dry-run: 何も書き換えていません。書き込むときは --apply)')
        return

    backup = DB_PATH.with_name(f'{DB_PATH.name}.bak-{datetime.now():%Y%m%d_%H%M%S}')
    shutil.copy(DB_PATH, backup)
    print(f'\nバックアップ: {backup}')
    init_ops_tables()  # priority_tier/excluded_kind/is_figure 列が無ければ追加する
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            'UPDATE agent_candidates SET priority_tier=?, excluded_kind=?, is_figure=? WHERE id=?',
            updates,
        )
    print(f'{len(updates)}行を更新しました。')


if __name__ == '__main__':
    main()
