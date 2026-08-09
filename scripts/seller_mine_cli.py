#!/usr/bin/env python3
"""
seller_mine_cli.py — sqlite_api_server.py(素のpython3、依存なし)から
keepa_mcp(mcpパッケージが要る、.venv/bin/pythonが必須)を呼ぶための
サブプロセス橋渡し。scan-loopの停止/再開(sqlite_api_server.pyの
control_scan_loop())と同じ「subprocess経由で別プロセスに任せる」設計。

ダッシュボードの「セラーマイニング」ページから呼ばれる想定。ブラウザからの
リクエストを長時間ブロックしないよう、常に wait_for_tokens=False で実行する
(トークンが足りない場合は即座にその時点までの結果を返す)。

標準出力にJSON1行だけを出す(最後の行だけがsqlite_api_server.py側で
パースされるので、警告等の余計な出力があっても壊れないが、意図的に
print()は使わずJSON以外は何も出さない)。

使い方:
    .venv/bin/python scripts/seller_mine_cli.py discover-sellers --asin B0XXXXXXXX [--max-sellers 5]
    .venv/bin/python scripts/seller_mine_cli.py expand --seller-id A1234567890 [--max-candidates 15] [--seed-asin B0XXXXXXXX] [--price-diff-min 0.3]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# このスクリプトは scripts/ 配下にあるため、`python /abs/path/scripts/seller_mine_cli.py`
# で起動すると sys.path[0] がこのファイルのディレクトリ(scripts/)になり、
# リポジトリルート直下の keepa_mcp/ ops_finance.py が見つからない
# (cwdをリポジトリルートにしても解決しない - sys.path[0]はスクリプト自身の
# 場所で決まるため)。明示的にリポジトリルートをsys.pathへ追加する。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keepa_mcp.keepa_client import KeepaError
from keepa_mcp.server import expand_from_seller, find_other_sellers_for_candidate
from ops_finance import (
    add_sellers,
    evaluate_mcp_candidates,
    init_ops_tables,
    log_agent_run,
    new_agent_run_id,
    persist_agent_run,
    record_seller_mined,
)


def cmd_discover_sellers(args: argparse.Namespace) -> dict:
    try:
        result = find_other_sellers_for_candidate(asin=args.asin, max_sellers=args.max_sellers)
    except KeepaError as exc:
        return {"ok": False, "error": str(exc)}

    if not result.get("found"):
        return {
            "ok": True, "asin": args.asin, "found": False, "sellerIds": [],
            "note": result.get("note") or result.get("error"),
        }
    seller_ids = result.get("seller_ids") or []
    # 定期セラーマイニング(Sellerエージェント)が後で巡回できるよう、
    # ダッシュボードから手動発見したセラーもプールに登録しておく。
    add_sellers(seller_ids, source="manual_expand", seed_asin=args.asin)
    return {
        "ok": True, "asin": args.asin, "found": True,
        "sellerIds": seller_ids,
        "domain": result.get("domain"),
    }


def cmd_expand(args: argparse.Namespace) -> dict:
    init_ops_tables()
    started_at = datetime.now(timezone.utc).isoformat()
    # プールに無いセラーIDが直接指定された場合(コピペしたばかりのセラーURL等)
    # にも登録しておく - INSERT OR IGNOREなので既存なら何もしない。
    add_sellers([args.seller_id], source="manual_expand", seed_asin=args.seed_asin)

    try:
        result = expand_from_seller(
            seller_id=args.seller_id,
            max_candidates=args.max_candidates,
            price_diff_min=args.price_diff_min,
            price_volatility_max=None,
            wait_for_tokens=False,  # UIからの呼び出しは長時間ブロックしない
        )
    except KeepaError as exc:
        return {"ok": False, "error": str(exc)}

    if result.get("error"):
        return {"ok": False, "error": result["error"]}

    seller_name = result.get("seller_name") or args.seller_id
    evaluation = evaluate_mcp_candidates(result)
    record_seller_mined(args.seller_id, qualified_count=len(evaluation["qualified"]), seller_name=seller_name)

    run_id = new_agent_run_id()
    label = "セラーマイニング(手動)"
    if evaluation["qualified"] or evaluation["rejected"]:
        persist_agent_run(
            f"{label} (セラー: {seller_name})", evaluation, run_id=run_id,
            source_type="seller", seller_id=args.seller_id, seller_name=seller_name,
            seed_asin=args.seed_asin,
        )
    log_agent_run(
        run_id, started_at, 0,
        keyword=f"[seller] {seller_name}", category=label,
        max_candidates=args.max_candidates, wait_for_tokens=False,
        mcp_result=result, evaluation=evaluation,
        notify_status="deferred_to_digest",
        source_type="seller", seller_id=args.seller_id, seller_name=seller_name,
        seed_asin=args.seed_asin,
    )

    return {
        "ok": True,
        "runId": run_id,
        "sellerId": args.seller_id,
        "sellerName": seller_name,
        "evaluated": result.get("evaluated", 0),
        "matched": result.get("matched", 0),
        "qualifiedCount": len(evaluation["qualified"]),
        "rejectedCount": len(evaluation["rejected"]),
        "stoppedEarlyForTokens": bool(result.get("stopped_early_for_tokens")),
        "note": result.get("note"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover-sellers", help="ASINからそのASINを出品しているセラー一覧を取得する")
    discover.add_argument("--asin", required=True)
    discover.add_argument("--max-sellers", type=int, default=5)
    discover.set_defaults(func=cmd_discover_sellers)

    expand = subparsers.add_parser("expand", help="セラーの出品一覧を実質利益率パイプラインで評価する")
    expand.add_argument("--seller-id", required=True)
    expand.add_argument("--max-candidates", type=int, default=15)
    expand.add_argument("--seed-asin", default=None)
    expand.add_argument("--price-diff-min", type=float, default=0.30)
    expand.set_defaults(func=cmd_expand)

    args = parser.parse_args()
    try:
        output = args.func(args)
    except Exception as exc:  # noqa: BLE001 - どんな失敗でもJSONで返す(呼び出し元がstdoutをパースするため)
        output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0 if output.get("ok") else 1)


if __name__ == "__main__":
    main()
