#!/usr/bin/env python3
"""
seller_count_cli.py — sqlite_api_server.py(素のpython3、依存なし)から
keepa_mcp(mcpパッケージが要る、.venv/bin/pythonが必須)を呼ぶための
サブプロセス橋渡し。scripts/asin_lookup_cli.py と同じ設計。

CEO: 「候補商品に対して、セラーの数、在庫、売れてる数、を取得できますか？」
「すべての商品ではなく、有力候補のみ。」「選択的にバックフィルをしたい。」
「セラー数が38となっていますが、keepaで直接見た値と明らかに違います。調査
おねがいします。」「セラー数をを埋めるだけでなく、在庫の数...も取得して
表示してください。」

`fetch` サブコマンド: セラー数の再計算(v2 - 通常の商品取得1トークンのみ、
offers=N不要。以前はoffers配列の水増しバグで誤った値になっていたため、
既存の値の有無に関わらず「再取得」ボタンから常に呼べる)。
`fetch-stock` サブコマンド: 在庫合計の取得(offers=N&stock=1、約10トークン -
こちらは新規のトークン消費が発生するため、値が無い候補にのみボタンを出す想定)。

標準出力にJSON1行だけを出す(sqlite_api_server.py側でパースされる)。

使い方:
    .venv/bin/python scripts/seller_count_cli.py fetch --asin B0XXXXXXXX [--domain US]
    .venv/bin/python scripts/seller_count_cli.py fetch-stock --asin B0XXXXXXXX [--domain US]
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
from keepa_mcp.server import enrich_qualified_candidates_with_offer_details, recompute_seller_count


def cmd_fetch(args: argparse.Namespace) -> dict:
    try:
        count = recompute_seller_count(args.asin, domain=args.domain)
    except KeepaError as exc:
        return {"ok": False, "error": str(exc)}

    if count is None:
        return {"ok": False, "error": f"ASIN {args.asin} のセラー数を取得できませんでした。"}

    return {"ok": True, "asin": args.asin, "competitorSellerCount": count}


def cmd_fetch_stock(args: argparse.Namespace) -> dict:
    entry = {"asin": args.asin}
    try:
        result = enrich_qualified_candidates_with_offer_details(
            [entry], domain=args.domain, wait_for_tokens=False,
        )
    except KeepaError as exc:
        return {"ok": False, "error": str(exc)}

    stock = entry.get("competitor_stock_total")
    if result["failed"] and stock is None:
        return {"ok": False, "error": f"ASIN {args.asin} の在庫を取得できませんでした。"}

    return {"ok": True, "asin": args.asin, "competitorStockTotal": stock}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="指定した1つのASINの競合セラー数を再計算する(通常の商品取得のみ、追加コストなし)")
    fetch.add_argument("--asin", required=True)
    fetch.add_argument("--domain", default="US")
    fetch.set_defaults(func=cmd_fetch)

    fetch_stock = subparsers.add_parser("fetch-stock", help="指定した1つのASINの競合在庫合計を取得する(offers+stock、追加トークンあり)")
    fetch_stock.add_argument("--asin", required=True)
    fetch_stock.add_argument("--domain", default="US")
    fetch_stock.set_defaults(func=cmd_fetch_stock)

    args = parser.parse_args()
    try:
        output = args.func(args)
    except Exception as exc:  # noqa: BLE001 - どんな失敗でもJSONで返す(呼び出し元がstdoutをパースするため)
        output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0 if output.get("ok") else 1)


if __name__ == "__main__":
    main()
