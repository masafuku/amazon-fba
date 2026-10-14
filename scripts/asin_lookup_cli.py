#!/usr/bin/env python3
"""
asin_lookup_cli.py — sqlite_api_server.py(素のpython3、依存なし)から
keepa_mcp(mcpパッケージが要る、.venv/bin/pythonが必須)を呼ぶための
サブプロセス橋渡し。scripts/seller_mine_cli.pyと同じ設計。

CEO: 「ASIN指定で調査する入力UIを追加できますか？」— ダッシュボードの
「ASIN指定調査」入力欄(AgentPage.jsx)、および未調査ASINの詳細ページ
(CandidateDetailPage.jsx)の「このASINを調査する」ボタンから呼ばれる想定。
ブラウザからのリクエストを長時間ブロックしないよう wait_for_tokens=False
固定で実行する。

標準出力にJSON1行だけを出す(sqlite_api_server.py側でパースされる)。

使い方:
    .venv/bin/python scripts/asin_lookup_cli.py lookup --asin B0XXXXXXXX [--domain US]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# scripts/ 配下から起動されるため、リポジトリルートを明示的にsys.pathへ追加する
# (seller_mine_cli.py と同じ理由)。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keepa_mcp.keepa_client import KeepaError
from keepa_mcp.server import investigate_asin
from ops_finance import (
    evaluate_mcp_candidates,
    init_ops_tables,
    log_agent_run,
    new_agent_run_id,
    persist_agent_run,
)


def cmd_lookup(args: argparse.Namespace) -> dict:
    init_ops_tables()
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        result = investigate_asin(asin=args.asin, sell_domain=args.domain, wait_for_tokens=False)
    except KeepaError as exc:
        return {"ok": False, "error": str(exc)}

    if result.get("found") is False:
        return {"ok": False, "error": result.get("error") or f"ASIN {args.asin} が見つかりませんでした。"}

    evaluation = evaluate_mcp_candidates(result)
    run_id = new_agent_run_id()
    label = "ASIN指定調査"

    if evaluation["qualified"] or evaluation["rejected"]:
        persist_agent_run(
            label, evaluation, run_id=run_id,
            source_type="manual_asin", seed_asin=args.asin,
        )
    log_agent_run(
        run_id, started_at, 0,
        keyword=f"[asin] {args.asin}", category=label,
        max_candidates=1, wait_for_tokens=False,
        mcp_result=result, evaluation=evaluation,
        notify_status="deferred_to_digest",
        source_type="manual_asin", seed_asin=args.asin,
    )

    entry = (evaluation["qualified"] or evaluation["rejected"] or [None])[0]
    if entry is None:
        # US側の価格すら取れなかった(在庫切れ等)場合。それでも実行履歴には
        # 記録済みなので、UI側には「見つからなかった」ではなく理由付きで返す。
        return {"ok": False, "error": f"ASIN {args.asin} の価格データを取得できませんでした。"}

    return {
        "ok": True,
        "runId": run_id,
        "asin": args.asin,
        "tier": entry.get("tier"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    lookup = subparsers.add_parser("lookup", help="指定した1つのASINを実質利益率パイプラインで評価する")
    lookup.add_argument("--asin", required=True)
    lookup.add_argument("--domain", default="US")
    lookup.set_defaults(func=cmd_lookup)

    args = parser.parse_args()
    try:
        output = args.func(args)
    except Exception as exc:  # noqa: BLE001 - どんな失敗でもJSONで返す(呼び出し元がstdoutをパースするため)
        output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0 if output.get("ok") else 1)


if __name__ == "__main__":
    main()
