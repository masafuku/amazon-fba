#!/usr/bin/env python3
"""
daily_scan.py — 毎朝1回実行するだけで完結するスクリプト。

処理の流れ:
  1. キーワードを決める(--keyword未指定ならKeyword/Categoryエージェントの
     キーワードプールから自動選択。プールが空ならKEYWORD_ROTATIONで曜日ごとに
     自動選択)
  2. keepa_mcp.server.find_arbitrage_candidates() でMCPの粗いスクリーニング
     (キーワード検索 + 価格差率・ランキング・レビュー数・価格変動で絞り込み)
  3. ops_finance.evaluate_mcp_candidates() でFBA手数料・国際送料込みの
     実質利益率を計算し、閾値未満を除外
  4. 結果をDBに保存する(agent_candidates / agent_runs)。即時のLINE通知は
     しない - CEOの希望で、1日の候補は朝8時・夜8時に send_daily_digest.py
     がまとめて1通ずつ通知する(スケジュール設定は send_daily_digest.py の
     docstring参照)。
  5. プールから選んだキーワードだった場合、使用実績(times_used等)を記録

  Keepaのトークンは低レート帯のプランだと1分に1トークン程度しか回復しない。
  デフォルトでは wait_for_tokens=True で実行するため、予算が足りない場面では
  即座に諦めるのではなく、トークンが貯まるのを待ちながら最後まで調査を
  進める(そのぶん実行時間は長くなる - 無人実行のcron向けの挙動)。
  対話的に手早く試したいときは --no-wait を付ける。

Keyword/Categoryエージェント(キーワードプールの管理):
  お気に入りに登録した商品(CEOが「良い」と判断した実績)や、Keepaの
  カテゴリツリー(関連カテゴリ・トップブランド)からキーワード候補を集めて
  プールに貯め、毎回のスキャンで使い回す。以下のコマンドはスキャンを実行
  せず、プールの更新のみ行って終了する:

    python3 daily_scan.py --seed-from-favorites
        お気に入り登録済み商品のブランド名・カテゴリ名を抽出してプールに追加
        (オフライン処理、Keepaトークン消費なし)

    python3 daily_scan.py --expand "G-Shock"
        指定したキーワードをKeepa Category Lookupで関連キーワード
        (サブカテゴリ名・関連カテゴリ名・トップブランド名)に拡張してプールに追加
        (Keepaトークンを消費する: 概算 1 + カテゴリ数 トークン)

    python3 daily_scan.py --list-keywords
        プールの内容(使用回数・最終使用日時・合格件数)を一覧表示

使い方:
    python3 daily_scan.py
    python3 daily_scan.py --keyword "kitchen gadget"
    python3 daily_scan.py --keyword "kitchen gadget" --category "Kitchen Utensils & Gadgets"
    python3 daily_scan.py --keyword "kitchen gadget" --max-candidates 20 --no-wait

キーワードのローテーションは KEYWORD_ROTATION を編集して調整する
(プールが空の場合のフォールバックとしてのみ使われる)。
毎朝 cron で実行する場合の例(平日7時に実行):
    0 7 * * 1-5 cd /path/to/amazon-fba && .venv/bin/python daily_scan.py >> logs/daily_scan.log 2>&1
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from keepa_mcp.keepa_client import KeepaError
from keepa_mcp.server import expand_keyword, find_arbitrage_candidates, search_category
from ops_finance import (
    add_keywords,
    evaluate_mcp_candidates,
    init_ops_tables,
    list_keyword_pool,
    log_agent_run,
    new_agent_run_id,
    persist_agent_run,
    pick_next_keyword,
    record_keyword_used,
    seed_keyword_pool_from_favorites,
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
    # price_min / require_amazon_out_of_stock / monthly_sold_peak_min は
    # find_arbitrage_candidates() 側のデフォルト(ダッシュボードの手動Finderと
    # 同じ「Amazon自体は在庫なし・3000円/ドル以上」レシピ)をそのまま使う。
    price_diff_min=0.30,   # MCP側の粗いフィルタ(実質利益率はここでは見ていない)
    # 価格変動フィルタは無効化(CEOの指示: 除外基準にはせず、結果の列として
    # 見えるだけにする)。price_volatility_90dは引き続き各候補に付与される。
    price_volatility_max=None,
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

    keyword_from_pool = False
    if keyword is None:
        keyword = pick_next_keyword()  # Keyword/Categoryエージェント: 未使用/最も久しく使っていないものを優先
        if keyword:
            keyword_from_pool = True
            print(f"[INFO] キーワードプールから選択: {keyword}")
        else:
            weekday = datetime.now(timezone.utc).weekday()
            keyword = KEYWORD_ROTATION.get(weekday, "kitchen gadget")
            print(f"[INFO] キーワードプールが空のため、曜日ローテーションを使用: {keyword}")
    else:
        print(f"[INFO] 指定されたキーワード: {keyword}")

    # エージェントページで「今まさに実行中」を表示できるよう、結果が出る前に
    # 一度status='running'で記録しておく(完了/失敗時に同じrun_idで更新される)。
    log_agent_run(
        run_id, started_at, time.monotonic() - start_time,
        keyword=keyword, category=category_name, category_id=category_id,
        max_candidates=max_candidates, wait_for_tokens=wait_for_tokens,
        status="running",
    )

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

    if keyword_from_pool:
        record_keyword_used(keyword, qualified_count=len(evaluation["qualified"]))

    # 即時のLINE通知はしない(CEOの希望: 1日の候補は朝8時・夜8時に
    # send_daily_digest.py がまとめて通知する)。ここでは結果をDBに
    # 保存するだけ。
    print(f"[INFO] 実質利益率{len(evaluation['qualified'])}件合格。ダイジェスト通知(8時/20時)でまとめて送信されます。")

    log_agent_run(
        run_id, started_at, time.monotonic() - start_time,
        keyword=keyword, category=category_name, category_id=category_id,
        max_candidates=max_candidates, wait_for_tokens=wait_for_tokens,
        mcp_result=mcp_result, evaluation=evaluation,
        notify_status="deferred_to_digest", notify_error=None,
    )


def cmd_seed_from_favorites() -> None:
    """お気に入りに登録済みの商品からブランド名・カテゴリ名を抽出し、キーワード
    プールに追加する(Keepa APIを一切呼ばないオフライン処理、トークン消費なし)。"""
    init_ops_tables()
    result = seed_keyword_pool_from_favorites()
    seeds = result.get("seeds_found", [])
    added = result.get("added", 0)
    print(f"[INFO] お気に入りから{len(seeds)}個のキーワード候補を抽出しました。")
    print(f"[INFO] 新規追加: {added}件 (既存キーワードは重複追加しません)")
    for kw in seeds:
        print(f"  - {kw}")


def cmd_expand(seed_keyword: str, max_categories: int) -> None:
    """1個のキーワードをKeepa Category Lookupで関連キーワードに拡張し、
    プールに追加する(Keepa APIを呼ぶのでトークンを消費する: 概算 1 + カテゴリ数)。"""
    init_ops_tables()
    print(f"[INFO] '{seed_keyword}' を関連キーワードに拡張中 (max_categories={max_categories}) ...")
    try:
        result = expand_keyword(keyword=seed_keyword, max_categories=max_categories)
    except KeepaError as exc:
        print(f"[ERROR] 拡張失敗: {exc}")
        sys.exit(1)
    if not result.get("matched_categories"):
        print(f"[WARN] '{seed_keyword}' に一致するKeepaカテゴリが見つかりませんでした。")
        return

    print(f"[INFO] 一致カテゴリ: {[c['name'] for c in result['matched_categories']]}")
    new_keywords = (
        result.get("subcategory_keywords", [])
        + result.get("related_category_keywords", [])
        + result.get("brand_keywords", [])
    )
    added = add_keywords(new_keywords, source="expanded", seed_keyword=seed_keyword)
    print(f"[INFO] サブカテゴリ: {result.get('subcategory_keywords', [])}")
    print(f"[INFO] 関連カテゴリ: {result.get('related_category_keywords', [])}")
    print(f"[INFO] ブランド: {result.get('brand_keywords', [])}")
    print(f"[INFO] キーワードプールに{added}件を新規追加しました(合計候補{len(new_keywords)}件、重複除く)。")


def cmd_list_keywords() -> None:
    init_ops_tables()
    rows = list_keyword_pool()
    if not rows:
        print("[INFO] キーワードプールは空です。--seed-from-favorites または --expand で追加してください。")
        return
    print(f"[INFO] キーワードプール ({len(rows)}件):")
    for row in rows:
        used = row.get("timesUsed", 0)
        qualified = row.get("totalQualified", 0)
        last_used = row.get("lastUsedAt") or "未使用"
        print(f"  - {row['keyword']:<30} source={row['source']:<10} "
              f"used={used:>3} qualified={qualified:>3} last_used={last_used}")


def main() -> None:
    parser = argparse.ArgumentParser(description="毎朝の候補スキャン + 実質利益率フィルタ + メール通知")
    parser.add_argument("--keyword", type=str, default=None,
                         help="検索キーワード(例: 'kitchen gadget')。省略時はキーワードプールから自動選択"
                              "(プールが空ならKEYWORD_ROTATIONで曜日ごとに自動選択)")
    parser.add_argument("--category", type=str, default=None, help="Keepaカテゴリ名で追加絞り込み(任意)")
    parser.add_argument("--category-id", type=int, default=None, help="Keepaカテゴリ ID を直接指定(--category より優先)")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_SEARCH_PARAMS["max_candidates"],
                         help="評価するASIN数の上限(Keepaトークン消費に直結)")
    parser.add_argument("--no-wait", dest="wait_for_tokens", action="store_false",
                         help="トークン不足時に待たず、その時点までの結果で打ち切る(デフォルトは待つ)")
    parser.set_defaults(wait_for_tokens=True)

    # --- Keyword/Categoryエージェント: キーワードプールの管理コマンド ---
    # いずれかが指定された場合は通常のスキャンを実行せず、プールの更新のみ行って終了する。
    parser.add_argument("--seed-from-favorites", action="store_true",
                         help="お気に入りの商品からブランド/カテゴリ名を抽出してキーワードプールに追加する"
                              "(オフライン処理、トークン消費なし)")
    parser.add_argument("--expand", type=str, default=None, metavar="KEYWORD",
                         help="指定したキーワードをKeepa Category Lookupで関連キーワードに拡張し、"
                              "プールに追加する(トークンを消費する)")
    parser.add_argument("--max-categories", type=int, default=3,
                         help="--expand で見る一致カテゴリ数の上限(デフォルト3、多いほどトークン消費増)")
    parser.add_argument("--list-keywords", action="store_true",
                         help="キーワードプールの内容を表示して終了する")

    args = parser.parse_args()

    if args.list_keywords:
        cmd_list_keywords()
        return
    if args.seed_from_favorites:
        cmd_seed_from_favorites()
        return
    if args.expand:
        cmd_expand(args.expand, args.max_categories)
        return

    run_daily_scan(
        keyword=args.keyword,
        category_name=args.category,
        category_id=args.category_id,
        max_candidates=args.max_candidates,
        wait_for_tokens=args.wait_for_tokens,
    )


if __name__ == "__main__":
    main()
