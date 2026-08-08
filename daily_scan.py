#!/usr/bin/env python3
"""
daily_scan.py — 毎朝1回実行するだけで完結するスクリプト。

処理の流れ:
  1. キーワードを決める(KEYWORD_ROTATIONで曜日ごとに自動選択、または--keywordで指定)
  2. keepa_mcp.server.find_arbitrage_candidates() でMCPの粗いスクリーニング
     (キーワード検索 + 価格差率・ランキング・レビュー数・価格変動で絞り込み)
  3. ops_finance.evaluate_mcp_candidates() でFBA手数料・国際送料込みの
     実質利益率を計算し、閾値未満を除外
  4. 合格した候補をメールに通知(notify_email.py を再利用)
  5. 予算アラート(check_budget_alert)もあわせて通知

  注: 当初は notify_line.py (LINE Notify) を使う想定だったが、LINE Notify は
  2025年3月末でサービス終了済み(notify-api.line.me は名前解決すら不可)の
  ため、既存の notify_email.py に切り替えている。

  Keepaのトークンは低レート帯のプランだと1分に1トークン程度しか回復しない。
  デフォルトでは wait_for_tokens=True で実行するため、予算が足りない場面では
  即座に諦めるのではなく、トークンが貯まるのを待ちながら最後まで調査を
  進める(そのぶん実行時間は長くなる - 無人実行のcron向けの挙動)。
  対話的に手早く試したいときは --no-wait を付ける。

使い方:
    python3 daily_scan.py
    python3 daily_scan.py --keyword "kitchen gadget"
    python3 daily_scan.py --keyword "kitchen gadget" --category "Kitchen Utensils & Gadgets"
    python3 daily_scan.py --keyword "kitchen gadget" --max-candidates 20 --no-wait

キーワードのローテーションは KEYWORD_ROTATION を編集して調整する。
毎朝 cron で実行する場合の例(平日7時に実行):
    0 7 * * 1-5 cd /path/to/amazon-fba && .venv/bin/python daily_scan.py >> logs/daily_scan.log 2>&1
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from config import Settings
from keepa_mcp.server import find_arbitrage_candidates, search_category
from notify_email import send_email
from ops_finance import (
    build_qualified_line_message,
    check_budget_alert,
    evaluate_mcp_candidates,
    init_ops_tables,
    log_agent_run,
    new_agent_run_id,
    persist_agent_run,
)

# ---------------------------------------------------------------------------
# 曜日ごとのキーワードローテーション(必要に応じて編集)
# キーは月曜=0 〜 日曜=6
# ---------------------------------------------------------------------------
KEYWORD_ROTATION = {
    0: "kitchen gadget",
    1: "pet supply",
    2: "office organizer",
    3: "home storage",
    4: "toy game",
    5: "outdoor gear",
    6: "beauty tool",
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


def run_daily_scan(
    keyword: str | None,
    category_name: str | None,
    category_id: int | None,
    max_candidates: int,
    wait_for_tokens: bool,
) -> None:
    init_ops_tables()

    run_id = new_agent_run_id()
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()

    if keyword is None:
        weekday = datetime.now(timezone.utc).weekday()
        keyword = KEYWORD_ROTATION.get(weekday, "kitchen gadget")
    print(f"[INFO] 本日のキーワード: {keyword}")

    if category_id is None and category_name:
        category_id = resolve_category_id(category_name)
        if category_id is None:
            log_agent_run(
                run_id, started_at, time.monotonic() - start_time,
                keyword=keyword, category=category_name, category_id=None,
                max_candidates=max_candidates, wait_for_tokens=wait_for_tokens,
                error=f"カテゴリ '{category_name}' が見つかりませんでした。",
            )
            sys.exit(1)

    label = f"{keyword}" + (f" / {category_name}" if category_name else "")
    print(f"[INFO] Keepa MCP で候補検索中 (keyword={keyword!r}, category_id={category_id}, "
          f"max_candidates={max_candidates}, wait_for_tokens={wait_for_tokens}) ...")
    search_params = dict(DEFAULT_SEARCH_PARAMS)
    search_params["max_candidates"] = max_candidates
    mcp_result = find_arbitrage_candidates(
        keyword=keyword, category_id=category_id, wait_for_tokens=wait_for_tokens, **search_params
    )

    if mcp_result.get("error"):
        print(f"[ERROR] MCP検索失敗: {mcp_result['error']}")
        log_agent_run(
            run_id, started_at, time.monotonic() - start_time,
            keyword=keyword, category=category_name, category_id=category_id,
            max_candidates=max_candidates, wait_for_tokens=wait_for_tokens,
            mcp_result=mcp_result, error=mcp_result["error"],
        )
        sys.exit(1)

    print(f"[INFO] MCP評価対象: {mcp_result.get('evaluated', 0)}件 / "
          f"粗フィルタ通過: {mcp_result.get('matched', 0)}件 / "
          f"スキップ: {len(mcp_result.get('skipped', []))}件")
    if mcp_result.get("stopped_early_for_tokens"):
        print("[WARN] トークン予算が尽きたため、一部の候補は評価しきれずに打ち切りました"
              "(--wait-for-tokens を付けると貯まるまで待って最後まで進めます)。")

    print("[INFO] 実質利益率(FBA手数料・国際送料込み)でフィルタ中 ...")
    evaluation = evaluate_mcp_candidates(mcp_result)

    print(f"[INFO] 実質利益率20%以上: {len(evaluation['qualified'])}件 / "
          f"却下: {len(evaluation['rejected'])}件")
    if evaluation["weight_missing"]:
        print(f"[WARN] 重量データなし(仮値で計算): {evaluation['weight_missing']}")
    if evaluation["fee_missing"]:
        print(f"[WARN] 手数料データなし(仮値で計算): {evaluation['fee_missing']}")

    persist_agent_run(label, evaluation, run_id=run_id)
    print(f"[INFO] 「エージェント」ページ用に保存しました (run_id={run_id})。ダッシュボードで確認できます。")

    # 候補の通知
    candidate_message = build_qualified_line_message(evaluation)
    full_message = f"【本日の候補: {label}】\n{candidate_message}"

    # 予算アラートも合わせて通知
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    budget_alerts = check_budget_alert(this_month)
    if budget_alerts:
        full_message += "\n\n【予算アラート】\n" + "\n".join(budget_alerts)

    notify_status = "skipped"
    notify_error = None
    settings = Settings.load()
    if settings.email_smtp_host and settings.email_username and settings.email_to:
        try:
            send_email(
                smtp_host=settings.email_smtp_host,
                smtp_port=settings.email_smtp_port,
                username=settings.email_username,
                password=settings.email_password,
                from_addr=settings.email_from,
                to_addr=settings.email_to,
                subject=f"【本日の候補】{label}",
                body=full_message,
            )
            print(f"[INFO] メール通知を送信しました ({settings.email_to})。")
            notify_status = "sent"
        except Exception as exc:
            print(f"[ERROR] メール通知送信失敗: {exc}")
            notify_status = "failed"
            notify_error = str(exc)
    else:
        print("[WARN] EMAIL_* が未設定のため、通知はスキップしました。")
        print("--- 通知予定だった内容 ---")
        print(full_message)

    log_agent_run(
        run_id, started_at, time.monotonic() - start_time,
        keyword=keyword, category=category_name, category_id=category_id,
        max_candidates=max_candidates, wait_for_tokens=wait_for_tokens,
        mcp_result=mcp_result, evaluation=evaluation,
        notify_status=notify_status, notify_error=notify_error,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="毎朝の候補スキャン + 実質利益率フィルタ + メール通知")
    parser.add_argument("--keyword", type=str, default=None,
                         help="検索キーワード(例: 'kitchen gadget')。省略時はKEYWORD_ROTATIONから曜日で自動選択")
    parser.add_argument("--category", type=str, default=None, help="Keepaカテゴリ名で追加絞り込み(任意)")
    parser.add_argument("--category-id", type=int, default=None, help="Keepaカテゴリ ID を直接指定(--category より優先)")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_SEARCH_PARAMS["max_candidates"],
                         help="評価するASIN数の上限(Keepaトークン消費に直結)")
    parser.add_argument("--no-wait", dest="wait_for_tokens", action="store_false",
                         help="トークン不足時に待たず、その時点までの結果で打ち切る(デフォルトは待つ)")
    parser.set_defaults(wait_for_tokens=True)
    args = parser.parse_args()

    run_daily_scan(
        keyword=args.keyword,
        category_name=args.category,
        category_id=args.category_id,
        max_candidates=args.max_candidates,
        wait_for_tokens=args.wait_for_tokens,
    )


if __name__ == "__main__":
    main()
