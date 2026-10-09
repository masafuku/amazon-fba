#!/usr/bin/env python3
"""
backfill_seller_count.py — 既存のagent_candidates行に残っている、誤った
competitor_seller_countの値を一括で正しい値に上書き訂正する一度きりのスクリプト
(パイプラインには組み込まない、手動実行専用)。

CEO: 「セラー数が38となっていますが、keepaで直接見た値と明らかに違います。
調査おねがいします。」

背景: 以前のセラー数取得は、Keepaのoffers配列(過去の・もう出品されていない
古いオファーが混在)を全件カウントしており、実データを大きく水増ししていた
(例: ASIN B07V31TRKB は 38 と保存されていたが、正しくは 2)。修正版
(keepa_mcp.server.recompute_seller_count、通常の商品取得1トークンのみ)で
既存の合格候補を再計算し、data_jsonを上書きする。

合格候補(qualified=1)のASINだけを重複排除して対象にする(不合格候補は
CEOが普段見ないため対象外、トークンも節約)。同じASINが複数run_idで
見つかっている場合は、Keepa呼び出しを1回だけにして全てのrun_id行を更新する。

使い方:
    .venv/bin/python scripts/backfill_seller_count.py --dry-run   # 変更内容だけ確認
    .venv/bin/python scripts/backfill_seller_count.py             # 実際に上書きする
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

# scripts/ 配下から起動されるため、リポジトリルートを明示的にsys.pathへ追加する
# (seller_mine_cli.py と同じ理由)。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keepa_mcp.config import settings as keepa_settings
from keepa_mcp.keepa_client import KeepaError, get_token_status
from keepa_mcp.server import recompute_seller_count
from ops_finance import DB_PATH


def _wait_for_one_token() -> None:
    """netsea_sourcing.pyの_wait_for_one_tokenと同じ考え方(残高0コストの
    /tokenをポーリングして待つ)。このスクリプトは31件程度をまとめて叩くため、
    低いrefill_rateのプランでは待たないとほぼ全件429で失敗する。"""
    while True:
        status = get_token_status(keepa_settings.keepa_api_key)
        if (status.get("tokens_left") or 0) >= 1:
            return
        refill_rate = status.get("refill_rate_per_minute") or 1
        wait_seconds = min(60, max(5, math.ceil(60 / refill_rate)))
        time.sleep(wait_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="DBを書き換えず、変更前後の値だけ表示する")
    args = parser.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT asin FROM agent_candidates WHERE qualified = 1 ORDER BY asin",
        ).fetchall()
    asins = [r[0] for r in rows]

    if not asins:
        print("対象の合格候補が見つかりませんでした。")
        return

    print(f"対象ASIN: {len(asins)}件{'(dry-run: 書き換えません)' if args.dry_run else ''}")

    changed = 0
    for asin in asins:
        _wait_for_one_token()
        try:
            new_count = recompute_seller_count(asin)
        except KeepaError as exc:
            print(f"  {asin}: 取得失敗({exc})")
            continue

        with sqlite3.connect(DB_PATH) as conn:
            run_rows = conn.execute(
                "SELECT run_id, data_json FROM agent_candidates WHERE asin = ?",
                (asin,),
            ).fetchall()
            for run_id, data_json in run_rows:
                data = json.loads(data_json)
                old_count = data.get("competitor_seller_count")
                if old_count == new_count:
                    continue
                print(f"  {asin} (run_id={run_id}): {old_count} -> {new_count}")
                changed += 1
                if not args.dry_run:
                    data["competitor_seller_count"] = new_count
                    conn.execute(
                        "UPDATE agent_candidates SET data_json = ? WHERE run_id = ? AND asin = ?",
                        (json.dumps(data, ensure_ascii=False), run_id, asin),
                    )
            if not args.dry_run:
                conn.commit()

    print(f"完了: {changed}行{'を更新しました' if not args.dry_run else 'が変更対象でした(--dry-runのため未書き換え)'}")


if __name__ == "__main__":
    main()
