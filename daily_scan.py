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

  セラーマイニング(CEOのアイデア: 「よく売れている日本のものを売っている
  セラーは、他にも同じようなものを売っていることが多い」)は、この日中の
  サイクルでは行わない(CEO: 「優良が見つかってもその場では検索せず。
  セラーサーチは夜間だけにする」2026-09-27)。夜間のセラーマイニング枠
  (--seller-mining、run_seller_mining_cycle)が、この日中のサイクルで
  見つかった合格候補を後追いで拾い、セラーの発見(discover_sellers_for_
  pending_candidates)とマイニング(keepa_mcp.server.find_other_sellers_
  for_candidate / expand_from_seller)の両方を行う。

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

Sellerエージェント(セラープールの管理・定期セラーマイニング):
  合格候補から自動発見したセラー・ダッシュボードから手動発見したセラーは
  すべて seller_pool に貯まる。以下のコマンドで、キーワード検索の代わりに
  セラープールから1件選んでマイニングできる(run_all_day.shが深夜の時間帯に
  自動的に呼ぶ想定):

    python3 daily_scan.py --seller-mining
        セラープールから次の1件(最も調査回数が少ない/久しく調べていないもの)
        を選び、その出品を評価する(Keepaトークンを消費する)

    python3 daily_scan.py --list-sellers
        セラープールの内容(調査回数・最終調査日時・合格件数)を一覧表示

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
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from keepa_mcp.keepa_client import KeepaError
from keepa_mcp.server import (
    enrich_qualified_candidates_with_offer_details,
    expand_from_seller,
    expand_keyword,
    find_arbitrage_candidates,
    find_other_sellers_for_candidate,
    search_category,
)
from ops_finance import (
    DB_PATH,
    PRIORITY_TIER_SQL_ORDER,
    add_keywords,
    add_sellers,
    evaluate_mcp_candidates,
    init_ops_tables,
    list_keyword_pool,
    list_seller_pool,
    log_agent_run,
    new_agent_run_id,
    persist_agent_run,
    pick_next_keyword,
    pick_next_seller,
    record_keyword_used,
    record_seller_mined,
    seed_keyword_pool_from_favorites,
    set_seller_status,
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
    # require_amazon_out_of_stock / monthly_sold_peak_min は
    # find_arbitrage_candidates() 側のデフォルト(ダッシュボードの手動Finderと
    # 同じ「Amazon自体は在庫なし」レシピ)をそのまま使う。
    # price_min/price_maxはCEOの指示(2026-09-07: 「デフォルトでは30ドル以下を
    # 探すようにして欲しい」)でここを$30以下に上書きする。find_arbitrage_
    # candidates()自体のデフォルトはprice_min=3000($30)のみでprice_max無し
    # (=$30以上しか見ない)だったため、文具女子アワード/JetPens受賞歴等の
    # $3〜27程度の候補が軒並み検索対象外になっていた(2026-09-06に判明)。
    price_min=None,
    price_max=3000,
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


# セラーマイニングで追加評価する出品数の上限(通常検索の12より控えめに
# して、1回あたりのトークン消費を予測可能な範囲に収める)。
SELLER_EXPANSION_MAX_CANDIDATES = 10


# 1件の合格候補から芋づる式に「登録」するセラー数の上限。
# 実質、無制限にする値(offers取得は1候補につき1回のKeepa呼び出しで完結し、
# そこから何件のセラーIDを取り出すかはPython側のリスト切り詰めでしかない
# ため、上限を上げても追加のトークンは一切かからない - CEO: 「候補セラーを
# 全て検討候補にいれるのはどう？」2026-09-27。トークンを消費するのは
# 「マイニング」(_expand_from_one_seller)の方なので、そちらの回数は別途絞る)。
DISCOVER_SELLERS_PER_CANDIDATE = 50

# 1回の夜間サイクルで、「まだセラーを発見していない、日中に見つかった
# 合格候補」の発見処理を行う件数の上限(discover_sellers_for_pending_candidates)。
NIGHTLY_SELLER_DISCOVERY_LIMIT = 5

# discover_sellers_for_pending_candidates が対象にする、候補の新しさの上限。
NIGHTLY_SELLER_DISCOVERY_MAX_AGE_HOURS = 48


# 同じセラーをこの回数以上マイニング済みなら「深掘り」する(通常の
# SELLER_EXPANSION_MAX_CANDIDATESではなくSELLER_DEEP_REMINE_MAX_CANDIDATESを
# 使う)。Keepaのセラー出品リスト(asinList)は「新しい順」で短期間ではほぼ
# 変化しないため、同じセラーを浅く繰り返し再訪問しても新しい商品はほぼ
# 見つからない(実測: AWS本番で328回のセラーマイニングから59件のユニーク
# 商品しか発見できなかった)。閾値回数を超えたら評価件数を広げ、11件目
# 以降(未評価の新しい範囲)まで踏み込む。
SELLER_DEEP_REMINE_THRESHOLD = 3
SELLER_DEEP_REMINE_MAX_CANDIDATES = 30


def discover_and_register_sellers(
    asin: str, source: str, seed_asin: str = None, seed_keyword: str = None,
) -> list[str]:
    """指定ASINの出品セラー一覧(Amazonの「他のセラー」欄相当、実質全員
    = DISCOVER_SELLERS_PER_CANDIDATE件まで)を取得し、seller_poolに登録する
    (この場では即座にマイニングしない - 呼び出し元が必要に応じて行う)。
    トークン消費は約7(offers取得)のみで済み、登録件数を増やしても追加費用は
    かからないため、広さ優先(CEO: 「多くのものを輸出してる優秀なセラー候補を
    探したいので広さ優先の方が良い」「候補セラーを全て検討候補にいれる」)で
    プールを育てるのに向いている。
    戻り値: 発見できたセラーID一覧(0件の場合は空リスト)。
    """
    print(f"[INFO] {asin} の出品セラーを調べています...")
    try:
        sellers_lookup = find_other_sellers_for_candidate(asin=asin, max_sellers=DISCOVER_SELLERS_PER_CANDIDATE)
    except KeepaError as exc:
        print(f"[WARN] セラー一覧の取得に失敗しました: {exc}")
        return []

    seller_ids = sellers_lookup.get("seller_ids") or []
    if not seller_ids:
        print(f"[INFO] {asin} の出品セラーを特定できませんでした({sellers_lookup.get('note') or sellers_lookup.get('error')})。")
        return []

    print(f"[INFO] セラー{len(seller_ids)}件を特定: {seller_ids}")
    add_sellers(seller_ids, source=source, seed_asin=seed_asin or asin, seed_keyword=seed_keyword)
    return seller_ids


def _expand_from_one_seller(
    seller_id: str, source_label: str, wait_for_tokens: bool,
    seed_asin: str = None, max_candidates: int = SELLER_EXPANSION_MAX_CANDIDATES,
) -> dict | None:
    """1セラー分の出品をパイプラインで評価し、結果を保存する。失敗しても
    メインのスキャン結果には影響させない(例外を握りつぶしてログのみ)。
    seed_asin: このセラーを見つけるきっかけになったASIN(呼び出し元の
    合格候補) - agent_candidates/agent_runsのseed_asin列にそのまま入る。
    自動発動・定期サイクルの両方がここを通るので、seller_poolへの実績記録
    (record_seller_mined)もここで一元化する。
    戻り値: evaluate_mcp_candidates()の評価結果(qualified/rejectedを含む
    辞書、呼び出し元が合格候補の中身を見て連鎖発見に使えるように)。
    失敗した場合はNone。
    """
    print(f"[INFO] セラー {seller_id} の出品一覧を取得して評価します(最大{max_candidates}件)...")
    try:
        seller_result = expand_from_seller(
            seller_id=seller_id, max_candidates=max_candidates,
            price_diff_min=0.30, price_volatility_max=None, wait_for_tokens=wait_for_tokens,
        )
    except KeepaError as exc:
        print(f"[WARN] セラー出品の評価に失敗しました: {exc}")
        # times_mined を記録しないと pick_next_seller() のLRU順序でこのセラーが
        # 永久に最優先で選ばれ続け、他のセラーが一切マイニングされなくなる
        # (実際にAWS本番で発生した無限ループ - 壊れたセラーID1件が深夜枠を
        # 丸ごと占有し続けていた)。一時的な失敗(トークン枯渇など)なので
        # pausedにはせず、試行の記録だけ残す。
        record_seller_mined(seller_id, qualified_count=0)
        return None

    if seller_result.get("error"):
        print(f"[WARN] セラー出品の評価に失敗しました: {seller_result['error']}")
        record_seller_mined(seller_id, qualified_count=0)
        if seller_result["error"].startswith("No seller found for id"):
            # Keepa側にこのセラーIDが存在しないことが確定しているケース。
            # リトライしても直らないため、次回以降のLRU巡回から完全に除外する。
            print(f"[WARN] セラー {seller_id} はKeepa側に存在しないため、プールから除外(paused)します。")
            set_seller_status(seller_id, "paused")
        return None

    seller_name = seller_result.get("seller_name") or seller_id
    seller_evaluation = evaluate_mcp_candidates(seller_result)
    qualified_count = len(seller_evaluation["qualified"])
    print(f"[INFO] セラー「{seller_name}」の出品: {seller_result.get('evaluated', 0)}件評価 / "
          f"合格(優先度C+以上): {qualified_count}件")

    if seller_evaluation["qualified"]:
        enrich_qualified_candidates_with_offer_details(
            seller_evaluation["qualified"], wait_for_tokens=wait_for_tokens,
        )

    if seller_evaluation["qualified"] or seller_evaluation["rejected"]:
        seller_run_id = new_agent_run_id()
        persist_agent_run(
            f"{source_label} (セラー: {seller_name})", seller_evaluation, run_id=seller_run_id,
            source_type="seller", seller_id=seller_id, seller_name=seller_name, seed_asin=seed_asin,
        )
        log_agent_run(
            seller_run_id, datetime.now(timezone.utc).isoformat(), 0,
            keyword=f"[seller] {seller_name}", category=source_label,
            max_candidates=max_candidates, wait_for_tokens=wait_for_tokens,
            mcp_result=seller_result, evaluation=seller_evaluation,
            notify_status="deferred_to_digest",
            source_type="seller", seller_id=seller_id, seller_name=seller_name, seed_asin=seed_asin,
        )
        print(f"[INFO] セラー出品の評価結果も「エージェント」ページに保存しました (run_id={seller_run_id})。")

    record_seller_mined(seller_id, qualified_count=qualified_count, seller_name=seller_name)
    return seller_evaluation


def _best_discovery_candidates(evaluation: dict, limit: int = 1) -> list[dict]:
    """セラー発見の起点候補を選ぶ。evaluate_mcp_candidates()の戻り値をそのまま
    渡せる({"qualified": [...], "rejected": [...]})。

    2026-09-28、tier(pass/consider/reference/reject)がpriority_tierへ統合された
    ことで、qualifiedの範囲自体がB-/C+まで広がった(旧・要検討候補の大半を含む)ため、
    単純にqualified(既にpriority_tier順+ROI降順でソート済み)の先頭からlimit件を
    返すだけでよい(旧・rejected内のtier=='consider'を補う処理は不要になった)。
    """
    qualified = evaluation.get("qualified") or []
    return list(qualified[:limit])


def discover_sellers_for_pending_candidates(
    limit: int = NIGHTLY_SELLER_DISCOVERY_LIMIT,
    max_age_hours: int = NIGHTLY_SELLER_DISCOVERY_MAX_AGE_HOURS,
) -> int:
    """夜間のセラーマイニング枠の冒頭で呼ぶ: 日中のキーワード検索で見つかった
    合格候補(qualified=1、2026-09-28にpriority_tierへ統合後はS〜C+まで含む)のうち、
    まだセラーを発見していないものを見つけて、discover_and_register_sellers()で
    登録する(即時マイニングはしない)。

    CEO: 「優良が見つかってもその場では検索せず。セラーサーチは夜間だけに
    する」(2026-09-27) - 以前はrun_daily_scan()が合格候補を見つけたその場で
    セラーの発見・マイニングを行っていた(旧expand_from_top_seller)が、日中の
    キーワード検索サイクルからセラー関連の処理を完全に切り離し、発見・
    マイニングともに夜間のセラーマイニング枠(run_seller_mining_cycle)に
    一本化した。

    「まだ発見していない」の判定は、そのASINがseller_pool.seed_asinとして
    一度でも登録済みかどうかで行う(登録済みなら、既にこの関数か旧来の
    処理で発見済み)。対象はsource_type='keyword'(セラーマイニング由来の
    候補は対象外 - そちらはrun_seller_mining_cycle内のチェーンで別途処理
    される)。

    戻り値: セラーを発見できた候補の件数。
    """
    init_ops_tables()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            WITH ranked AS (
                SELECT asin, category, qualified, priority_tier, data_json, created_at,
                    ROW_NUMBER() OVER (PARTITION BY asin ORDER BY created_at DESC) AS rn
                FROM agent_candidates
                WHERE source_type = 'keyword' AND created_at >= ?
            )
            SELECT asin, category FROM ranked
            WHERE rn = 1 AND qualified = 1
              AND asin NOT IN (SELECT DISTINCT seed_asin FROM seller_pool WHERE seed_asin IS NOT NULL)
            ORDER BY ''' + PRIORITY_TIER_SQL_ORDER + ''', json_extract(data_json, '$.roi_pct') DESC
            LIMIT ?
            ''',
            (cutoff, limit),
        ).fetchall()

    if not rows:
        print("[INFO] セラー未発見の合格候補はありません。")
        return 0

    found = 0
    for asin, category in rows:
        print(f"[INFO] 夜間セラー発見: 候補 {asin}({category}) の出品セラーを調べています...")
        seller_ids = discover_and_register_sellers(asin, source="keyword_expansion")
        if seller_ids:
            found += 1
    return found


def run_seller_mining_cycle(max_candidates: int, wait_for_tokens: bool) -> bool:
    """Sellerエージェント: セラープールから次に調べるべき1件を選び、マイニングする。
    深夜のセラーマイニング時間帯(run_all_day.sh)に、キーワード検索の代わりに
    呼ばれる想定。プールが空なら何もせずFalseを返す(トークン消費なし)。

    冒頭で discover_sellers_for_pending_candidates() を呼び、日中に見つかった
    合格候補のうちセラー未発見のものを登録する(CEO: 「セラーサーチは夜間
    だけにする」2026-09-27)。マイニング本体(トークン消費の主体)は、
    これまで通りLRUで1セラーずつ処理する。
    """
    init_ops_tables()
    discover_sellers_for_pending_candidates()
    seller_id, times_mined = pick_next_seller()
    if not seller_id:
        print("[INFO] セラープールが空のため、定期セラーマイニングをスキップしました。")
        return False

    if times_mined >= SELLER_DEEP_REMINE_THRESHOLD:
        # 既にこのセラーを何度も浅く再訪問済み(KeepaのasinListは新しい順で
        # 短期間ではほぼ変化しないため、毎回同じ上位N件を再評価するだけに
        # なっていた)。評価件数を広げて未評価の範囲まで踏み込む。
        depth = SELLER_DEEP_REMINE_MAX_CANDIDATES
        print(f"[INFO] セラープールから選択: {seller_id}(調査済み{times_mined}回 - "
              f"深掘りのため評価件数を{depth}件に拡大)")
    else:
        depth = max_candidates
        print(f"[INFO] セラープールから選択: {seller_id}")

    seller_evaluation = _expand_from_one_seller(
        seller_id, "定期セラーマイニング", wait_for_tokens,
        seed_asin=None, max_candidates=depth,
    )

    if seller_evaluation:
        # 広さ優先(CEO: 「多くのものを輸出してる優秀なセラー候補を探したいので
        # 広さ優先の方が良い」)。このセラーの合格候補(無ければ要検討候補)が
        # 見つかったら、その商品を売っている他のセラーも新たに発見してプールに
        # 登録する(即座にはマイニングしない - トークンを抑えつつプールを広げ、
        # 次回以降のLRUサイクルで自然に巡回されるようにする)。
        for top in _best_discovery_candidates(seller_evaluation, limit=1):
            discover_and_register_sellers(top["asin"], source="seller_mining_chain")

    return seller_evaluation is not None


def run_daily_scan(
    keyword: str | None,
    category_name: str | None,
    category_id: int | None,
    max_candidates: int,
    wait_for_tokens: bool,
    price_min: int | None = None,
    price_max: int | None = None,
) -> None:
    init_ops_tables()

    run_id = new_agent_run_id()
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()

    keyword_from_pool = False
    if keyword is None:
        # Keyword/Categoryエージェント: 未使用/最も久しく使っていないものを優先。
        # pool_price_minは--price-min未指定(CLIから明示指定していない)場合のみ
        # 採用する - CLI引数の方が優先度が高い。
        keyword, pool_price_min = pick_next_keyword()
        if keyword:
            keyword_from_pool = True
            if price_min is None and pool_price_min is not None:
                price_min = pool_price_min
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
    if price_min is not None:
        # デフォルト(DEFAULT_SEARCH_PARAMS)はprice_min=None/price_max=3000
        # ($30以下)。--price-min / キーワードプールのper-keyword price_minで
        # 個別に下限を上書きしたい場合(例: 高単価カテゴリを試す時)にここで反映する。
        search_params["price_min"] = price_min
    if price_max is not None:
        search_params["price_max"] = price_max
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

    print(f"[INFO] 合格(優先度C+以上): {len(evaluation['qualified'])}件 / "
          f"却下: {len(evaluation['rejected'])}件")
    if evaluation["weight_missing"]:
        print(f"[WARN] 重量データなし(仮値で計算): {evaluation['weight_missing']}")
    if evaluation["fee_missing"]:
        print(f"[WARN] 手数料データなし(仮値で計算): {evaluation['fee_missing']}")

    if evaluation["qualified"]:
        print(f"[INFO] 有力候補{len(evaluation['qualified'])}件の在庫を取得中(Keepa offers+stock、約10トークン/件)...")
        offer_details_result = enrich_qualified_candidates_with_offer_details(
            evaluation["qualified"], wait_for_tokens=wait_for_tokens,
        )
        print(f"[INFO] 在庫取得: 成功{offer_details_result['enriched']}件 / 失敗{offer_details_result['failed']}件")

    persist_agent_run(label, evaluation, run_id=run_id)
    print(f"[INFO] 「エージェント」ページ用に保存しました (run_id={run_id})。ダッシュボードで確認できます。")

    if keyword_from_pool:
        record_keyword_used(keyword, qualified_count=len(evaluation["qualified"]))

    # 即時のLINE通知はしない(CEOの希望: 1日の候補は朝8時・夜8時に
    # send_daily_digest.py がまとめて通知する)。ここでは結果をDBに
    # 保存するだけ。
    print(f"[INFO] 合格(優先度C+以上){len(evaluation['qualified'])}件。ダイジェスト通知(8時/20時)でまとめて送信されます。")

    log_agent_run(
        run_id, started_at, time.monotonic() - start_time,
        keyword=keyword, category=category_name, category_id=category_id,
        max_candidates=max_candidates, wait_for_tokens=wait_for_tokens,
        mcp_result=mcp_result, evaluation=evaluation,
        notify_status="deferred_to_digest", notify_error=None,
    )

    # セラーマイニング(セラーの発見・調査)は、ここでは行わない。CEO:
    # 「優良が見つかってもその場では検索せず。セラーサーチは夜間だけに
    # する」(2026-09-27) - 日中のこのサイクルは、キーワード検索と評価・
    # 保存だけに専念する。この候補のセラー発見は、夜間枠
    # (run_seller_mining_cycle -> discover_sellers_for_pending_candidates)
    # が、agent_candidatesを見て後追いで行う。


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


def cmd_list_sellers() -> None:
    init_ops_tables()
    rows = list_seller_pool()
    if not rows:
        print("[INFO] セラープールは空です。合格候補が出るか、ダッシュボードから手動発見すると自動的に追加されます。")
        return
    print(f"[INFO] セラープール ({len(rows)}件):")
    for row in rows:
        mined = row.get("timesMined", 0)
        qualified = row.get("totalQualified", 0)
        last_mined = row.get("lastMinedAt") or "未調査"
        name = row.get("sellerName") or "(名前未取得)"
        print(f"  - {row['sellerId']:<20} {name:<30} source={row['source']:<18} "
              f"mined={mined:>3} qualified={qualified:>3} last_mined={last_mined}")


def main() -> None:
    parser = argparse.ArgumentParser(description="毎朝の候補スキャン + 実質利益率フィルタ + メール通知")
    parser.add_argument("--keyword", type=str, default=None,
                         help="検索キーワード(例: 'kitchen gadget')。省略時はキーワードプールから自動選択"
                              "(プールが空ならKEYWORD_ROTATIONで曜日ごとに自動選択)")
    parser.add_argument("--category", type=str, default=None, help="Keepaカテゴリ名で追加絞り込み(任意)")
    parser.add_argument("--category-id", type=int, default=None, help="Keepaカテゴリ ID を直接指定(--category より優先)")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_SEARCH_PARAMS["max_candidates"],
                         help="評価するASIN数の上限(Keepaトークン消費に直結)")
    parser.add_argument("--price-min", type=int, default=None,
                         help="US側の最低価格(セント単位、例: 3000 = $30.00)。省略時はデフォルト(下限なし、"
                              "$30以下のみを対象)のまま。高単価カテゴリを試す時など、下限を上書きしたい場合に使う")
    parser.add_argument("--price-max", type=int, default=None,
                         help="US側の最高価格(セント単位、例: 5000 = $50.00)。省略時はデフォルトの$30(3000)のまま")
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

    # --- Sellerエージェント: 定期セラーマイニング(run_all_day.shの深夜時間帯から呼ばれる想定) ---
    parser.add_argument("--seller-mining", action="store_true",
                         help="通常のキーワード検索の代わりに、セラープールから次の1件を選んでマイニングする")
    parser.add_argument("--seller-mining-max-candidates", type=int, default=SELLER_EXPANSION_MAX_CANDIDATES,
                         help="--seller-mining 使用時に1セラーあたり評価する出品数の上限")
    parser.add_argument("--list-sellers", action="store_true",
                         help="セラープールの内容を表示して終了する")

    args = parser.parse_args()

    if args.list_keywords:
        cmd_list_keywords()
        return
    if args.list_sellers:
        cmd_list_sellers()
        return
    if args.seed_from_favorites:
        cmd_seed_from_favorites()
        return
    if args.expand:
        cmd_expand(args.expand, args.max_categories)
        return
    if args.seller_mining:
        run_seller_mining_cycle(args.seller_mining_max_candidates, args.wait_for_tokens)
        return

    run_daily_scan(
        keyword=args.keyword,
        category_name=args.category,
        category_id=args.category_id,
        max_candidates=args.max_candidates,
        wait_for_tokens=args.wait_for_tokens,
        price_min=args.price_min,
        price_max=args.price_max,
    )


if __name__ == "__main__":
    main()
