#!/usr/bin/env python3
"""
backfill_exclusions.py — agent_candidatesの既存行のうち、いまの除外ルール(化粧品・無印良品・電気製品など、
ops_finance.candidate_excluded_kind)に当たるのに除外になっていない行だけを、完全除外にする。

scripts/backfill_priority_tier.py は全行のTier・qualifiedを再計算するが、こちらは
「excluded_kindが空 かつ 新しく除外に当たる行」だけを触る(ほかの行のTierや合否は変えない)。
除外にする行は、既存の除外行と同じ形にする: priority_tier=NULL, qualified=0, reason='excluded_<種別>'。
ダッシュボードとダイジェストは excluded_kind が空の行だけを使うので、これで候補から外れる。

**既定は dry-run(何も書き換えない)。** --apply のときはDBをバックアップしてから書き込む。
ブランドは data_json の brand 欄も見る。DBは実行したマシンのもの(本番はAWSで実行)。

使い方:
    .venv/bin/python scripts/backfill_exclusions.py                 # 件数と、S/A+への影響の確認
    .venv/bin/python scripts/backfill_exclusions.py --list electronics   # 種別ごとのタイトル全件(誤検知の確認用)
    .venv/bin/python scripts/backfill_exclusions.py --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops_finance import DB_PATH, candidate_excluded_kind, init_ops_tables  # noqa: E402


def find_new_exclusions(conn) -> list:
    """[(id, asin, title, tier, kind, is_latest_for_asin)]"""
    latest = {asin: max_id for asin, max_id in conn.execute('SELECT asin, MAX(id) FROM agent_candidates GROUP BY asin')}
    found = []
    for row_id, asin, title, tier, data_json in conn.execute(
            'SELECT id, asin, title, priority_tier, data_json FROM agent_candidates WHERE excluded_kind IS NULL'):
        try:
            brand = (json.loads(data_json) if data_json else {}).get('brand')
        except ValueError:
            brand = None
        kind = candidate_excluded_kind(title, brand)
        if kind:
            found.append((row_id, asin, title or '', tier, kind, latest.get(asin) == row_id))
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='実際に書き込む(既定はdry-run)')
    parser.add_argument('--list', metavar='KIND', help='指定した種別の、最新行のタイトルを全件表示する')
    args = parser.parse_args()

    with sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True) as conn:
        found = find_new_exclusions(conn)

    by_kind = Counter(f[4] for f in found)
    latest = [f for f in found if f[5]]
    print(f'DB: {DB_PATH}')
    print(f'新しく除外になる行: {len(found)}行 / {len({f[1] for f in found})} ASIN（最新の行は{len(latest)}件）')
    print('  種別: ' + ', '.join(f'{k}={n}' for k, n in sorted(by_kind.items())))
    tiers = Counter(f[3] or '(Tierなし)' for f in latest)
    print('  最新の行のTier: ' + ', '.join(f'{t}={n}' for t, n in sorted(tiers.items())))
    top = [f for f in latest if f[3] in ('S', 'A+')]
    print(f'  うちS/A+: {len(top)}件')

    samples = defaultdict(list)
    for f in latest:
        samples[f[4]].append(f)
    for kind, items in sorted(samples.items()):
        if args.list and kind != args.list:
            continue
        shown = items if args.list else sorted(items, key=lambda f: (f[3] not in ('S', 'A+'), f[3] or 'Z'))[:8]
        print(f'\n[{kind}] 最新の行{len(items)}件' + ('' if args.list else '（S/A+を優先して先頭8件。全件は --list <種別>）'))
        for row_id, asin, title, tier, _, _ in shown:
            print(f'  {tier or "-":<3} {asin} {title[:80]}')

    if not args.apply:
        print('\n(dry-run: 何も書き換えていません。書き込むときは --apply)')
        return

    backup = DB_PATH.with_name(f'{DB_PATH.name}.bak-{datetime.now():%Y%m%d_%H%M%S}')
    shutil.copy(DB_PATH, backup)
    print(f'\nバックアップ: {backup}')
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "UPDATE agent_candidates SET excluded_kind = ?, priority_tier = NULL, qualified = 0, reason = ? WHERE id = ? AND excluded_kind IS NULL",
            [(f[4], f'excluded_{f[4]}', f[0]) for f in found],
        )
    print(f'{len(found)}行を完全除外にしました。')


if __name__ == '__main__':
    main()
