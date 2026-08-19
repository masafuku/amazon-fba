#!/usr/bin/env python3
"""
netsea_sourcing_cli.py — netsea_sourcing.py(mcpパッケージが要る、
.venv/bin/pythonが必須)を実行するCLI。scripts/seller_mine_cli.py と
同じ設計(サブプロセス橋渡し、標準出力にJSON1行だけ)。

CEO: 「Amazon以外にネット系の卸売り業者からの仕入れも考えたほうがよいと考えます。
自動で行いたい」「GTINやJANなどでSKUの完全一致確認をする方法を提案してください」

使い方:
    .venv/bin/python scripts/netsea_sourcing_cli.py run \\
        --category 20110:文具 --category 121201:釣具ルアー
    .venv/bin/python scripts/netsea_sourcing_cli.py run --category 20126:キッチン --price-min 500 --price-max 5000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# scripts/ 配下から起動されるため、リポジトリルートを明示的にsys.pathへ追加する
# (seller_mine_cli.py と同じ理由)。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings
from keepa_mcp.config import settings as keepa_settings
from netsea_client import NetseaError
from netsea_sourcing import run_netsea_sourcing_cycle


def _parse_category_arg(raw: str) -> tuple[str, str]:
    """--category '20110:文具' -> ('20110', '文具')。ラベル省略時はIDをそのまま使う。"""
    if ":" in raw:
        category_id, label = raw.split(":", 1)
        return category_id.strip(), label.strip()
    return raw.strip(), raw.strip()


def cmd_run(args: argparse.Namespace) -> dict:
    root_settings = Settings.load()
    if not root_settings.netsea_access_token:
        return {"ok": False, "error": "NETSEA_ACCESS_TOKEN が未設定です(.envを確認してください)。"}
    if not keepa_settings.keepa_api_key:
        return {"ok": False, "error": "KEEPA_API_KEY が未設定です(.envを確認してください)。"}

    pairs = [_parse_category_arg(c) for c in args.category]
    category_ids = [cid for cid, _label in pairs]
    category_labels = dict(pairs)

    try:
        result = run_netsea_sourcing_cycle(
            api_key=keepa_settings.keepa_api_key,
            netsea_token=root_settings.netsea_access_token,
            category_ids=category_ids,
            category_labels=category_labels,
            price_range_from=args.price_min,
            price_range_to=args.price_max,
            wait_for_tokens=args.wait_for_tokens,
        )
    except NetseaError as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, **result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="指定カテゴリーのNETSEA商品をJANコード完全一致でAmazon USと突き合わせ、実質利益率パイプラインで評価する")
    run.add_argument(
        "--category", action="append", required=True,
        help="NETSEAカテゴリーID(任意で ':ラベル' を付与、例: 20110:文具)。複数指定可。",
    )
    run.add_argument("--price-min", type=int, default=None, help="卸価格(税抜、円)の下限")
    run.add_argument("--price-max", type=int, default=None, help="卸価格(税抜、円)の上限")
    run.add_argument(
        "--no-wait", dest="wait_for_tokens", action="store_false",
        help="Keepaトークン不足時に待たずスキップする(既定は待つ - daily_scan.pyと同じ)",
    )
    run.set_defaults(func=cmd_run, wait_for_tokens=True)

    args = parser.parse_args()
    try:
        output = args.func(args)
    except Exception as exc:  # noqa: BLE001 - どんな失敗でもJSONで返す(呼び出し元がstdoutをパースするため)
        output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0 if output.get("ok") else 1)


if __name__ == "__main__":
    main()
