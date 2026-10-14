#!/usr/bin/env python3
"""
add_manual_supplier.py — 手動(Claudeがブラウザ等で調べた結果)で見つけた仕入れ先を、
指定ASINに記録する。NETSEA自動連携(scripts/sync_netsea_catalog.py / check_candidate_wholesale.py)
とは別の一覧(data_json['manual_suppliers'])に追加し、価格が今の原価より安ければ、
利益・ROI・優先度Tierを自動で再計算する(ops_finance.add_manual_supplier)。

CEO(2026-09-27): 「SDなど、複数の仕入れ先が見つかった場合、表示方法は？」→「全社を並べて
表示」「NETSEAは自動で埋めてください。SDは自動では埋まらないので、claudeが記録する」
「手動で仕入れ先を追加したとき、利益・ROIも自動で再計算する」。

使い方:
    .venv/bin/python scripts/add_manual_supplier.py B0012ORKN8 --source sd --shop "ハリマ共和物産" \\
        --price 276 --url "https://www.superdelivery.com/p/r/pd_p/5067075/" --min-qty 1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops_finance import add_manual_supplier  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("asin", help="候補のASIN")
    parser.add_argument("--source", required=True, help="仕入れ先の種類(例: sd, netsea, other)")
    parser.add_argument("--shop", required=True, help="仕入れ先(出展企業・ショップ)名")
    parser.add_argument("--price", required=True, type=float, help="卸価格(円、税抜)")
    parser.add_argument("--url", default=None, help="商品ページのURL")
    parser.add_argument("--min-qty", type=int, default=None, help="最小ロット(個)")
    parser.add_argument("--note", default=None, help="補足メモ")
    args = parser.parse_args()

    result = add_manual_supplier(
        args.asin, args.source, args.shop, args.price,
        url=args.url, min_qty=args.min_qty, note=args.note,
    )
    if not result["matched"]:
        print(f"[WARN] {args.asin} は候補として見つかりませんでした(0行を更新)。")
        return 1

    print(f"[INFO] {args.asin} に「{args.shop}」(¥{args.price:.0f}) を記録しました({result['rows_updated']}行更新)。")
    if result["recalculated"]:
        print(
            f"[INFO] 原価が下がったため再計算しました: "
            f"ROI {result.get('roi_before')} -> {result.get('roi_after')} / "
            f"Tier {result.get('tier_before')} -> {result.get('tier_after')}"
        )
    else:
        print("[INFO] 既存の原価より安くならなかったため、利益・ROI・Tierは変更していません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
