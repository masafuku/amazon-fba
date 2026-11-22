#!/usr/bin/env python3
"""
check_candidate_wholesale.py — 調査エージェントの候補(優先度Tier S/A/B+、出品可)について、
netsea_catalog(scripts/sync_netsea_catalog.py で同期)をJANで突き合わせ、NETSEAの卸価格・仕入れ先・
最小ロットを agent_candidates.data_json に保存する。卸価格の方が安ければ、原価・利益・ROI・優先度Tier
を再計算する。Keepa・NETSEAのAPIは呼ばない(DBの照会のみ)。

CEO: 「スーパーデリバリー等の仕入れ価格を自動で確認するように変更することはできる？」→
NETSEAを自動化(SDはログインにボット判定があり自動化できない)。スキャンのサイクルの後に自動で
回る(scripts/run_all_day.sh)。

使い方:
    .venv/bin/python scripts/check_candidate_wholesale.py --dry-run   # 対象と突き合わせ結果を表示するだけ
    .venv/bin/python scripts/check_candidate_wholesale.py             # 未照会・7日以上前のものを最大20件
    .venv/bin/python scripts/check_candidate_wholesale.py --limit 200 --max-age-days 0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MARKER_PATH = Path(__file__).resolve().parent.parent / '.netsea_catalog_synced_at'

from ops_finance import (  # noqa: E402
    apply_wholesale_result, find_netsea_match, load_candidates_needing_wholesale,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--limit', type=int, default=20, help='1回で照会する最大件数')
    parser.add_argument('--max-age-days', type=int, default=7, help='この日数より古い照会結果は再照会する')
    parser.add_argument('--dry-run', action='store_true', help='DBに書き込まず、突き合わせ結果を表示するだけ')
    args = parser.parse_args()

    # カタログの全件同期が一度も成功していないうちに突き合わせると、「該当なし」を誤って記録して
    # しまうため、(dry-run以外は)何もしない。
    if not args.dry_run and not MARKER_PATH.exists():
        print('[INFO] NETSEAカタログの全件同期が未完了のため、卸価格の照会をスキップしました。')
        return 0

    candidates = load_candidates_needing_wholesale(limit=args.limit, max_age_days=args.max_age_days)
    if not candidates:
        print('[INFO] 卸価格を照会する候補はありません。')
        return 0

    matched = recalculated = 0
    for cand in candidates:
        match = find_netsea_match(cand['jans'])
        if match:
            matched += 1
        if args.dry_run:
            if match:
                print(f"[dry-run] {cand['asin']}: 一致 ¥{match['unit_price_jpy']:.0f}/個 "
                      f"({match['shop_name']}、{match['set_num']}個〜) {match['product_url']}")
            else:
                print(f"[dry-run] {cand['asin']}: 該当なし")
            continue
        result = apply_wholesale_result(cand['id'], match)
        if result.get('recalculated'):
            recalculated += 1
            print(f"[INFO] {cand['asin']}: 卸価格で再計算 ROI {result['roi_before']} -> {result['roi_after']}、"
                  f"Tier {result['tier_before']} -> {result['tier_after']}")
    if args.dry_run:
        print(f'[dry-run] {len(candidates)}件中 一致 {matched}件(DBは書き換えていません)')
    else:
        print(f'[INFO] 卸価格を照会しました({len(candidates)}件中、一致 {matched}件、再計算 {recalculated}件)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
