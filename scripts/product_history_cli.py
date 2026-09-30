#!/usr/bin/env python3
"""
product_history_cli.py — sqlite_api_server.py(素のpython3、依存なし)から
keepa_mcp(mcpパッケージが要る、.venv/bin/pythonが必須)を呼ぶための
サブプロセス橋渡し。scripts/seller_mine_cli.pyと同じ設計。

CandidateDetailPage(合格商品の詳細ページ)の「詳細データ(価格・ランキング履歴)
を取得」ボタンから呼ばれる想定。CEOの明示的な指示により、これは常にユーザーの
ボタンクリックでのみ呼ばれ、自動では絶対に呼ばれない。ブラウザからのリクエスト
を長時間ブロックしないよう wait_for_tokens は使わない(get_product_history()は
単一ASINの1回きりのfetchなので、そもそもトークン待ちのポーリングは不要)。

標準出力にJSON1行だけを出す(sqlite_api_server.py側でパースされる)。

使い方:
    .venv/bin/python scripts/product_history_cli.py fetch-history --asin B0XXXXXXXX [--domain US]
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
from keepa_mcp.server import get_product_history


def cmd_fetch_history(args: argparse.Namespace) -> dict:
    try:
        result = get_product_history(asin=args.asin, domain=args.domain)
    except KeepaError as exc:
        return {"ok": False, "error": str(exc)}

    if not result.get("found"):
        return {
            "ok": True, "asin": args.asin, "found": False,
            "error": result.get("error") or result.get("note"),
        }

    return {
        "ok": True,
        "asin": args.asin,
        "found": True,
        "domain": result.get("domain"),
        "priceHistory": result.get("price_history"),
        "rankHistory": result.get("rank_history"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch-history", help="1商品の価格・ランキング時系列をKeepaから取得する")
    fetch.add_argument("--asin", required=True)
    fetch.add_argument("--domain", default="US")
    fetch.set_defaults(func=cmd_fetch_history)

    args = parser.parse_args()
    try:
        output = args.func(args)
    except Exception as exc:  # noqa: BLE001 - どんな失敗でもJSONで返す(呼び出し元がstdoutをパースするため)
        output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0 if output.get("ok") else 1)


if __name__ == "__main__":
    main()
