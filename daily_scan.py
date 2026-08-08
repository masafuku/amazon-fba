#!/usr/bin/env python3
"""
daily_scan.py — 毎朝1回実行するだけで完結するスクリプト。

処理の流れ:
  1. keepa_mcp.server.find_arbitrage_candidates() でMCPの粗いスクリーニング
     (価格差率・ランキング・レビュー数・価格変動で絞り込み)
  2. ops_finance.evaluate_mcp_candidates() でFBA手数料・国際送料込みの
     実質利益率を計算し、閾値未満を除外
  3. 合格した候補を LINE に通知(notify_line.py を再利用)
  4. 予算アラート(check_budget_alert)もあわせて通知

使い方:
    python3 daily_scan.py
    python3 daily_scan.py --category "Kitchen Utensils & Gadgets"
    python3 daily_scan.py --category-id 12345 --max-candidates 20

カテゴリのローテーションは CATEGORY_ROTATION を編集して調整する。
毎朝 cron で実行する場合の例(平日7時に実行):
    0 7 * * 1-5 cd /path/to/amazon-fba && .venv/bin/python daily_scan.py >> logs/daily_scan.log 2>&1
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from keepa_mcp.server import find_arbitrage_candidates, search_category
from ops_finance import (
    build_qualified_line_message,
    check_budget_alert,
    evaluate_mcp_candidates,
    init_ops_tables,
)
from notify_line import send_line_notify

# ---------------------------------------------------------------------------
# 曜日ごとのカテゴリローテーション(必要に応じて編集)
# キーは月曜=0 〜 日曜=6
# ---------------------------------------------------------------------------
CATEGORY_ROTATION = {
    0: "Kitchen & Dining",
    1: "Pet Supplies",
    2: "Office Products",
    3: "Home & Kitchen",
    4: "Toys & Games",
    5: "Sports & Outdoors",
    6: "Beauty & Personal Care",
}

DEFAULT_SEARCH_PARAMS = dict(
    sales_rank_min=1000,
    sales_rank_max=20000,
    review_count_max=200,
    price_diff_min=0.30,   # MCP側の粗いフィルタ(実質利益率はここでは見ていない)
    price_volatility_max=0.20,
    max_candidates=30,
)


def resolve_category_id(category_name: str) -> int | None:
    result = search_category(term=category_name)
    matches = result.get("categories") or []
    if not matches:
        print(f"[WARN] カテゴリ '{category_name}' が見つかりませんでした。")
        return None
    # 最初の一致を採用(必要ならここでスコアリングに変更)
    return matches[0]["category_id"]


def run_daily_scan(category_name: str | None, category_id: int | None, max_candidates: int) -> None:
    init_ops_tables()

    if category_id is None:
        weekday = datetime.now(timezone.utc).weekday()
        category_name = category_name or CATEGORY_ROTATION.get(weekday, "Home & Kitchen")
        print(f"[INFO] 本日のカテゴリ: {category_name}")
        category_id = resolve_category_id(category_name)
        if category_id is None:
            sys.exit(1)

    print(f"[INFO] Keepa MCP で候補検索中 (category_id={category_id}, max_candidates={max_candidates}) ...")
    search_params = dict(DEFAULT_SEARCH_PARAMS)
    search_params["max_candidates"] = max_candidates
    mcp_result = find_arbitrage_candidates(category_id=category_id, **search_params)

    if mcp_result.get("error"):
        print(f"[ERROR] MCP検索失敗: {mcp_result['error']}")
        sys.exit(1)

    print(f"[INFO] MCP評価対象: {mcp_result.get('evaluated', 0)}件 / "
          f"粗フィルタ通過: {mcp_result.get('matched', 0)}件 / "
          f"スキップ: {len(mcp_result.get('skipped', []))}件")

    print("[INFO] 実質利益率(FBA手数料・国際送料込み)でフィルタ中 ...")
    evaluation = evaluate_mcp_candidates(mcp_result)

    print(f"[INFO] 実質利益率20%以上: {len(evaluation['qualified'])}件 / "
          f"却下: {len(evaluation['rejected'])}件")
    if evaluation["weight_missing"]:
        print(f"[WARN] 重量データなし(仮値で計算): {evaluation['weight_missing']}")

    # 候補の通知
    candidate_message = build_qualified_line_message(evaluation)
    full_message = f"【本日の候補: {category_name}】\n{candidate_message}"

    # 予算アラートも合わせて通知
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    budget_alerts = check_budget_alert(this_month)
    if budget_alerts:
        full_message += "\n\n【予算アラート】\n" + "\n".join(budget_alerts)

    line_token = os.getenv("LINE_NOTIFY_TOKEN", "").strip()
    if line_token:
        try:
            send_line_notify(line_token, full_message)
            print("[INFO] LINE通知を送信しました。")
        except Exception as exc:
            print(f"[ERROR] LINE通知送信失敗: {exc}")
    else:
        print("[WARN] LINE_NOTIFY_TOKEN が未設定のため、通知はスキップしました。")
        print("--- 通知予定だった内容 ---")
        print(full_message)


def main() -> None:
    parser = argparse.ArgumentParser(description="毎朝の候補スキャン + 実質利益率フィルタ + LINE通知")
    parser.add_argument("--category", type=str, default=None, help="Keepaカテゴリ名(例: 'Kitchen Utensils & Gadgets')")
    parser.add_argument("--category-id", type=int, default=None, help="Keepaカテゴリ ID を直接指定(--category より優先)")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_SEARCH_PARAMS["max_candidates"],
                         help="評価するASIN数の上限(Keepaトークン消費に直結)")
    args = parser.parse_args()

    run_daily_scan(category_name=args.category, category_id=args.category_id, max_candidates=args.max_candidates)


if __name__ == "__main__":
    main()
