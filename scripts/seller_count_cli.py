#!/usr/bin/env python3
"""
seller_count_cli.py — sqlite_api_server.py(素のpython3、依存なし)から
keepa_mcp(mcpパッケージが要る、.venv/bin/pythonが必須)を呼ぶための
サブプロセス橋渡し。scripts/asin_lookup_cli.py と同じ設計。

CEO: 「候補商品に対して、セラーの数、在庫、売れてる数、を取得できますか？」
「すべての商品ではなく、有力候補のみ。」「選択的にバックフィルをしたい。」
— 新規の合格候補はパイプライン内で自動的にセラー数を取得するが、過去の
合格候補はCandidateDetailPage.jsxの「セラー数を取得」ボタンから、CEOが
気になったものだけ個別に取得する。ブラウザからのリクエストを長時間
ブロックしないよう wait_for_tokens=False 固定で実行する。

標準出力にJSON1行だけを出す(sqlite_api_server.py側でパースされる)。

使い方:
    .venv/bin/python scripts/seller_count_cli.py fetch --asin B0XXXXXXXX [--domain US]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# scripts/ 配下から起動されるため、リポジトリルートを明示的にsys.pathへ追加する
# (seller_mine_cli.py と同じ理由)。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keepa_mcp.keepa_client import KeepaError
from keepa_mcp.server import enrich_qualified_candidates_with_seller_count


def cmd_fetch(args: argparse.Namespace) -> dict:
    entry = {"asin": args.asin}
    try:
        result = enrich_qualified_candidates_with_seller_count(
            [entry], domain=args.domain, wait_for_tokens=False,
        )
    except KeepaError as exc:
        return {"ok": False, "error": str(exc)}

    count = entry.get("competitor_seller_count")
    if result["failed"] and count is None:
        return {"ok": False, "error": f"ASIN {args.asin} のセラー数を取得できませんでした。"}

    return {"ok": True, "asin": args.asin, "competitorSellerCount": count}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="指定した1つのASINの競合セラー数を取得する")
    fetch.add_argument("--asin", required=True)
    fetch.add_argument("--domain", default="US")
    fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args()
    try:
        output = args.func(args)
    except Exception as exc:  # noqa: BLE001 - どんな失敗でもJSONで返す(呼び出し元がstdoutをパースするため)
        output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0 if output.get("ok") else 1)


if __name__ == "__main__":
    main()
