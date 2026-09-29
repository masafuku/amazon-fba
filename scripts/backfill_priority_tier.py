#!/usr/bin/env python3
"""
backfill_priority_tier.py — agent_candidatesの既存行に、発注の優先度Tier
(priority_tier: S/A+/A-/B+/B-/C+/C-/D)・完全除外の種別(excluded_kind: food/drug_cosmetic/knife)・
フィギュアのフラグ(is_figure)・合否(qualified)・不合格理由(reason)を一括で付ける、
手動実行専用のスクリプト。

CEO: 「AWS版にTierの分類を加えたい」「食品、医薬品、刃物は輸出に課題があるので
完全除外」「フィギアは、Tierの分類はする(表示はオン/オフ)」。判定ロジックは
ops_finance.py の _classify_priority_tier / _excluded_kind を使う(定義元は
_shared/fba-sourcing-candidates.md の「Tier 確定版」)。保存済みの列・data_json・
タイトルだけから計算するので、Keepaは呼ばない(トークン消費なし)。

2026-09-28: tier(pass/consider/reference/reject)がpriority_tierへ統合されたのに
伴い、qualified/reasonもこのスクリプトで再計算するようになった(is_qualified_priority_tier
/ _priority_rejection_reason、ops_finance.py参照)。ただし「粗選別で除外」(価格差率・
価格変動などで落ちた候補、reasonがこの文言で始まる行)は、たとえpriority_tierが
合格範囲になっていてもqualified=0のまま維持する(evaluate_mcp_candidates()側の
挙動と一致させるための例外 - 詳細はops_finance.evaluate_mcp_candidates()のdocstring参照)。

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
    DB_PATH, PRIORITY_TIER_ORDER, _classify_priority_tier, _excluded_kind,
    _is_figure_or_collectible_keyword, _priority_rejection_reason,
    is_qualified_priority_tier, init_ops_tables,
)

TIER_ORDER = list(PRIORITY_TIER_ORDER)
_COARSE_SKIP_PREFIX = '粗選別で除外'


def classify_row(asin, title, monthly_sold, sales_rank, unit_profit_usd, data_json, old_reason):
    """(priority_tier, excluded_kind, is_figure, qualified, reason) を返す。"""
    try:
        data = json.loads(data_json) if data_json else {}
    except Exception:
        data = {}
    if monthly_sold is None:
        monthly_sold = data.get('monthly_sold')
    title = title or ''
    kind = _excluded_kind(title, is_title=True)
    is_figure = 1 if _is_figure_or_collectible_keyword(title) else 0
    tier = None if kind else _classify_priority_tier(
        data.get('roi_pct'), monthly_sold, data.get('sales_rank_drops_30'),
    )

    is_coarse_skip = bool(old_reason) and old_reason.startswith(_COARSE_SKIP_PREFIX)
    if kind:
        qualified, reason = 0, f'excluded_{kind}'
    elif is_coarse_skip:
        # evaluate_mcp_candidates()側の挙動に合わせ、粗選別で落ちた行は
        # priority_tierに関わらずqualified=0のまま、元のreasonも変えない。
        qualified, reason = 0, old_reason
    else:
        entry = {
            'priority_tier': tier, 'roi_pct': data.get('roi_pct'),
            'monthly_sold': monthly_sold, 'sales_rank_drops_30': data.get('sales_rank_drops_30'),
        }
        qualified = 1 if is_qualified_priority_tier(tier) else 0
        reason = None if qualified else _priority_rejection_reason(entry)
    return tier, kind, is_figure, qualified, reason


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='実際に書き込む(既定はdry-run)')
    parser.add_argument('--list-excluded', action='store_true', help='除外に当たったタイトルを、ASINごとに全件表示する')
    args = parser.parse_args()

    # dry-runで列が未作成のDB(本番のマイグレーション前)でも動くよう、読み取りに使うのは
    # 既存の列だけにする。
    with sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True) as conn:
        rows = conn.execute(
            'SELECT id, asin, title, monthly_sold, sales_rank, unit_profit_usd, data_json, created_at, '
            'qualified, reason '
            'FROM agent_candidates ORDER BY id'
        ).fetchall()

    updates = []
    qualified_transition = Counter()  # (旧qualified, 新qualified) -> 件数
    latest = {}  # asin -> (created_at, id, tier, kind, is_figure, title, qualified)
    for row_id, asin, title, monthly_sold, sales_rank, unit_profit_usd, data_json, created_at, old_qualified, old_reason in rows:
        tier, kind, is_figure, qualified, reason = classify_row(
            asin, title, monthly_sold, sales_rank, unit_profit_usd, data_json, old_reason,
        )
        updates.append((tier, kind, is_figure, qualified, reason, row_id))
        qualified_transition[(bool(old_qualified), bool(qualified))] += 1
        prev = latest.get(asin)
        if prev is None or (created_at, row_id) > (prev[0], prev[1]):
            latest[asin] = (created_at, row_id, tier, kind, is_figure, title, qualified)

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
    summarize('全行', [(t, k, f) for t, k, f, _, _, _ in updates])
    summarize('ASIN重複除外(最新の行)', [(v[2], v[3], v[4]) for v in latest.values()])

    print('\nqualified遷移(旧→新、全行):')
    for (old_q, new_q), n in sorted(qualified_transition.items()):
        print(f'  {int(old_q)} -> {int(new_q)}: {n}件')
    latest_qualified_count = sum(1 for v in latest.values() if v[6])
    print(f'ASIN重複除外(最新の行)でqualified=1: {latest_qualified_count} / {len(latest)} ASIN')

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
            'UPDATE agent_candidates SET priority_tier=?, excluded_kind=?, is_figure=?, qualified=?, reason=? WHERE id=?',
            updates,
        )
    print(f'{len(updates)}行を更新しました。')


if __name__ == '__main__':
    main()
