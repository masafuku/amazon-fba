#!/usr/bin/env python3
"""
sync_netsea_catalog.py — NETSEA(Buyer API)の、取引承認済みサプライヤーの全商品のうち、JANコード付きの
ものを、DBの netsea_catalog に保存(入れ替え)する。/items にはJAN・キーワードの検索が無いため、
候補ごとの照会はできない。全件を保存しておき、scripts/check_candidate_wholesale.py が候補のJANで
突き合わせる。

サプライヤー10社(API上の上限)ごとに、全ページを取得してから、その10社分を1トランザクションで
入れ替える。途中で止まっても、取得済みのバッチは保存されている(次回は最初からやり直す)。
1日1回程度の実行を想定している(scripts/run_all_day.sh が、前回から24時間以上たっていれば呼ぶ)。

全サプライヤーの同期には、1時間前後かかる見込み(取引承認済み165社のうち、最初の25社だけで約3.6万件、8分)。
そのため、スキャンのループ(scripts/run_all_day.sh)からは、バックグラウンドで、--if-older-than-hours 付きで
呼ぶ(全件の同期が最後に成功した時刻を、.netsea_catalog_synced_at に記録して判定する)。

使い方:
    .venv/bin/python scripts/sync_netsea_catalog.py                  # 全サプライヤー
    .venv/bin/python scripts/sync_netsea_catalog.py --suppliers 10   # 先頭の10社だけ(動作確認用)
    .venv/bin/python scripts/sync_netsea_catalog.py --dry-run --suppliers 10   # DBに書かず、件数だけ表示
    .venv/bin/python scripts/sync_netsea_catalog.py --if-older-than-hours 72   # 前回の全件同期から72時間以上たっていれば実行
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / '.env')

from netsea_client import BATCH_SIZE, NetseaError, get_items  # noqa: E402
from netsea_sourcing import fetch_supplier_pool  # noqa: E402
from ops_finance import delete_stale_netsea_rows, netsea_rows_from_items, upsert_netsea_rows  # noqa: E402

MARKER_PATH = Path(__file__).resolve().parent.parent / '.netsea_catalog_synced_at'
PAGE_RETRIES = 4                  # 1ページの取得の再試行回数(タイムアウト等の一時的な失敗用)
REQUEST_INTERVAL_SECONDS = 0.15   # netsea_sourcing.py と同じ(明示的なレート制限は不明のため保守的に)


def get_page_with_retry(token: str, supplier_ids: list, next_id) -> dict:
    """/items の1ページを取得する。タイムアウト等(NetseaError・OSError)は、間隔を空けて再試行する。
    在庫あり(sold_out_flag='N')の商品だけを取得する(売り切れの商品まで入れると件数が大きく増える)。"""
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            return get_items(token, supplier_ids, sold_out_flag='N', next_direct_item_id=next_id)
        except (NetseaError, OSError) as exc:
            if attempt == PAGE_RETRIES:
                raise NetseaError(f'{PAGE_RETRIES}回失敗: {exc}') from exc
            time.sleep(5 * attempt)


def sync_batch(token: str, supplier_ids: list, dry_run: bool) -> tuple:
    """指定サプライヤー(最大BATCH_SIZE社)の全商品を、next_direct_item_idでページングして取得し、
    1ページごとに netsea_catalog へ書き込む(メモリを増やさないため。常駐サーバーのメモリは約
    400MBしかなく、10社分の商品を全部持つと足りなくなる)。全ページの取得に成功したら、今回の同期で
    見つからなかった古い行(出品終了した商品)を削除する。戻り値: (商品数, JAN付きセット数)。"""
    started = datetime.now(timezone.utc).isoformat()
    item_count = row_count = pages = 0
    next_id = None
    while True:
        page = get_page_with_retry(token, supplier_ids, next_id)
        data = page.get('data', [])
        rows = netsea_rows_from_items(data, started)
        item_count += len(data)
        row_count += len(rows)
        if rows and not dry_run:
            upsert_netsea_rows(rows)
        pages += 1
        if pages % 50 == 0:
            print(f'[INFO]   ... {pages}ページ、商品 {item_count}件', flush=True)
        next_id = page.get('next_direct_item_id')
        if not next_id:
            break
        time.sleep(REQUEST_INTERVAL_SECONDS)
    if not dry_run:
        delete_stale_netsea_rows(supplier_ids, started)
    return item_count, row_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--suppliers', type=int, default=None, help='先頭のN社だけ同期する(動作確認用)')
    parser.add_argument('--dry-run', action='store_true', help='DBに書き込まず、件数だけ表示する')
    parser.add_argument('--if-older-than-hours', type=float, default=None,
                        help='前回の全件同期(成功)からこの時間以上たっているときだけ実行する')
    args = parser.parse_args()

    if args.if_older_than_hours is not None and MARKER_PATH.exists():
        age_hours = (time.time() - MARKER_PATH.stat().st_mtime) / 3600
        if age_hours < args.if_older_than_hours:
            print(f'[INFO] NETSEAカタログは {age_hours:.0f}時間前に同期済みのため、スキップしました。')
            return 0

    token = os.getenv('NETSEA_ACCESS_TOKEN')
    if not token:
        print('[INFO] NETSEA_ACCESS_TOKEN が未設定のため、NETSEAカタログの同期をスキップしました。')
        return 0

    suppliers = fetch_supplier_pool(token)
    supplier_ids = [str(s['id']) for s in suppliers]
    if args.suppliers:
        supplier_ids = supplier_ids[:args.suppliers]

    started = time.time()
    total_items = total_rows = failed_batches = 0
    for i in range(0, len(supplier_ids), BATCH_SIZE):
        batch = supplier_ids[i:i + BATCH_SIZE]
        try:
            item_count, row_count = sync_batch(token, batch, args.dry_run)
        except NetseaError as exc:
            failed_batches += 1
            print(f'[WARN] サプライヤー {batch[0]}〜: 取得失敗(このバッチは前回の内容を残します) - {str(exc)[:120]}')
            continue
        total_items += item_count
        total_rows += row_count
        print(f'[INFO] {min(i + BATCH_SIZE, len(supplier_ids))}/{len(supplier_ids)}社: 商品 {item_count}件、JAN付きセット {row_count}件', flush=True)

    if not args.dry_run and not args.suppliers and not failed_batches:
        MARKER_PATH.touch()   # 全件の同期が成功した時刻(--if-older-than-hours の判定用)
    print(f'[INFO] NETSEAカタログを{"確認" if args.dry_run else "同期"}しました: {len(supplier_ids)}社、商品 {total_items}件、'
          f'JAN付きセット {total_rows}件、失敗バッチ {failed_batches}、{time.time() - started:.0f}秒')
    return 1 if failed_batches and not total_rows else 0


if __name__ == '__main__':
    raise SystemExit(main())
