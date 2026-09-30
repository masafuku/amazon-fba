#!/usr/bin/env python3
"""
check_candidate_restrictions.py — 調査エージェントの候補(優先度Tier S/A/B+)について、SP-APIの
Listings Restrictions APIで、Amazon.comに新品で出品できるかを照会し、DBの
agent_candidates.listing_status / listing_checked_at に保存する。出品は一切行わない(照会のみ)。

CEO: 「Holbein や muji は商品登録ができない可能性があります。登録まで自動で試す方法を
提案できますか？」→ 承認を得て組み込み(2026-09-27)。スキャンのサイクルの後に自動で回る
(scripts/run_all_day.sh)。SP-APIの認証情報(.env)が無い環境では、何もせず正常終了する。

使い方:
    .venv/bin/python scripts/check_candidate_restrictions.py             # 未確認・7日以上前のものを最大30件
    .venv/bin/python scripts/check_candidate_restrictions.py --limit 100 --max-age-days 3
    .venv/bin/python scripts/check_candidate_restrictions.py --dry-run   # 対象のASINを表示するだけ
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops_finance import load_asins_needing_listing_check, save_listing_status  # noqa: E402
from sp_api import client  # noqa: E402
from sp_api.config import settings  # noqa: E402
from sp_api.restrictions import STATUS_LABELS, summarize_restrictions  # noqa: E402

REQUEST_INTERVAL_SECONDS = 0.5   # Listings Restrictions API のレート制限(毎秒5回程度)より十分ゆっくり


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=30, help="1回で照会する最大ASIN数")
    parser.add_argument("--max-age-days", type=int, default=7, help="この日数より古い照会結果は再照会する")
    parser.add_argument("--all", action="store_true",
                        help="優先度Tierに関係なく、完全除外以外の全ASINを対象にする(全件の一括照会)")
    parser.add_argument("--dry-run", action="store_true", help="照会せず、対象のASINを表示するだけ")
    args = parser.parse_args()

    asins = load_asins_needing_listing_check(limit=args.limit, max_age_days=args.max_age_days, all_tiers=args.all)
    if args.dry_run:
        print(f"[dry-run] 照会対象 {len(asins)}件: {' '.join(asins)}")
        return 0

    if not (settings.configured and settings.seller_id):
        print("[INFO] SP-APIの認証情報(または販売者ID)が未設定のため、出品可否の照会をスキップしました。")
        return 0
    if not asins:
        print("[INFO] 出品可否を照会するASINはありません。")
        return 0

    counts: Counter = Counter()
    errors = 0
    for index, asin in enumerate(asins, start=1):
        if len(asins) > 100 and index % 200 == 0:
            print(f"[INFO] 進捗: {index}/{len(asins)}件 (失敗 {errors}件)", flush=True)
        try:
            payload = client.get_listings_restrictions(asin)
        except client.SpApiError as exc:
            errors += 1
            print(f"[WARN] {asin}: 照会失敗 - {str(exc)[:120]}")
            continue
        status, _details = summarize_restrictions(payload)
        save_listing_status(asin, status)
        counts[status] += 1
        time.sleep(REQUEST_INTERVAL_SECONDS)
    summary = " / ".join(f"{STATUS_LABELS[s]} {n}件" for s, n in counts.items()) or "なし"
    print(f"[INFO] 出品可否を照会しました({len(asins)}件中、失敗 {errors}件): {summary}")
    return 1 if errors and not counts else 0


if __name__ == "__main__":
    raise SystemExit(main())
