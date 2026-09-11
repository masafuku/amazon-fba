"""
Ops/Finance エージェント

既存の keepa_imports.sqlite3 (sqlite_api_server.py と同じDB) に
運用・財務系のテーブルを追加し、以下を提供する:

  1. calc_unit_profit()      - 1個あたりの利益・利益率を計算
  2. record_daily_cost()     - 日次コスト(在庫/広告/リスティング)を記録
  3. check_budget_alert()    - 月次予算(カテゴリ別)の消化状況チェック
  4. record_sale() / record_inventory() - 売上・在庫のログ
  5. inventory_turnover()    - 在庫回転率
  6. weekly_report()         - CEOエージェント向け週次サマリー
  7. evaluate_mcp_candidates() / persist_agent_run() - keepa_mcpの候補を
     実質利益率でフィルタし、ダッシュボードの「エージェント」ページに
     表示するため agent_candidates に保存する(favoritesへの追加は
     CEOが手動で判断する運用)

既存コードには手を加えず、このモジュールを import して使う。
DBパスは sqlite_api_server.py と同じ場所を参照する。
"""

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sqlite_api_server.py と同じDBファイルを共有する
DB_PATH = Path(__file__).resolve().parent / 'keepa-csv-dashboard' / 'keepa_imports.sqlite3'

# 月次予算(円) - 状況に応じて調整
DEFAULT_BUDGET = {
    'inventory': 50_000,
    'ads': 30_000,
    'listing': 20_000,
}

# 予算消化率がこの値を超えたらアラート
BUDGET_ALERT_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# スキーマ初期化
# ---------------------------------------------------------------------------

def init_ops_tables():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS products (
                asin TEXT PRIMARY KEY,
                sku_name TEXT,
                jp_cost_jpy REAL,
                us_price_usd REAL,
                weight_kg REAL,
                status TEXT DEFAULT 'candidate'  -- candidate/sourcing/active/discontinued
            );

            CREATE TABLE IF NOT EXISTS daily_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,              -- YYYY-MM-DD
                category TEXT NOT NULL,          -- inventory/ads/listing
                amount_jpy REAL NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                asin TEXT NOT NULL,
                units_sold INTEGER NOT NULL,
                revenue_usd REAL NOT NULL,
                ad_spend_usd REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS inventory_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                asin TEXT NOT NULL,
                units_in_fba INTEGER NOT NULL
            );

            -- Researchエージェント(daily_scan.py)が調べた候補の一覧。
            -- 「エージェント」ページで人間(CEO)が見て、気に入ったものだけ
            -- favorites に手動で追加する運用のためのテーブル。
            CREATE TABLE IF NOT EXISTS agent_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                category TEXT,
                asin TEXT NOT NULL,
                title TEXT,
                image_url TEXT,
                us_url TEXT,
                jp_asin TEXT,
                jp_url TEXT,
                us_price_usd REAL,
                jp_cost_jpy REAL,
                sales_rank INTEGER,
                review_count INTEGER,
                monthly_sold INTEGER,            -- Keepaの月間販売個数(概算、bucketed estimate)
                price_volatility_90d REAL,
                weight_kg REAL,
                weight_estimated INTEGER,
                fee_estimated INTEGER,
                price_diff_rate_gross REAL,
                unit_profit_usd REAL,
                margin_pct REAL,
                qualified INTEGER NOT NULL,      -- 実質利益率が閾値以上なら1
                reason TEXT,                     -- 不合格理由(qualified=0のとき)
                data_json TEXT NOT NULL,         -- favoritesに渡す用の詳細データ
                created_at TEXT NOT NULL
            );

            -- daily_scan.py を1回実行するごとの動作履歴(エージェントページの
            -- 「実行履歴」に表示する)。agent_candidates が候補の中身なら、
            -- こちらは「いつ・何を条件に・何件評価して・通知はどうなったか」
            -- というラン単位のサマリー。
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                duration_seconds REAL,
                keyword TEXT,
                category TEXT,
                category_id INTEGER,
                max_candidates INTEGER,
                wait_for_tokens INTEGER,
                evaluated INTEGER,
                mcp_matched INTEGER,
                qualified_count INTEGER,
                rejected_count INTEGER,
                stopped_early_for_tokens INTEGER,
                error TEXT,
                notify_status TEXT,              -- sent/skipped/failed
                notify_error TEXT,
                status TEXT NOT NULL DEFAULT 'running'  -- running/completed/failed。エージェントページで進行中の検索を表示するため
            );

            CREATE INDEX IF NOT EXISTS idx_daily_costs_date ON daily_costs(date);
            CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(date);
            CREATE INDEX IF NOT EXISTS idx_sales_asin ON sales(asin);
            CREATE INDEX IF NOT EXISTS idx_inventory_log_asin ON inventory_log(asin);
            CREATE INDEX IF NOT EXISTS idx_agent_candidates_run_id ON agent_candidates(run_id);
            CREATE INDEX IF NOT EXISTS idx_agent_candidates_created_at ON agent_candidates(created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent_runs(started_at);
            CREATE INDEX IF NOT EXISTS idx_agent_candidates_asin ON agent_candidates(asin);

            -- Keyword/Categoryエージェント: daily_scan.py が使うキーワードの
            -- プール。手動シード・お気に入りから抽出したブランド/カテゴリ・
            -- Keepaのカテゴリツリー(children/relatedCategories/topBrands)で
            -- 自動拡張したキーワード、をまとめて管理する。
            CREATE TABLE IF NOT EXISTS keyword_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,        -- manual/favorite/expanded
                seed_keyword TEXT,           -- source=expandedの場合、展開元のキーワード
                added_at TEXT NOT NULL,
                last_used_at TEXT,
                times_used INTEGER NOT NULL DEFAULT 0,
                total_qualified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',  -- active/paused
                price_min INTEGER              -- find_arbitrage_candidates()のprice_min
                                                -- 上書き(米セント単位)。NULLならそちらの
                                                -- デフォルト($30/3000)のまま。文具女子
                                                -- アワード/JetPens受賞歴等、$30未満が
                                                -- 中心のキーワード群は自動巡回でも
                                                -- 全滅しないようここに低い値を入れる。
            );
            CREATE INDEX IF NOT EXISTS idx_keyword_pool_status ON keyword_pool(status);

            -- Sellerエージェント: セラーマイニング(expand_from_seller経由)の
            -- 対象セラーのプール。keyword_poolと全く同じLRUパターン(times_mined昇順
            -- →last_mined_at昇順で1件選ぶ)。3つの経路すべてがここに登録・記録する:
            -- ①daily_scan.pyのexpand_from_top_seller(合格候補から自動発見)、
            -- ②ダッシュボード(scripts/seller_mine_cli.py)からの手動発見/展開、
            -- ③本テーブル導入後の定期セラーマイニングサイクル。
            CREATE TABLE IF NOT EXISTS seller_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id TEXT NOT NULL UNIQUE,
                seller_name TEXT,
                source TEXT NOT NULL,        -- keyword_expansion/manual_expand/manual
                seed_asin TEXT,               -- このセラーを見つけたきっかけのASIN
                seed_keyword TEXT,            -- keyword_expansion経由の場合、発見元のキーワード検索語
                added_at TEXT NOT NULL,
                last_mined_at TEXT,
                times_mined INTEGER NOT NULL DEFAULT 0,
                total_qualified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'  -- active/paused
            );
            CREATE INDEX IF NOT EXISTS idx_seller_pool_status ON seller_pool(status);

            -- 1日2回(朝8時/夜8時)のLINEダイジェスト通知が「前回の通知以降」
            -- を正しく判定するための状態テーブル(常に1行だけ)。
            CREATE TABLE IF NOT EXISTS digest_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_sent_at TEXT
            );

            -- ===================================================================
            -- 収支・在庫ダッシュボード(SP-API連携)用テーブル群。
            -- Keepaには一切依存しない(sp_api/, gmail_client.py, sd_email_parser.py,
            -- sp_api_sync.pyのみが書き込む)。詳細は計画メモ
            -- 「SP-API連携による収支・在庫ダッシュボード」参照。
            -- ===================================================================

            -- Orders API (getOrders) の取得結果。
            CREATE TABLE IF NOT EXISTS sp_orders (
                order_id TEXT PRIMARY KEY,
                purchase_date TEXT,
                asin TEXT,
                sku TEXT,
                quantity INTEGER,
                item_price_usd REAL,
                order_status TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sp_orders_asin ON sp_orders(asin);
            CREATE INDEX IF NOT EXISTS idx_sp_orders_purchase_date ON sp_orders(purchase_date);

            -- Finances API (listFinancialEventsByOrderId) の取得結果。
            -- referral_fee_percent/fba_pickpack_feeという「推定値」ではなく、
            -- Amazonが実際に請求した金額をここに実績値として持つ。
            CREATE TABLE IF NOT EXISTS sp_financial_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                event_type TEXT NOT NULL,  -- Commission/FBAPerUnitFulfillmentFee/StorageFee 等
                amount_usd REAL NOT NULL,
                posted_date TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sp_financial_events_order_id ON sp_financial_events(order_id);

            -- FBA Inventory API (getInventorySummaries) の最新スナップショット
            -- (履歴ではなく毎回上書き。時系列が必要になれば別途拡張)。
            CREATE TABLE IF NOT EXISTS sp_fba_inventory (
                asin TEXT PRIMARY KEY,
                sku TEXT,
                fnsku TEXT,
                fulfillable_quantity INTEGER,
                snapshot_at TEXT
            );

            -- SP-APIエンドポイントごとの前回同期時刻(digest_stateと同じsingleton行)。
            CREATE TABLE IF NOT EXISTS sp_sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                orders_synced_at TEXT,
                finances_synced_at TEXT,
                inventory_synced_at TEXT
            );

            -- Amazon大口出品プラン/Keepa Pro/Claude等、SP-APIでは取得できない
            -- 固定費の手入力テーブル。
            CREATE TABLE IF NOT EXISTS fixed_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                monthly_amount_jpy REAL NOT NULL,
                effective_from TEXT,
                note TEXT
            );

            -- Super Deliveryの注文確定メール(「＜SD＞ご注文内容控え」)を
            -- sd_email_parser.pyがパースして書き込む仕入原価・数量の実績。
            CREATE TABLE IF NOT EXISTS jp_purchase_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_date TEXT,
                sd_reception_no TEXT UNIQUE,   -- 受付番号(メール1通に複数出展企業分入ることがあるが、受付番号は行ごとに一意)
                supplier_name TEXT,            -- 出展企業名(丸進/Zoomy BUNGU等)
                sd_product_no TEXT,            -- SD品番
                product_name TEXT,
                jan_code TEXT,
                variant TEXT,                  -- 内訳(色/キャラクター等)
                unit_price_jpy REAL,           -- 注文単価
                quantity INTEGER,              -- 注文点数
                amount_jpy REAL,               -- 注文金額
                asin TEXT                      -- asin_jan_map経由で手動リンク(未リンクならNULL)
            );
            CREATE INDEX IF NOT EXISTS idx_jp_purchase_records_jan_code ON jp_purchase_records(jan_code);
            CREATE INDEX IF NOT EXISTS idx_jp_purchase_records_asin ON jp_purchase_records(asin);

            -- JANコード<->ASINの手動リンク。Keepaのeanフィールドで自動照合も
            -- できるが、それだとKeepa依存が復活してしまうため、出品確定時に
            -- 手で1行登録する運用にして独立性を保つ(「Keepaからの独立性」参照)。
            CREATE TABLE IF NOT EXISTS asin_jan_map (
                asin TEXT PRIMARY KEY,
                jan_code TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_asin_jan_map_jan_code ON asin_jan_map(jan_code);

            -- sd_email_parser.pyの前回実行時刻(digest_stateと同じsingleton行)。
            CREATE TABLE IF NOT EXISTS sd_parse_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_parsed_at TEXT
            );
            '''
        )
        # 既存DBに対する後方互換マイグレーション(CREATE TABLE IF NOT EXISTSは
        # 既存テーブルに新カラムを追加してくれないため)。
        agent_candidates_columns = {row[1] for row in conn.execute('PRAGMA table_info(agent_candidates)').fetchall()}
        if 'image_url' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN image_url TEXT')
        if 'price_volatility_90d' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN price_volatility_90d REAL')
        if 'monthly_sold' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN monthly_sold INTEGER')
        # セラーマイニング(keepa_mcp.server.expand_from_seller経由で見つかった候補)を
        # キーワード検索経由の候補と区別するための列。source_typeのデフォルトは
        # 'keyword'なので、既存行(このマイグレーション以前のもの)は全て
        # 'keyword'扱いになる - 過去のセラーマイニング結果はcategory列の
        # テキスト("... (セラー: X)")に頼ったままで、一括バックフィルはしない
        # (本番データへの一括UPDATEのリスクを避けるため、必要になれば別途)。
        if 'source_type' not in agent_candidates_columns:
            conn.execute("ALTER TABLE agent_candidates ADD COLUMN source_type TEXT NOT NULL DEFAULT 'keyword'")
        if 'seller_id' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN seller_id TEXT')
        if 'seller_name' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN seller_name TEXT')
        if 'seed_asin' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN seed_asin TEXT')  # このセラーを見つけたきっかけのASIN
        if 'tier' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN tier TEXT')
            # 一度きりのバックフィル: 既存行はmargin_pct/us_price_usd/jp_cost_jpyから
            # tierを再計算できる(exchange_rate=150.0はevaluate_mcp_candidates()の
            # 呼び出し元が誰も上書きしていない、このコードベースで常に使われている
            # デフォルト値)。ADD COLUMN直後の一度だけ実行され、以後'tier'カラムが
            # 存在するので二度と実行されない。
            conn.execute(
                '''
                UPDATE agent_candidates
                SET tier = CASE
                    WHEN margin_pct IS NULL THEN 'reject'
                    WHEN margin_pct >= 0.20 THEN 'pass'
                    WHEN margin_pct >= 0 THEN 'consider'
                    WHEN (us_price_usd - jp_cost_jpy / 150.0) >= 0 THEN 'reference'
                    ELSE 'reject'
                END
                WHERE tier IS NULL
                '''
            )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_seller_id ON agent_candidates(seller_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_source_type ON agent_candidates(source_type)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_tier ON agent_candidates(tier)')

        agent_runs_columns = {row[1] for row in conn.execute('PRAGMA table_info(agent_runs)').fetchall()}
        if 'status' not in agent_runs_columns:
            conn.execute("ALTER TABLE agent_runs ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
            # 既存の行は(このカラム追加以前は)すべて完了済みのランなので'completed'を
            # デフォルトにする。今後の新規ランはlog_agent_run()が明示的に'running'から
            # 始めて更新する。
        if 'source_type' not in agent_runs_columns:
            conn.execute("ALTER TABLE agent_runs ADD COLUMN source_type TEXT NOT NULL DEFAULT 'keyword'")
        if 'seller_id' not in agent_runs_columns:
            conn.execute('ALTER TABLE agent_runs ADD COLUMN seller_id TEXT')
        if 'seller_name' not in agent_runs_columns:
            conn.execute('ALTER TABLE agent_runs ADD COLUMN seller_name TEXT')
        if 'seed_asin' not in agent_runs_columns:
            conn.execute('ALTER TABLE agent_runs ADD COLUMN seed_asin TEXT')

        seller_pool_columns = {row[1] for row in conn.execute('PRAGMA table_info(seller_pool)').fetchall()}
        if 'seed_keyword' not in seller_pool_columns:
            conn.execute('ALTER TABLE seller_pool ADD COLUMN seed_keyword TEXT')

        keyword_pool_columns = {row[1] for row in conn.execute('PRAGMA table_info(keyword_pool)').fetchall()}
        if 'price_min' not in keyword_pool_columns:
            conn.execute('ALTER TABLE keyword_pool ADD COLUMN price_min INTEGER')

        # seller_poolの一度きりの自動バックフィル: 既にsource_type='seller'の
        # 実績がagent_candidatesにある(過去のセラーマイニング結果)場合、
        # seller_poolがまだ空ならそこから再構築する。INSERT OR IGNOREなので
        # 複数プロセスから同時に呼ばれても安全(二重実行しても実害なし)。
        seller_pool_count = conn.execute('SELECT COUNT(*) FROM seller_pool').fetchone()[0]
        if seller_pool_count == 0:
            backfill_rows = conn.execute(
                '''
                SELECT seller_id,
                       MAX(seller_name) AS seller_name,
                       MIN(seed_asin) AS seed_asin,
                       MIN(created_at) AS added_at,
                       MAX(created_at) AS last_mined_at,
                       COUNT(DISTINCT run_id) AS times_mined,
                       SUM(qualified) AS total_qualified
                FROM agent_candidates
                WHERE source_type = 'seller' AND seller_id IS NOT NULL AND seller_id != ''
                GROUP BY seller_id
                '''
            ).fetchall()
            conn.executemany(
                '''
                INSERT OR IGNORE INTO seller_pool
                    (seller_id, seller_name, source, seed_asin, added_at, last_mined_at, times_mined, total_qualified, status)
                VALUES (?, ?, 'keyword_expansion', ?, ?, ?, ?, ?, 'active')
                ''',
                backfill_rows,
            )


# ---------------------------------------------------------------------------
# 1. 損益分岐点 / 利益率計算
# ---------------------------------------------------------------------------

# 国際送料モデル: 日本郵便EMS公式料金表(米国向け・第4地帯)を「重量に対して
# コストがどう増えるか」の形として使い、実際にフォワーダーから受け取った見積もり
# (グローバルブランド社、2026-08-04、容積重量7.20kgでFedEx International
# Economy実費 運賃¥8,615+燃油サーチャージ¥3,919+発送代行手数料¥1,500=
# 合計¥13,034)に合わせてスケールし直したもの。実際に使う予定のフォワーダー
# 経由のFedExの方がEMSより同じ重量帯で安いため(EMSの7〜8kg帯は¥19,900〜
# 22,300)、EMS表の重量別カーブの「形」だけ借りて金額は実見積もりに合わせて
# 割り引く。CEO宛見積もりメールより、米国側の関税も別途概算12.5%と判明した
# ため、DEFAULT_IMPORT_DUTY_RATEとして新規に計上する。
#
# 出典: 日本郵便 EMS料金表(第4地帯・米国)
#   https://www.post.japanpost.jp/int/charge/list/ems_all.html
#
# (weight_kgの上限, EMS運賃 円) のタプル一覧。「Xkgまで」の生データ
# (スケール前、_SHIPPING_COST_SCALE_FACTORで割り引く)。
_EMS_US_RATE_TABLE_JPY: list[tuple[float, float]] = [
    (0.5, 3900), (0.6, 4180), (0.7, 4460), (0.8, 4740), (0.9, 5020),
    (1.0, 5300), (1.25, 5990), (1.5, 6600), (1.75, 7290), (2.0, 7900),
    (2.5, 9100), (3.0, 10300), (3.5, 11500), (4.0, 12700), (4.5, 13900),
    (5.0, 15100), (5.5, 16300), (6.0, 17500), (7.0, 19900), (8.0, 22300),
    (9.0, 24700), (10.0, 27100), (11.0, 29500), (12.0, 31900), (13.0, 34300),
    (14.0, 36700), (15.0, 39100), (16.0, 41500), (17.0, 43900), (18.0, 46300),
    (19.0, 48700), (20.0, 51100), (21.0, 53500), (22.0, 55900), (23.0, 58300),
    (24.0, 60700), (25.0, 63100), (26.0, 65500), (27.0, 67900), (28.0, 70300),
    (29.0, 72700), (30.0, 75100),
]

# 実見積もり(容積重量7.20kg -> ¥13,034)によるキャリブレーション。
# EMS表を7.20kgで線形補間すると¥20,380相当になるため、その比率で全体をスケールする。
_FORWARDER_QUOTE_WEIGHT_KG = 7.20
_FORWARDER_QUOTE_COST_JPY = 13_034.0
_EMS_RATE_AT_QUOTE_WEIGHT_JPY = 19_900 + (_FORWARDER_QUOTE_WEIGHT_KG - 7.0) / (8.0 - 7.0) * (22_300 - 19_900)
_SHIPPING_COST_SCALE_FACTOR = _FORWARDER_QUOTE_COST_JPY / _EMS_RATE_AT_QUOTE_WEIGHT_JPY

# 米国の一般的な関税率(グローバルブランド社見積もりメールより:「基本的には
# 通常関税（12.5％）が定義」)。商品のHSコード次第で実際の税率は変わりうるため
# あくまで概算値。
DEFAULT_IMPORT_DUTY_RATE = 0.125


def _shipping_cost_jpy_for_weight(total_weight_kg: float) -> float:
    """1回の発送の合計重量(kg)から、フォワーダー実勢に合わせてスケールした
    国際送料(円)を返す。EMS公式表の重量別カーブを使い、表の点と点の間は
    線形補間する。表の範囲外(30kg超)は最後の区間の傾きで延長する。"""
    table = _EMS_US_RATE_TABLE_JPY
    if total_weight_kg <= table[0][0]:
        raw_jpy = table[0][1]
    elif total_weight_kg >= table[-1][0]:
        (w1, c1), (w2, c2) = table[-2], table[-1]
        slope = (c2 - c1) / (w2 - w1)
        raw_jpy = c2 + slope * (total_weight_kg - w2)
    else:
        raw_jpy = table[-1][1]  # ループが必ず上書きするが、型チェッカー向けの初期値
        for (w1, c1), (w2, c2) in zip(table, table[1:]):
            if w1 <= total_weight_kg <= w2:
                ratio = (total_weight_kg - w1) / (w2 - w1)
                raw_jpy = c1 + ratio * (c2 - c1)
                break
    return raw_jpy * _SHIPPING_COST_SCALE_FACTOR


def calc_unit_profit(
    us_price_usd: float,
    jp_cost_jpy: float,
    weight_kg: float,
    exchange_rate: float = 150.0,      # 円/ドル
    amazon_fee_rate: float = 0.15,     # Amazon販売手数料(カテゴリにより8〜15%)
    fba_fee_usd: float = 3.5,          # FBAピック&パック手数料(サイズ依存、要調整)
    shipment_budget_jpy: float = 50_000.0,      # 1回にまとめて仕入れる想定総額
    import_duty_rate: float = DEFAULT_IMPORT_DUTY_RATE,  # 米国関税(概算、商品カテゴリにより変動)
) -> dict:
    """1個あたりの利益・利益率を計算する。

    候補が出た瞬間にこの関数を通し、利益率が閾値未満なら自動除外できる。

    国際送料の想定(CEO: 「国際便なので一万円くらいはしそうです。ただし何個纏めて
    仕入れるかで損益分岐点が変わらそうです」→ 実際のフォワーダー見積もりを受けて
    重量連動モデルに改訂): 「1回にまとめて仕入れる個数」はこれまで通りJP原価から
    逆算する(総額が概ねshipment_budget_jpy(既定¥50,000)になるように、安い商品
    ほど多くまとめ買いできる)。そのうえで、1回の発送の合計重量(=商品1個の重量×
    まとめ買い個数)を_shipping_cost_jpy_for_weight()に渡し、実際のフォワーダー
    見積もりに合わせてスケールした金額を使う。JP原価が予算を超える場合は最低1個
    として扱う(その1個の重量分の送料を丸ごと負担する形)。

    関税(CEO宛フォワーダー見積もりメール: 「基本的には通常関税（12.5％）が
    定義」)も新たに費用として計上する。JP原価(輸入申告額の代理指標)に対して
    import_duty_rate(既定12.5%)を掛けた額を差し引く——実際の税率は商品の
    HSコード次第で変わりうるため、あくまで概算。
    """
    jp_cost_usd = jp_cost_jpy / exchange_rate
    amazon_fee_usd = us_price_usd * amazon_fee_rate
    units_per_shipment = max(1, int(shipment_budget_jpy // jp_cost_jpy)) if jp_cost_jpy > 0 else 1
    total_shipment_weight_kg = weight_kg * units_per_shipment
    shipping_cost_usd = (
        _shipping_cost_jpy_for_weight(total_shipment_weight_kg) / units_per_shipment
    ) / exchange_rate
    import_duty_usd = jp_cost_usd * import_duty_rate

    unit_profit_usd = (
        us_price_usd - amazon_fee_usd - fba_fee_usd - shipping_cost_usd - import_duty_usd - jp_cost_usd
    )
    margin_pct = unit_profit_usd / us_price_usd if us_price_usd else 0.0
    # ROI(投下資本利益率) = 実質利益 ÷ JP原価(投下資本)。CEO: 「今回のケースは、
    # 投下資本に対して利益の割合も重要ですよね」— 1回の仕入れに使える資金
    # (shipment_budget_jpy)が実質的な制約であるこのビジネスモデルでは、US価格に
    # 対する粗利率(margin_pct)よりもJP原価に対するROIの方が資金効率の指標として
    # 本質的、という判断。高単価・低JP原価の商品ほどmargin_pctとの乖離が大きくなる。
    roi_pct = unit_profit_usd / jp_cost_usd if jp_cost_usd else 0.0

    return {
        'us_price_usd': round(us_price_usd, 2),
        'jp_cost_usd': round(jp_cost_usd, 2),
        'amazon_fee_usd': round(amazon_fee_usd, 2),
        'fba_fee_usd': round(fba_fee_usd, 2),
        'shipping_cost_usd': round(shipping_cost_usd, 2),
        'import_duty_usd': round(import_duty_usd, 2),
        'unit_profit_usd': round(unit_profit_usd, 2),
        'margin_pct': round(margin_pct, 4),
        'roi_pct': round(roi_pct, 4),
    }


def break_even_units(fixed_cost_jpy: float, unit_profit_usd: float, exchange_rate: float = 150.0) -> float:
    """固定費(リスティング制作費など)を回収するのに必要な販売個数。"""
    if unit_profit_usd <= 0:
        return float('inf')
    fixed_cost_usd = fixed_cost_jpy / exchange_rate
    return fixed_cost_usd / unit_profit_usd


def backfill_roi_pct() -> int:
    """CEO: 「過去のデータを全て更新してください」— roi_pct追加(calc_unit_profit()の
    出力にroi_pctを追加した際の変更)がagent_candidatesの既存行には反映されないため、
    一度きりのバックフィルで追加する。unit_profit_usd/jp_cost_usdは元々data_jsonに
    保存済みなので、Keepaへの再問い合わせなしに算術だけで計算できる(shipping_cost_usd/
    import_duty_usdのようにKeepa再取得が要る値とは違う)。ローカルDB・AWS本番DBは
    それぞれ独立している(*.sqlite3はgitignore対象)ため、両方で個別に一度実行する。
    戻り値は更新した行数。
    """
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT id, data_json FROM agent_candidates
            WHERE json_extract(data_json, '$.unit_profit_usd') IS NOT NULL
              AND json_extract(data_json, '$.jp_cost_usd') IS NOT NULL
              AND json_extract(data_json, '$.roi_pct') IS NULL
            '''
        ).fetchall()
        updated = 0
        for row_id, data_json in rows:
            data = json.loads(data_json)
            jp_cost_usd = data.get('jp_cost_usd')
            unit_profit_usd = data.get('unit_profit_usd')
            if not jp_cost_usd:
                continue
            data['roi_pct'] = round(unit_profit_usd / jp_cost_usd, 4)
            conn.execute(
                'UPDATE agent_candidates SET data_json = ? WHERE id = ?',
                (json.dumps(data, ensure_ascii=False), row_id),
            )
            updated += 1
    return updated


# ---------------------------------------------------------------------------
# 2. 日次コスト記録 & 予算アラート
# ---------------------------------------------------------------------------

def record_daily_cost(date: str, category: str, amount_jpy: float, note: str = ''):
    assert category in DEFAULT_BUDGET, f'unknown category: {category}'
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO daily_costs (date, category, amount_jpy, note) VALUES (?, ?, ?, ?)',
            (date, category, amount_jpy, note),
        )


def get_monthly_spend(year_month: str) -> dict:
    """year_month: 'YYYY-MM'"""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT category, SUM(amount_jpy)
            FROM daily_costs
            WHERE date LIKE ?
            GROUP BY category
            ''',
            (f'{year_month}%',),
        ).fetchall()
    return {category: total for category, total in rows}


def check_budget_alert(year_month: str, budget: dict = None) -> list:
    """予算消化率が閾値を超えたカテゴリのアラートメッセージ一覧を返す。"""
    budget = budget or DEFAULT_BUDGET
    spent = get_monthly_spend(year_month)
    alerts = []
    for category, limit in budget.items():
        used = spent.get(category, 0)
        pct = used / limit if limit else 0
        if pct >= 1.0:
            alerts.append(f'⚠️ {category}: 予算超過 {used:.0f}/{limit:.0f}円 ({pct:.0%})')
        elif pct >= BUDGET_ALERT_THRESHOLD:
            alerts.append(f'△ {category}: {pct:.0%}消化 ({used:.0f}/{limit:.0f}円)')
    return alerts


# ---------------------------------------------------------------------------
# 3. 売上 / 在庫ログ
# ---------------------------------------------------------------------------

def record_sale(date: str, asin: str, units_sold: int, revenue_usd: float, ad_spend_usd: float = 0.0):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO sales (date, asin, units_sold, revenue_usd, ad_spend_usd) VALUES (?, ?, ?, ?, ?)',
            (date, asin, units_sold, revenue_usd, ad_spend_usd),
        )


def record_inventory(date: str, asin: str, units_in_fba: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO inventory_log (date, asin, units_in_fba) VALUES (?, ?, ?)',
            (date, asin, units_in_fba),
        )


def inventory_turnover(asin: str, period_days: int = 30) -> float:
    """直近 period_days 日の在庫回転率(販売数 / 平均在庫数)。目標:月1回転以上。"""
    since = (datetime.now(timezone.utc) - timedelta(days=period_days)).strftime('%Y-%m-%d')
    with sqlite3.connect(DB_PATH) as conn:
        units_sold = conn.execute(
            'SELECT COALESCE(SUM(units_sold), 0) FROM sales WHERE asin = ? AND date >= ?',
            (asin, since),
        ).fetchone()[0]
        avg_inventory = conn.execute(
            'SELECT AVG(units_in_fba) FROM inventory_log WHERE asin = ? AND date >= ?',
            (asin, since),
        ).fetchone()[0]

    if not avg_inventory:
        return 0.0
    return round(units_sold / avg_inventory, 2)


def estimate_mrr_pace(period_days: int = 7) -> float:
    """直近 period_days の売上から月換算ペース(USD)を推定する。"""
    since = (datetime.now(timezone.utc) - timedelta(days=period_days)).strftime('%Y-%m-%d')
    with sqlite3.connect(DB_PATH) as conn:
        revenue = conn.execute(
            'SELECT COALESCE(SUM(revenue_usd), 0) FROM sales WHERE date >= ?',
            (since,),
        ).fetchone()[0]
    daily_avg = revenue / period_days if period_days else 0
    return round(daily_avg * 30, 2)


# ---------------------------------------------------------------------------
# 4. 週次レポート(CEOエージェント向け)
# ---------------------------------------------------------------------------

def weekly_report(year_month: str = None) -> dict:
    year_month = year_month or datetime.now(timezone.utc).strftime('%Y-%m')

    with sqlite3.connect(DB_PATH) as conn:
        active_asins = [
            row[0] for row in conn.execute(
                "SELECT asin FROM products WHERE status = 'active'"
            ).fetchall()
        ]

    turnover = {asin: inventory_turnover(asin) for asin in active_asins}

    return {
        'year_month': year_month,
        'budget_alerts': check_budget_alert(year_month),
        'monthly_spend': get_monthly_spend(year_month),
        'inventory_turnover': turnover,
        'mrr_pace_usd': estimate_mrr_pace(),
        'target_mrr_usd': 500,
    }


# ---------------------------------------------------------------------------
# 5. Keepa MCP との連携ブリッジ
# ---------------------------------------------------------------------------

DEFAULT_WEIGHT_KG_FALLBACK = 0.5  # weight_kg が取れない商品向けの保守的な仮値

# CEO: 「輸出ビジネスだと利益率よりも、ROIの方が適切な指標では？」「利益率は15%に
# しましょう」— 資金を回転させて増やしていくビジネスモデルでは、投下資本(JP原価)
# に対する回収率(ROI)の方が資金効率の観点で本質的に重要。ただし利益率(US価格に
# 対する余裕度)は価格競争・手数料変動への耐性を示す安全弁として引き続き必要
# (ROIが高くても利益率が極端に薄い商品は、僅かな値下げで簡単に赤字転落するため)。
# ROIを主な合格基準、利益率を安全弁とする二段階ゲート(20%→15%に引き下げ)。
MIN_MARGIN_PCT = 0.15             # 安全弁: これ未満は(ROIが良くても)自動除外
MIN_ROI_PCT = 0.50                # 主な合格基準: 資金効率

# 合格ラインの多段階化(CEO: 「合格ラインは何段階かに分けてください。たとえば、
# US−JPがゼロ以上、つまり手数料が0円なら成立する、というのもみたいです」)。
# qualified/rejectedという2リストのメンバーシップ自体は変えない(tier==passが
# qualified、それ以外はすべてrejected) - 各エントリに付与するtierフィールドが
# 新しい情報として増えるだけ。
TIER_PASS = 'pass'            # ROI >= 50% かつ 実質利益率 >= 15%(両方満たして合格)
TIER_CONSIDER = 'consider'    # 実質利益率 0%以上だが、ROIまたは利益率が基準未満
TIER_REFERENCE = 'reference'  # 実質利益率マイナスだが、手数料を一切引かない粗差
                               # (US価格 - JP原価)が0以上(=手数料が0円なら成立する)
TIER_REJECT = 'reject'        # 上記のいずれでもない、または価格データ自体が無い

# CEO: 「何もしていないので特に課税事業者としては登録されていないとおもいます」
# 「本業は不動産賃貸業です。課税事業者登録は、今後しますが、今は税込前提で試算
# してください」— 免税事業者の間は消費税の仕入税額控除ができず、卸仕入れの消費税が
# 実質コストになる。課税事業者登録(インボイス登録)が完了したらTrueに変更する。
IS_JCT_REGISTERED = False
JCT_RATE = 0.10


def normalize_jp_cost_for_tax(
    wholesale_raw_jpy: float | None,
    jp_amazon_raw_jpy: float | None,
) -> float | None:
    """卸価格(NETSEA等、税抜表示が通例)とAmazon JP小売価格(Keepa経由、税込表示が
    通例)は税基準が揃っていないため単純比較できない。IS_JCT_REGISTEREDに応じて
    両方を同じ基準(免税事業者なら税込、課税事業者なら税抜)に揃えてから、両方
    揃っていれば安い方を返す(CEO: 「利益等は安い方で計算してください」の前提を
    保つ)。どちらか一方しか無ければそちらを正規化して返す。両方NoneならNone。
    """
    candidates = []
    if wholesale_raw_jpy is not None:
        candidates.append(
            wholesale_raw_jpy if IS_JCT_REGISTERED else wholesale_raw_jpy * (1 + JCT_RATE)
        )
    if jp_amazon_raw_jpy is not None:
        candidates.append(
            jp_amazon_raw_jpy / (1 + JCT_RATE) if IS_JCT_REGISTERED else jp_amazon_raw_jpy
        )
    return min(candidates) if candidates else None


def _has_minimum_demand_evidence(demand_signal: dict | None) -> bool:
    """需要データが完全に欠落している(=売上ランクも月間販売数も無い)候補を
    見分ける。CEO: 「需要シグナルを加味する」への対応。

    NETSEA本実行で、利益率・ROIは基準を満たすのに売上ランク無し・月間販売数
    無し・出品者1件のみという「合格」判定が4件連続で発生した(実例:
    B001AI0MDQ/B001AI6DJ8/B0779MLPPK/B07GSDKMPC)。共通していたのは
    `sales_rank`が完全に欠落(Keepaのstats.current[SALES]が -1 = データなし)
    していたこと - これは「ランクが低い」のとは違い、Amazonが売上ランクを
    一切算出していない=ほぼ動きの無いリスティングであることを示す。
    `sales_rank_drops_30`が0(値として存在はする)だけでは判定材料にしない
    (0自体は正当な値であり、僅かな実売があるケースと区別がつかないため)。

    demand_signal自体が渡されない(=呼び出し元がまだ対応していない、または
    データ取得元がKeepaでない)場合は判定不能なので、常にTrue(=足切りしない、
    従来通りの挙動)を返す。"""
    if demand_signal is None:
        return True
    return demand_signal.get('sales_rank') is not None or demand_signal.get('monthly_sold') is not None


def _classify_tier(
    margin_pct: float | None,
    roi_pct: float | None,
    us_price_usd: float | None,
    jp_cost_usd: float | None,
    min_margin_pct: float = MIN_MARGIN_PCT,
    min_roi_pct: float = MIN_ROI_PCT,
    demand_signal: dict | None = None,
) -> str:
    """calc_unit_profit()の結果から4段階のtierを判定する。
    CEO: 「輸出ビジネスだと利益率よりも、ROIの方が適切な指標では？」— ROIを主な
    合格基準(資金効率)、利益率を安全弁(価格競争・手数料変動への耐性)として
    両方を満たす場合のみ合格とする。

    CEO: 「需要シグナルを加味する」— 利益率・ROIの基準を満たしていても、
    売上ランク・月間販売数のどちらも存在しない(=需要の裏付けが全く無い)
    候補は、価格が歪んだ放置リスティングである可能性が高いため合格にせず
    「要検討」に格下げする(不合格にはしない - 価格計算自体は間違っていない
    ため、人間の目視確認に委ねる)。"""
    if margin_pct is None:
        return TIER_REJECT
    if margin_pct >= min_margin_pct and roi_pct is not None and roi_pct >= min_roi_pct:
        if _has_minimum_demand_evidence(demand_signal):
            return TIER_PASS
        return TIER_CONSIDER
    if margin_pct >= 0:
        return TIER_CONSIDER
    if us_price_usd is not None and jp_cost_usd is not None and (us_price_usd - jp_cost_usd) >= 0:
        return TIER_REFERENCE
    return TIER_REJECT


def evaluate_mcp_candidates(
    mcp_result: dict,
    min_margin_pct: float = MIN_MARGIN_PCT,
    min_roi_pct: float = MIN_ROI_PCT,
    exchange_rate: float = 150.0,
) -> dict:
    """keepa_mcp.server.find_arbitrage_candidates() の戻り値を受け取り、
    各候補に calc_unit_profit() を通して実利益ベースでフィルタする。

    MCP側の price_diff_rate は FBA手数料・送料・関税を含まないため、
    ここで初めて「本当に儲かるか」を判定する。

    Amazon販売手数料率・FBA Pick&Packピック手数料は、Keepaが商品ごとに
    返す referralFeePercentage / fbaFees.pickAndPackFee があればそれを
    使い、無い場合のみ calc_unit_profit() のデフォルト仮値にフォールバック
    する(国際送料は仮値のまま。Keepaは国際配送費までは持っていない)。

    Returns:
        {
          'qualified': [利益率が閾値以上の候補 + profit詳細],
          'rejected':  [利益率が閾値未満だった候補 + 理由],
          'weight_missing': [重量データが無く仮値で計算した候補のASIN一覧],
          'fee_missing': [手数料データが無く仮値で計算した候補のASIN一覧],
        }
    """
    qualified, rejected, weight_missing, fee_missing = [], [], [], []

    for candidate in mcp_result.get('candidates', []):
        sell = candidate['sell']
        cost = candidate['cost']
        asin = sell['asin']

        # ブランド名等の広いキーワード("Sanrio"等)で検索すると、そのブランドが
        # 展開しているフィギュア/コレクタブルもヒットしてしまう。これらは
        # 出品時にブランドゲーティング・Transparency・ライセンス許諾で
        # 行き止まりになることが繰り返し確認されている(CEOメモリ
        # 「Excluded sourcing categories」)ため、キーワード自体が問題ない
        # 場合でも商品タイトル段階でここで弾く。
        title = sell.get('title') or ''
        if _is_figure_or_collectible_keyword(title):
            # ダッシュボードの「エージェント」ページでフィルタ・表示できるよう、
            # 却下理由(reason)に加えて画像・価格など一覧表示に要る最低限の
            # フィールドも持たせておく(qualified候補ほど詳細ではないが、
            # 一覧上でどの商品か判別できる程度)。
            rejected.append({
                'asin': asin,
                'title': title,
                'url': sell.get('url'),
                'image_url': sell.get('image_url'),
                'us_price_usd': sell.get('price'),
                'jp_cost_jpy': cost.get('price'),
                'reason': 'figure_or_collectible',
            })
            continue

        weight_kg = sell.get('weight_kg')
        used_fallback_weight = weight_kg is None
        if used_fallback_weight:
            weight_kg = DEFAULT_WEIGHT_KG_FALLBACK
            weight_missing.append(asin)

        # Keepa returns per-product referralFeePercentage / fbaFees for many
        # ASINs - real, category/size-specific figures beat the flat
        # defaults in calc_unit_profit() whenever they're available.
        fee_kwargs = {}
        used_fallback_fee = False
        referral_pct = sell.get('referral_fee_percent')
        if referral_pct is not None:
            fee_kwargs['amazon_fee_rate'] = referral_pct / 100
        else:
            used_fallback_fee = True
        fba_fee = sell.get('fba_pickpack_fee')
        if fba_fee is not None:
            fee_kwargs['fba_fee_usd'] = fba_fee
        else:
            used_fallback_fee = True
        if used_fallback_fee:
            fee_missing.append(asin)

        # cost['price'] は JP円、sell['price'] はUSD想定(sell_domain=US前提)
        profit = calc_unit_profit(
            us_price_usd=sell['price'],
            jp_cost_jpy=cost['price'],
            weight_kg=weight_kg,
            exchange_rate=exchange_rate,
            **fee_kwargs,
        )

        entry = {
            'asin': asin,
            'title': sell.get('title'),
            'url': sell.get('url'),
            'image_url': sell.get('image_url'),
            'jp_asin': cost.get('asin'),
            'jp_url': cost.get('url'),
            'sales_rank': sell.get('sales_rank'),
            'review_count': sell.get('review_count'),
            'monthly_sold': sell.get('monthly_sold'),
            'sales_rank_drops_30': sell.get('sales_rank_drops_30'),
            'sales_rank_drops_90': sell.get('sales_rank_drops_90'),
            'competitor_seller_count': sell.get('competitor_seller_count'),
            'demand_signal': sell.get('demand_signal'),
            'brand_store': sell.get('brand_store'),
            'price_diff_rate_gross': candidate.get('price_diff_rate'),  # 手数料・送料考慮前
            'price_volatility_90d': candidate.get('price_volatility_90d'),
            'weight_kg': weight_kg,
            'weight_estimated': used_fallback_weight,
            'fee_estimated': used_fallback_fee,
            # 既に取得済み(追加のKeepaトークン消費なし)だが、これまで
            # entryにコピーされていなかったフィールド。詳細ページ用。
            'brand': sell.get('brand'),
            'jp_brand': cost.get('brand'),
            'rating': sell.get('rating'),
            'jp_rating': cost.get('rating'),
            'upc': sell.get('upc'),
            'ean': sell.get('ean'),
            **profit,  # unit_profit_usd, margin_pct など (jp_cost_usd 含む)
            'jp_cost_jpy': cost['price'],
            # この経路はJP Amazon価格のみが原価情報(卸価格は別経路
            # のnetsea_sourcing.py側でのみ判明する) - フロントエンドが
            # 「Amazon JP価格」「卸価格」を別列で出し分けられるよう、
            # どちらの経路でも同じ2フィールドを必ず持たせる。
            'jp_amazon_cost_jpy': cost['price'],
            'wholesale_cost_jpy': None,
        }
        entry['tier'] = _classify_tier(
            profit['margin_pct'], profit['roi_pct'], profit['us_price_usd'], profit['jp_cost_usd'],
            min_margin_pct, min_roi_pct, entry['demand_signal'],
        )

        if entry['tier'] == TIER_PASS:
            qualified.append(entry)
        else:
            entry['reason'] = (
                f"実質利益率 {profit['margin_pct']:.1%}(閾値{min_margin_pct:.0%}) / "
                f"ROI {profit['roi_pct']:.0%}(閾値{min_roi_pct:.0%}) が基準未満"
            )
            rejected.append(entry)

    qualified.sort(key=lambda e: e['margin_pct'], reverse=True)

    # keepa_mcp.find_arbitrage_candidates() の粗選別(価格変動・JP一致・
    # 価格差率など)で落ちた候補も「不合格」として表示する。
    #
    # ほとんどの粗選別スキップ(価格変動が大きい、JPにASINが無い等)は
    # コスト側(JP)のデータをそもそも取得していない(トークン節約のため、
    # CEOの判断で意図的にそうしている)ので unit_profit_usd/margin_pct は
    # Noneのまま。ただし「価格差率が閾値未満」で落ちたものだけは、US/JP
    # 両方の価格がすでに取得済み(そこまで進んで初めて価格差率を計算できる
    # ため)なので、他の合格/不合格候補と同じ実利益計算をしてあげられる。
    for skip in mcp_result.get('skipped', []):
        asin = skip.get('asin')
        if not asin:
            continue

        us_price = skip.get('price')
        jp_price = skip.get('jp_price')
        entry = {
            'asin': asin,
            'title': skip.get('title'),
            'url': skip.get('url'),
            'image_url': skip.get('image_url'),
            'jp_asin': skip.get('jp_asin'),
            'jp_url': skip.get('jp_url'),
            'sales_rank': skip.get('sales_rank'),
            'review_count': skip.get('review_count'),
            'monthly_sold': skip.get('monthly_sold'),
            'sales_rank_drops_30': skip.get('sales_rank_drops_30'),
            'sales_rank_drops_90': skip.get('sales_rank_drops_90'),
            'competitor_seller_count': skip.get('competitor_seller_count'),
            'demand_signal': skip.get('demand_signal'),
            'brand_store': skip.get('brand_store'),
            'price_diff_rate_gross': None,
            'price_volatility_90d': skip.get('price_volatility_90d'),
            'weight_kg': skip.get('weight_kg'),
            'weight_estimated': False,
            'fee_estimated': False,
            'brand': skip.get('brand'),
            'jp_brand': None,
            'rating': skip.get('rating'),
            'jp_rating': None,
            'upc': skip.get('upc'),
            'ean': skip.get('ean'),
            'us_price_usd': us_price,
            'jp_cost_jpy': jp_price,
            'jp_amazon_cost_jpy': jp_price,
            'wholesale_cost_jpy': None,
            'unit_profit_usd': None,
            'margin_pct': None,
            'tier': TIER_REJECT,  # 価格データが無い場合の既定値(下で価格が両方揃えば上書きされる)
            'reason': f"粗選別で除外: {_translate_skip_reason(skip.get('reason', ''))}",
        }

        if us_price is not None and jp_price is not None:
            weight_kg = skip.get('weight_kg')
            used_fallback_weight = weight_kg is None
            if used_fallback_weight:
                weight_kg = DEFAULT_WEIGHT_KG_FALLBACK
                weight_missing.append(asin)

            fee_kwargs = {}
            used_fallback_fee = False
            referral_pct = skip.get('referral_fee_percent')
            if referral_pct is not None:
                fee_kwargs['amazon_fee_rate'] = referral_pct / 100
            else:
                used_fallback_fee = True
            fba_fee = skip.get('fba_pickpack_fee')
            if fba_fee is not None:
                fee_kwargs['fba_fee_usd'] = fba_fee
            else:
                used_fallback_fee = True
            if used_fallback_fee:
                fee_missing.append(asin)

            profit = calc_unit_profit(
                us_price_usd=us_price, jp_cost_jpy=jp_price,
                weight_kg=weight_kg, exchange_rate=exchange_rate, **fee_kwargs,
            )
            entry.update(profit)  # unit_profit_usd, margin_pct など
            entry['weight_kg'] = weight_kg
            entry['weight_estimated'] = used_fallback_weight
            entry['fee_estimated'] = used_fallback_fee
            entry['tier'] = _classify_tier(
                profit['margin_pct'], profit['roi_pct'], profit['us_price_usd'], profit['jp_cost_usd'],
                min_margin_pct, min_roi_pct, entry['demand_signal'],
            )

        rejected.append(entry)

    return {
        'qualified': qualified,
        'rejected': rejected,
        'weight_missing': weight_missing,
        'fee_missing': fee_missing,
        'evaluated': len(mcp_result.get('candidates', [])) + len(mcp_result.get('skipped', [])),
    }


_SKIP_REASON_TRANSLATIONS = (
    ('no current price for this ASIN on Amazon Japan', '日本のAmazonに価格データなし(未取扱の可能性)'),
    ('no current price', '価格データなし'),
    ('price volatility', '価格変動が大きすぎる'),
    ('stopped early: Keepa token budget ran out', 'Keepaトークン予算切れで打ち切り'),
    ('JP lookup failed', '日本側の商品取得に失敗(トークン切れの可能性)'),
    ('ASIN not found in Amazon Japan catalog', '日本のAmazonにこのASINが見つからない(別ASINの可能性)'),
    ('price diff rate', '価格差率が閾値未満'),
    ('could not compute price diff rate', '価格差率を計算できなかった'),
)


def _translate_skip_reason(reason: str) -> str:
    """keepa_mcp.find_arbitrage_candidates()のskip理由(英語)を、元の数値
    情報を保ったまま日本語ラベルに置き換える(完全一致ではなく前方一致的な
    キーワードマッチなので、新しいreason文言が増えても壊れにくい)。"""
    for needle, label in _SKIP_REASON_TRANSLATIONS:
        if needle in reason:
            return f"{label} ({reason})" if reason else label
    return reason or '理由不明'


def build_qualified_line_message(evaluation: dict, max_items: int = 5) -> str:
    """evaluate_mcp_candidates() の結果を notify_line.send_line_notify() に
    渡せるメッセージ文字列に整形する。"""
    qualified = evaluation['qualified']
    if not qualified:
        return f"本日の候補: 実質利益率20%以上の商品は見つかりませんでした(評価{evaluation['evaluated']}件)。"

    lines = [f"実質利益率{MIN_MARGIN_PCT:.0%}以上の候補 {len(qualified)}件"]
    for item in qualified[:max_items]:
        weight_note = '(重量は仮値)' if item['weight_estimated'] else ''
        lines.append('---')
        lines.append(f"ASIN: {item['asin']}")
        lines.append(f"{item['title']}")
        lines.append(f"利益率: {item['margin_pct']:.1%} / 1個あたり利益: ${item['unit_profit_usd']:.2f}")
        lines.append(f"ランキング: {item['sales_rank']} / レビュー数: {item['review_count']}{weight_note}")

    if len(qualified) > max_items:
        lines.append(f"他 {len(qualified) - max_items} 件")

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 7. 1日2回(朝8時/夜8時)のLINEダイジェスト通知
#
# daily_scan.py 自体は即時通知しない(CEOの希望: 「1日の候補を朝8時と
# 夜8時にまとめて送ってほしい。即時通知は不要」)。代わりに
# send_daily_digest.py がこのセクションの関数を使い、前回のダイジェスト
# 送信以降にたまった合格候補をまとめて1通のLINEメッセージにする。
# ---------------------------------------------------------------------------

def get_last_digest_sent_at() -> str | None:
    """前回ダイジェストを送った時刻(ISO8601)。まだ一度も送っていなければNone。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT last_sent_at FROM digest_state WHERE id = 1').fetchone()
    return row[0] if row else None


def set_last_digest_sent_at(sent_at: str) -> None:
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO digest_state (id, last_sent_at) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET last_sent_at = excluded.last_sent_at
            ''',
            (sent_at,),
        )


def load_digest_window(since_iso: str | None):
    """前回ダイジェスト送信以降(初回はsince_iso=None、直近24時間扱い)の
    データをまとめて返す: 合格候補(ASIN重複除去・複数回見つかった場合は
    最新のものを採用)と、その間に検索したキーワード一覧。
    """
    init_ops_tables()
    if since_iso is None:
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        candidate_rows = conn.execute(
            '''
            WITH ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (PARTITION BY asin ORDER BY created_at DESC) AS rn
                FROM agent_candidates
                WHERE tier IN ('pass', 'consider') AND created_at > ?
            )
            SELECT asin, title, us_url, jp_url, us_price_usd, jp_cost_jpy,
                   sales_rank, review_count, margin_pct, unit_profit_usd,
                   weight_estimated, category, created_at, tier
            FROM ranked
            WHERE rn = 1
            ORDER BY CASE tier WHEN 'pass' THEN 0 ELSE 1 END, margin_pct DESC
            ''',
            (since_iso,),
        ).fetchall()

        keyword_rows = conn.execute(
            'SELECT DISTINCT keyword FROM agent_runs WHERE started_at > ? AND keyword IS NOT NULL ORDER BY keyword',
            (since_iso,),
        ).fetchall()

    candidates = [
        {
            'asin': asin, 'title': title, 'us_url': us_url, 'jp_url': jp_url,
            'us_price_usd': us_price_usd, 'jp_cost_jpy': jp_cost_jpy,
            'sales_rank': sales_rank, 'review_count': review_count,
            'margin_pct': margin_pct, 'unit_profit_usd': unit_profit_usd,
            'weight_estimated': bool(weight_estimated), 'category': category,
            'created_at': created_at, 'tier': tier,
        }
        for asin, title, us_url, jp_url, us_price_usd, jp_cost_jpy, sales_rank, review_count,
            margin_pct, unit_profit_usd, weight_estimated, category, created_at, tier in candidate_rows
    ]
    keywords = [row[0] for row in keyword_rows]
    return candidates, keywords, since_iso


def build_daily_digest_message(
    candidates: list,
    keywords: list,
    period_label: str,
    budget_alerts: list = None,
    max_items: int = 5,
    exchange_rate: float = 150.0,
) -> str:
    """1日2回のダイジェスト通知本文を組み立てる。"""
    today = datetime.now(timezone.utc).strftime('%Y/%m/%d')
    lines = [f"【{period_label}まとめ】{today}"]

    if keywords:
        lines.append(f"検索キーワード: {', '.join(keywords)} ({len(keywords)}件)")
    else:
        lines.append("検索は行われませんでした。")

    if not candidates:
        lines.append("合格・要検討の候補はありませんでした。")
    else:
        pass_count = sum(1 for c in candidates if c.get('tier') == 'pass')
        consider_count = len(candidates) - pass_count
        lines.append(f"合格(利益率20%以上): {pass_count}件 / 要検討(0〜20%): {consider_count}件(重複除く)")
        for item in candidates[:max_items]:
            weight_note = '(重量は仮値)' if item['weight_estimated'] else ''
            tier_label = '【合格】' if item.get('tier') == 'pass' else '【要検討】'
            lines.append('---')
            lines.append(f"{tier_label} ASIN: {item['asin']}")
            lines.append(f"{item['title'] or ''}")
            profit_jpy = round((item['unit_profit_usd'] or 0) * exchange_rate)
            margin = item['margin_pct']
            lines.append(f"利益率: {margin:.1%} / 1個あたり利益: ¥{profit_jpy:,}" if margin is not None else "利益率: -")
            us_price = item['us_price_usd']
            jp_price = item['jp_cost_jpy']
            us_price_str = f"${us_price:.2f}" if us_price is not None else '-'
            jp_price_str = f"¥{jp_price:.0f}" if jp_price is not None else '-'
            lines.append(f"US: {us_price_str} / JP: {jp_price_str}")
            lines.append(f"ランキング: {item['sales_rank']} / レビュー数: {item['review_count']}{weight_note}")
            if item['us_url']:
                lines.append(f"US: {item['us_url']}")
            if item['jp_url']:
                lines.append(f"JP: {item['jp_url']}")

        if len(candidates) > max_items:
            lines.append(f"他 {len(candidates) - max_items} 件")

    if budget_alerts:
        lines.append('')
        lines.append('【予算アラート】')
        lines.extend(budget_alerts)

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 6. 「エージェント」ページ向け: 調査結果の永続化
# ---------------------------------------------------------------------------

def new_agent_run_id() -> str:
    """persist_agent_run() と log_agent_run() で同じrun_idを共有したい
    呼び出し元(daily_scan.py)向けの採番ヘルパー。"""
    return f"agent-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def persist_agent_run(
    category: str, evaluation: dict, run_id: str = None,
    source_type: str = 'keyword', seller_id: str = None,
    seller_name: str = None, seed_asin: str = None,
) -> str:
    """evaluate_mcp_candidates() の結果(合格・不合格とも)を agent_candidates に
    保存する。ダッシュボードの「エージェント」ページがこれを表示し、
    CEOが気に入ったものだけ手動で favorites に追加する運用を想定。

    run_id を渡さない場合は新規採番する。log_agent_run() と同じ実行に
    紐付けたい場合は new_agent_run_id() で採番したものを両方に渡す。

    source_type='seller' + seller_id/seller_name/seed_asin は
    keepa_mcp.server.expand_from_seller() 経由(セラーマイニング)で見つかった
    候補であることを示す(daily_scan.py / scripts/seller_mine_cli.py が使う)。
    通常のキーワード検索候補は source_type='keyword' のまま(デフォルト)。

    Returns: 今回使った run_id
    """
    init_ops_tables()  # このモジュール単体で先に呼ばれるケースに備えて念のため

    run_id = run_id or new_agent_run_id()
    created_at = datetime.now(timezone.utc).isoformat()

    rows = []
    for qualified_flag, items in ((1, evaluation.get('qualified', [])), (0, evaluation.get('rejected', []))):
        for item in items:
            rows.append((
                run_id,
                category,
                item['asin'],
                item.get('title'),
                item.get('image_url'),
                item.get('url'),
                item.get('jp_asin'),
                item.get('jp_url'),
                item.get('us_price_usd'),
                item.get('jp_cost_jpy'),
                item.get('sales_rank'),
                item.get('review_count'),
                item.get('monthly_sold'),
                item.get('price_volatility_90d'),
                item.get('weight_kg'),
                1 if item.get('weight_estimated') else 0,
                1 if item.get('fee_estimated') else 0,
                item.get('price_diff_rate_gross'),
                item.get('unit_profit_usd'),
                item.get('margin_pct'),
                qualified_flag,
                item.get('tier'),
                item.get('reason'),
                json.dumps(item, ensure_ascii=False),
                created_at,
                source_type,
                seller_id,
                seller_name,
                seed_asin,
            ))

    if rows:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executemany(
                '''
                INSERT INTO agent_candidates (
                    run_id, category, asin, title, image_url, us_url, jp_asin, jp_url,
                    us_price_usd, jp_cost_jpy, sales_rank, review_count, monthly_sold, price_volatility_90d,
                    weight_kg, weight_estimated, fee_estimated,
                    price_diff_rate_gross, unit_profit_usd, margin_pct,
                    qualified, tier, reason, data_json, created_at,
                    source_type, seller_id, seller_name, seed_asin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                rows,
            )

    return run_id


def log_agent_run(
    run_id: str,
    started_at: str,
    duration_seconds: float,
    keyword: str = None,
    category: str = None,
    category_id: int = None,
    max_candidates: int = None,
    wait_for_tokens: bool = None,
    mcp_result: dict = None,
    evaluation: dict = None,
    error: str = None,
    notify_status: str = None,
    notify_error: str = None,
    status: str = None,
    source_type: str = 'keyword',
    seller_id: str = None,
    seller_name: str = None,
    seed_asin: str = None,
) -> None:
    """daily_scan.py の1回の実行を agent_runs に記録する(エージェントページの
    「実行履歴」用)。persist_agent_run() が候補の中身を保存するのに対し、
    こちらは実行条件と結果件数・通知結果のサマリーだけを保存する。

    daily_scan.py は開始直後にも(結果がまだ無い状態で)一度これを呼び、
    「今まさに実行中」であることをエージェントページに表示できるようにする
    (status='running' の行がINSERTされる)。完了/失敗時にもう一度同じ
    run_id で呼ぶと、ON CONFLICTでその行が更新される。

    `status` を明示しなければ、error があれば'failed'、mcp_result/evaluation
    のどちらかにデータがあれば'completed'、それ以外(まだ何も結果が無い=
    開始直後の呼び出し)は'running'と推測する。

    source_type/seller_id/seller_name/seed_asin は persist_agent_run() と
    同じ意味(セラーマイニング由来のランかどうか)。INSERT時のみ設定され、
    ON CONFLICT更新では変更しない(keyword/categoryなど他の実行条件列と
    同じく、running→completedの更新で変わるものではないため)。
    """
    init_ops_tables()

    mcp_result = mcp_result or {}
    evaluation = evaluation or {}

    if status is None:
        if error:
            status = 'failed'
        elif mcp_result or evaluation:
            status = 'completed'
        else:
            status = 'running'

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO agent_runs (
                run_id, started_at, duration_seconds, keyword, category, category_id,
                max_candidates, wait_for_tokens, evaluated, mcp_matched,
                qualified_count, rejected_count, stopped_early_for_tokens,
                error, notify_status, notify_error, status,
                source_type, seller_id, seller_name, seed_asin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                duration_seconds = excluded.duration_seconds,
                evaluated = excluded.evaluated,
                mcp_matched = excluded.mcp_matched,
                qualified_count = excluded.qualified_count,
                rejected_count = excluded.rejected_count,
                stopped_early_for_tokens = excluded.stopped_early_for_tokens,
                error = excluded.error,
                notify_status = excluded.notify_status,
                notify_error = excluded.notify_error,
                status = excluded.status
            ''',
            (
                run_id, started_at, duration_seconds, keyword, category, category_id,
                max_candidates, 1 if wait_for_tokens else 0,
                mcp_result.get('evaluated'), mcp_result.get('matched'),
                len(evaluation.get('qualified', [])), len(evaluation.get('rejected', [])),
                1 if mcp_result.get('stopped_early_for_tokens') else 0,
                error, notify_status, notify_error, status,
                source_type, seller_id, seller_name, seed_asin,
            ),
        )


# ---------------------------------------------------------------------------
# 7. Keyword/Categoryエージェント: キーワードプール管理
# ---------------------------------------------------------------------------

def add_keywords(keywords, source: str, seed_keyword: str = None, price_min: int = None) -> int:
    """キーワードをプールに追加する(既存のものはスキップ)。追加できた件数を返す。
    _is_searchable_keyword()(Amazon大分類名・日本語表記・食品など不向きな
    カテゴリを除外)をここで一元的に適用する - --expand経由のKeepaカテゴリ
    ツリー由来のキーワードもこれを通るので、呼び出し元ごとに個別にフィルタを
    書く必要はない。
    price_min: daily_scan.pyのfind_arbitrage_candidates()呼び出しに渡す
    price_min上書き(米セント単位)。省略時はデフォルト(下限なし、$30以下の
    み対象)のまま追加される。高単価カテゴリのキーワード群だけ下限を戻したい
    場合に指定する。

    キーワードの2方向運用(2026-09-09、文具女子アワード/JetPens受賞コレクション
    19件中2件しか実質利益率20%に届かなかった反省から): JP発の商品名(受賞リスト等)
    を直接キーワード化する場合(チャンネルB/JP→US)は、Keepaを叩く前にブラウザで
    (1)JP側の実売バッジ・ベストセラー順位、(2)US側に対応ASINが実在するか+実際の
    Amazonタイトル、を確認してから、その実タイトルの部分文字列をキーワードにする
    (自己流の言い換えは表記ゆれ(例: "Sun-Star"と"Sunstar")で0件になりやすい)。
    一方、日本メーカーのブランド名(Pilot/Kokuyo/Sun-Star/Tombow/Zebra/Uni
    Mitsubishi/Midori/Kutsuwa/Maruman/Nakabayashi/Lihit Lab等)は手がかり無しで
    直接投入してよい(チャンネルA/US→JP、source='jp_brand_name')。ブランド名は
    表記ゆれが起きにくく、find_arbitrage_candidates()自体のUS側実需フィルタ
    (monthly_sold_peak_min等)が「US側で既に売れているもの」だけを自然に残す。
    """
    init_ops_tables()
    keywords = [str(k).strip() for k in keywords if str(k or '').strip()]
    keywords = [k for k in keywords if _is_searchable_keyword(k)]
    if not keywords:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.executemany(
            '''
            INSERT OR IGNORE INTO keyword_pool (keyword, source, seed_keyword, added_at, times_used, total_qualified, status, price_min)
            VALUES (?, ?, ?, ?, 0, 0, 'active', ?)
            ''',
            [(keyword, source, seed_keyword, now, price_min) for keyword in keywords],
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


# Amazon USのトップレベル部門名(ブラウズカテゴリ)。実際の商品タイトルに
# 出てくる言葉ではないため、Keepaのtitleキーワード検索(find_products)に
# 渡しても0件になる。「粗選別で除外」の理由には出てこず、Finderの結果自体が
# 0件になるだけなので気づきにくい - 実際に "Clothing, Shoes & Jewelry" /
# "Arts, Crafts & Sewing" で0件ヒットを確認済み。productCategoryから
# キーワードを拾う際はここに含まれるものを除外する。
_AMAZON_TOP_LEVEL_CATEGORIES = {
    'clothing, shoes & jewelry', 'sports & outdoors', 'toys & games',
    'arts, crafts & sewing', 'home & kitchen', 'electronics',
    'beauty & personal care', 'health & household', 'grocery & gourmet food',
    'pet supplies', 'office products', 'tools & home improvement',
    'automotive', 'baby', 'books', 'movies & tv', 'cds & vinyl',
    'video games', 'cell phones & accessories', 'industrial & scientific',
    'patio, lawn & garden', 'appliances', 'musical instruments', 'software',
    'collectibles & fine art', 'entertainment collectibles',
    'sports collectibles', 'everything else', 'computers & accessories',
    'camera & photo', 'garden & outdoor', 'kitchen & dining',
    'luggage & travel gear', 'home improvement', 'clothing', 'shoes',
    'jewelry', 'watches', 'handmade products',
}


def _contains_japanese(text: str) -> bool:
    """ひらがな・カタカナ・漢字が含まれるか(Unicodeのブロック範囲で判定)。
    find_arbitrage_candidates()のキーワード検索は常にsell_domain(デフォルト
    US)側のタイトルに対して行われる - 仕入れ先(JP)のブランド名/カテゴリを
    そのまま拾うと、日本語表記はUS商品タイトルに一致しない
    (例: 'G-SHOCK(ジーショック)'ではなく'G-Shock'でないとヒットしない)。
    「日本原産ブランドが良い」のは正しいが、それは英語表記で検索する必要がある。
    """
    for ch in text:
        code = ord(ch)
        if (
            0x3040 <= code <= 0x309F   # ひらがな
            or 0x30A0 <= code <= 0x30FF  # カタカナ
            or 0x4E00 <= code <= 0x9FFF  # CJK統合漢字
            or 0xFF66 <= code <= 0xFF9D  # 半角カタカナ
        ):
            return True
    return False


# 食品・飲料・サプリなど、消費期限・国際輸送での液体/成分規制・税関手続きの
# 煩雑さでFBA輸出(日本→米国の小口国際発送)に向かないカテゴリのキーワード
# (CEO: 「食品などfba輸出に向かない物は検索から除外してください」)。
# 部分一致(小文字化して判定)なので、「Japan snacks」「matcha powder」の
# ようにこれらの語を含むキーワード全般を拾う。完璧な分類ではない
# (誤検知/見逃しはあり得る)が、既存のAmazon大分類名フィルタと同じ、
# 実用重視のヒューリスティックとして運用する。
_FOOD_AND_UNSUITABLE_KEYWORDS = (
    'food', 'snack', 'snacks', 'candy', 'candies', 'chocolate', 'chocolates',
    'cookie', 'cookies', 'cracker', 'crackers', 'gum', 'gums',
    # 'tea'/'coffee'は単独だと"tea kettle"/"coffee maker"のような器具まで
    # 誤って除外してしまう(実際にお気に入り由来の"Tea Kettles"で誤検知が
    # 確認された)ため、消費物そのものを指すフレーズに絞る。
    'green tea', 'black tea', 'oolong tea', 'tea bag', 'tea bags', 'tea leaves', 'loose tea',
    'matcha', 'cocoa',
    'coffee bean', 'coffee beans', 'coffee grounds', 'ground coffee', 'instant coffee',
    'rice', 'noodle', 'noodles', 'ramen', 'udon', 'soba',
    'miso', 'soy sauce', 'sauce', 'sauces', 'seasoning', 'seasonings',
    'spice', 'spices', 'koji',
    'sake', 'wine', 'wines', 'beer', 'beers', 'whisky', 'whiskey', 'gin',
    'alcohol', 'liquor',
    'wagyu', 'meat', 'meats', 'seafood', 'fish',
    'onigiri', 'bento', 'sushi', 'gourmet food', 'grocery', 'groceries',
    'supplement', 'supplements', 'vitamin', 'vitamins',
)


def _is_food_or_unsuitable_keyword(candidate: str) -> bool:
    """食品・飲料・サプリなど、FBA輸出に向かないカテゴリのキーワードかどうか。
    単純な部分文字列一致だと"tea"が"teak"に誤マッチするような事故が起きるため、
    単語境界(\\b)で区切って判定する(フレーズも"soy sauce"のようにそのまま
    使える)。
    """
    lowered = candidate.strip().lower()
    return any(
        re.search(r'\b' + re.escape(term) + r'\b', lowered)
        for term in _FOOD_AND_UNSUITABLE_KEYWORDS
    )


# フィギュア/キャラクターコレクタブルは、ブランドゲーティング・Amazon
# Transparency・ライセンス許諾の壁でこのセッション中に繰り返し行き止まりに
# なった(CEO: メモリ「Excluded sourcing categories」参照)。食品と同じ扱いで
# キーワード段階・候補タイトル段階の両方で除外する。
_FIGURE_AND_COLLECTIBLE_KEYWORDS = (
    'figure', 'figures', 'figurine', 'figurines', 'action figure', 'action figures',
    'pvc figure', 'scale figure', 'prize figure', 'nendoroid', 'figma',
    'funko', 'pop vinyl', 'statue', 'statues', 'model kit', 'model kits',
    'gunpla', 'garage kit', 'garage kits', 'trading card', 'trading cards',
    'tcg', 'blind box', 'blind boxes', 'gacha', 'capsule toy', 'capsule toys',
    'collectible', 'collectibles', 'diorama', 'dioramas',
    # トレーディングカードゲームの実際の商品タイトルは「trading card」と
    # 書かず「Booster Pack/Box」「Elite Trainer Box」等の製品形態名や
    # ブランド名そのものを名乗ることが大半(CEO: 「ポケモンカードなどの
    # コレクタブルが残ってる」— 上のtrading card系だけでは取りこぼした)。
    'booster pack', 'booster packs', 'booster box', 'booster boxes',
    'elite trainer box', 'card game', 'playing card', 'playing cards',
    'sports card', 'sports cards', 'graded card', 'graded cards', 'psa 10',
    'pokemon card', 'pokemon cards', 'pokémon card', 'pokémon cards',
    'yugioh', 'yu-gi-oh', 'magic the gathering', 'mtg card', 'mtg cards',
)


def _is_figure_or_collectible_keyword(candidate: str) -> bool:
    lowered = candidate.strip().lower()
    return any(
        re.search(r'\b' + re.escape(term) + r'\b', lowered)
        for term in _FIGURE_AND_COLLECTIBLE_KEYWORDS
    )


def _is_searchable_keyword(candidate: str) -> bool:
    """お気に入りから拾った文字列が、Keepaのtitleキーワード検索(常にUS側の
    タイトルに対して行われる)として使えそうかを判定する: Amazonの大分類名
    そのものではないか、日本語表記ではないか、食品/フィギュア・
    キャラクターコレクタブルなどFBA輸出・出品に向かないカテゴリでは
    ないか。"""
    stripped = candidate.strip()
    if stripped.lower() in _AMAZON_TOP_LEVEL_CATEGORIES:
        return False
    if _contains_japanese(stripped):
        return False
    if _is_food_or_unsuitable_keyword(stripped):
        return False
    if _is_figure_or_collectible_keyword(stripped):
        return False
    return True


def seed_keyword_pool_from_favorites() -> dict:
    """お気に入り登録済みの商品からブランド名・カテゴリ名を抽出し、
    キーワードプールの種にする。CEOが実際に「良い」と判断した商品が
    最も強いシグナルなので、Keyword/Categoryエージェントの起点として使う。
    """
    init_ops_tables()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute('SELECT data_json FROM favorites').fetchall()
    except sqlite3.OperationalError:
        return {'seeds_found': [], 'added': 0}  # favoritesテーブルがまだ無い

    seeds = set()
    for (data_json,) in rows:
        try:
            data = json.loads(data_json) if data_json else {}
        except Exception:
            continue
        us = data.get('US') if isinstance(data.get('US'), dict) else {}
        jp = data.get('JP') if isinstance(data.get('JP'), dict) else {}

        for candidate in (data.get('brand'), us.get('brand'), jp.get('brand')):
            candidate = str(candidate).strip() if candidate else ''
            if candidate and _is_searchable_keyword(candidate):
                seeds.add(candidate)

        for candidate in (data.get('productCategory'), data.get('category'), us.get('productCategory')):
            candidate = str(candidate).strip() if candidate else ''
            if candidate and candidate != '未分類' and _is_searchable_keyword(candidate):
                seeds.add(candidate)

    seeds_list = sorted(seeds)
    added = add_keywords(seeds_list, source='favorite')
    return {'seeds_found': seeds_list, 'added': added}


def pick_next_keyword() -> tuple[str, int | None] | tuple[None, None]:
    """次にdaily_scan.pyで使うキーワードを選ぶ。一度も使っていないものを
    優先し、次に最後に使ってから時間が経っているものを優先する。
    プールが空の場合は (None, None) を返す(呼び出し側でフォールバックする)。
    戻り値は (keyword, price_min) のタプル - price_minはそのキーワードに
    add_keywords()で個別設定された上書き値(無ければNone、呼び出し側の
    デフォルトのまま)。
    """
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            '''
            SELECT keyword, price_min FROM keyword_pool
            WHERE status = 'active'
            ORDER BY times_used ASC, COALESCE(last_used_at, '') ASC
            LIMIT 1
            '''
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def record_keyword_used(keyword: str, qualified_count: int = 0) -> None:
    """キーワードプール内のキーワードを実際に使った後、使用実績を記録する。
    プールに無いキーワード(--keywordで直接指定した等)なら何もしない。
    """
    init_ops_tables()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            UPDATE keyword_pool
            SET times_used = times_used + 1,
                last_used_at = ?,
                total_qualified = total_qualified + ?
            WHERE keyword = ?
            ''',
            (now, qualified_count, keyword),
        )


def set_keyword_status(keyword: str, status: str) -> bool:
    """キーワードプール内の1件のstatusを変更する(active/paused)。
    Amazonの大分類名など「検索語として機能しない」ことが分かったキーワードを
    pick_next_keyword()の対象から外すのに使う(削除ではなくpausedにするので、
    いつ・なぜ止めたかの履歴(times_used/total_qualified)は残る)。
    戻り値: 対象行が見つかって更新できたか。
    """
    init_ops_tables()
    if status not in ('active', 'paused'):
        raise ValueError("status must be 'active' or 'paused'")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            'UPDATE keyword_pool SET status = ? WHERE keyword = ?',
            (status, keyword),
        )
    return cursor.rowcount > 0


def list_keyword_pool() -> list:
    """ダッシュボード表示用に、キーワードプール全体を返す。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT keyword, source, seed_keyword, added_at, last_used_at,
                   times_used, total_qualified, status, price_min
            FROM keyword_pool
            ORDER BY times_used ASC, COALESCE(last_used_at, '') ASC
            '''
        ).fetchall()
    return [
        {
            'keyword': keyword,
            'source': source,
            'seedKeyword': seed_keyword,
            'addedAt': added_at,
            'lastUsedAt': last_used_at,
            'timesUsed': times_used,
            'totalQualified': total_qualified,
            'status': status,
            'priceMin': price_min,
        }
        for keyword, source, seed_keyword, added_at, last_used_at, times_used, total_qualified, status, price_min in rows
    ]


# ---------------------------------------------------------------------------
# 8. Sellerエージェント: セラープール管理(keyword_poolと同じLRUパターン)
# ---------------------------------------------------------------------------

def add_sellers(seller_ids, source: str, seed_asin: str = None, seed_keyword: str = None) -> int:
    """セラーIDをプールに追加する(既存のものはスキップ)。追加できた件数を返す。
    seed_keyword: source='keyword_expansion'の場合、このセラーを見つけるきっかけに
    なったキーワード検索語(ダッシュボードの「セラー別統計」でキーワード列として表示する)。
    定期セラーマイニング・ダッシュボードからの手動発見経路には「元になった検索キーワード」
    という概念が無いためNoneのまま(表示側で「-」扱い)。
    """
    init_ops_tables()
    seller_ids = [str(s).strip() for s in seller_ids if str(s or '').strip()]
    if not seller_ids:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.executemany(
            '''
            INSERT OR IGNORE INTO seller_pool (seller_id, source, seed_asin, seed_keyword, added_at, times_mined, total_qualified, status)
            VALUES (?, ?, ?, ?, ?, 0, 0, 'active')
            ''',
            [(seller_id, source, seed_asin, seed_keyword, now) for seller_id in seller_ids],
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def pick_next_seller() -> tuple[str, int] | tuple[None, None]:
    """次にマイニングするセラーを選ぶ。一度も調べていないものを優先し、
    次に最後に調べてから時間が経っているものを優先する。
    戻り値: (seller_id, times_mined)。プールが空の場合は (None, None)
    (呼び出し側でスキップする)。times_mined は呼び出し側が「同じセラーを
    何度も調べ済みなら深く掘る」判断(deep re-mine)に使う。
    """
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            '''
            SELECT seller_id, times_mined FROM seller_pool
            WHERE status = 'active'
            ORDER BY times_mined ASC, COALESCE(last_mined_at, '') ASC
            LIMIT 1
            '''
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def record_seller_mined(seller_id: str, qualified_count: int = 0, seller_name: str = None) -> None:
    """セラープール内のセラーを実際にマイニングした後、実績を記録する。
    プールに無いセラー(手動でIDを直接指定した等)なら何もしない。
    seller_nameが渡された場合は判明した名前で更新する(初回発見時はNoneのことが多い)。
    """
    init_ops_tables()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        if seller_name:
            conn.execute(
                '''
                UPDATE seller_pool
                SET times_mined = times_mined + 1,
                    last_mined_at = ?,
                    total_qualified = total_qualified + ?,
                    seller_name = ?
                WHERE seller_id = ?
                ''',
                (now, qualified_count, seller_name, seller_id),
            )
        else:
            conn.execute(
                '''
                UPDATE seller_pool
                SET times_mined = times_mined + 1,
                    last_mined_at = ?,
                    total_qualified = total_qualified + ?
                WHERE seller_id = ?
                ''',
                (now, qualified_count, seller_id),
            )


def set_seller_status(seller_id: str, status: str) -> bool:
    """セラープール内の1件のstatusを変更する(active/paused)。
    削除ではなくpausedにするので、いつ・なぜ止めたかの履歴
    (times_mined/total_qualified)は残る。
    戻り値: 対象行が見つかって更新できたか。
    """
    init_ops_tables()
    if status not in ('active', 'paused'):
        raise ValueError("status must be 'active' or 'paused'")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            'UPDATE seller_pool SET status = ? WHERE seller_id = ?',
            (status, seller_id),
        )
    return cursor.rowcount > 0


def list_seller_pool() -> list:
    """ダッシュボード表示用に、セラープール全体を返す。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT seller_id, seller_name, source, seed_asin, added_at, last_mined_at,
                   times_mined, total_qualified, status
            FROM seller_pool
            ORDER BY times_mined ASC, COALESCE(last_mined_at, '') ASC
            '''
        ).fetchall()
    return [
        {
            'sellerId': seller_id,
            'sellerName': seller_name,
            'source': source,
            'seedAsin': seed_asin,
            'addedAt': added_at,
            'lastMinedAt': last_mined_at,
            'timesMined': times_mined,
            'totalQualified': total_qualified,
            'status': status,
        }
        for seller_id, seller_name, source, seed_asin, added_at, last_mined_at, times_mined, total_qualified, status in rows
    ]


# ---------------------------------------------------------------------------
# SP-API連携(収支・在庫ダッシュボード)。Keepaには依存しない
# — sp_api/, gmail_client.py, sd_email_parser.py, sp_api_sync.py が書き込み、
# sqlite_api_server.py の /api/finance/* が読み出す。
# ---------------------------------------------------------------------------

def get_sp_sync_state() -> dict:
    """SP-APIエンドポイントごとの前回同期時刻。一度も同期していなければ全てNone。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT orders_synced_at, finances_synced_at, inventory_synced_at FROM sp_sync_state WHERE id = 1'
        ).fetchone()
    if not row:
        return {'ordersSyncedAt': None, 'financesSyncedAt': None, 'inventorySyncedAt': None}
    return {'ordersSyncedAt': row[0], 'financesSyncedAt': row[1], 'inventorySyncedAt': row[2]}


def set_sp_sync_state(*, orders_synced_at=None, finances_synced_at=None, inventory_synced_at=None) -> None:
    """渡されたフィールドだけ更新する(未指定のフィールドは既存値を保持)。"""
    init_ops_tables()
    current = get_sp_sync_state()
    orders_synced_at = orders_synced_at if orders_synced_at is not None else current['ordersSyncedAt']
    finances_synced_at = finances_synced_at if finances_synced_at is not None else current['financesSyncedAt']
    inventory_synced_at = inventory_synced_at if inventory_synced_at is not None else current['inventorySyncedAt']
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO sp_sync_state (id, orders_synced_at, finances_synced_at, inventory_synced_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                orders_synced_at = excluded.orders_synced_at,
                finances_synced_at = excluded.finances_synced_at,
                inventory_synced_at = excluded.inventory_synced_at
            ''',
            (orders_synced_at, finances_synced_at, inventory_synced_at),
        )


def upsert_sp_orders(orders: list) -> int:
    """orders: [{orderId, purchaseDate, asin, sku, quantity, itemPriceUsd, orderStatus}, ...]"""
    init_ops_tables()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            '''
            INSERT INTO sp_orders (order_id, purchase_date, asin, sku, quantity, item_price_usd, order_status, updated_at)
            VALUES (:orderId, :purchaseDate, :asin, :sku, :quantity, :itemPriceUsd, :orderStatus, :updatedAt)
            ON CONFLICT(order_id) DO UPDATE SET
                purchase_date = excluded.purchase_date,
                asin = excluded.asin,
                sku = excluded.sku,
                quantity = excluded.quantity,
                item_price_usd = excluded.item_price_usd,
                order_status = excluded.order_status,
                updated_at = excluded.updated_at
            ''',
            [{**o, 'updatedAt': now} for o in orders],
        )
    return len(orders)


def upsert_sp_financial_events(order_id: str, events: list) -> int:
    """events: [{eventType, amountUsd, postedDate}, ...]. 既存の同order_id分は洗い替え。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('DELETE FROM sp_financial_events WHERE order_id = ?', (order_id,))
        conn.executemany(
            'INSERT INTO sp_financial_events (order_id, event_type, amount_usd, posted_date) VALUES (?, ?, ?, ?)',
            [(order_id, e['eventType'], e['amountUsd'], e.get('postedDate')) for e in events],
        )
    return len(events)


def replace_sp_fba_inventory(items: list) -> int:
    """items: [{asin, sku, fnsku, fulfillableQuantity}, ...]. 毎回スナップショット全洗い替え。"""
    init_ops_tables()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('DELETE FROM sp_fba_inventory')
        conn.executemany(
            '''
            INSERT INTO sp_fba_inventory (asin, sku, fnsku, fulfillable_quantity, snapshot_at)
            VALUES (:asin, :sku, :fnsku, :fulfillableQuantity, :snapshotAt)
            ''',
            [{**i, 'snapshotAt': now} for i in items],
        )
    return len(items)


def get_sd_parse_state() -> str | None:
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT last_parsed_at FROM sd_parse_state WHERE id = 1').fetchone()
    return row[0] if row else None


def set_sd_parse_state(parsed_at: str) -> None:
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO sd_parse_state (id, last_parsed_at) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET last_parsed_at = excluded.last_parsed_at
            ''',
            (parsed_at,),
        )


def upsert_jp_purchase_record(record: dict) -> bool:
    """record: {orderDate, sdReceptionNo, supplierName, sdProductNo, productName,
    janCode, variant, unitPriceJpy, quantity, amountJpy}. 受付番号が重複していれば
    スキップ(受付番号はSuper Delivery側で一意なので、同じメールを2回パースしても
    多重登録しない)。ASINは asin_jan_map から自動で引ければ埋める。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            'SELECT 1 FROM jp_purchase_records WHERE sd_reception_no = ?', (record['sdReceptionNo'],)
        ).fetchone()
        if existing:
            return False
        asin_row = conn.execute(
            'SELECT asin FROM asin_jan_map WHERE jan_code = ?', (record.get('janCode'),)
        ).fetchone()
        asin = asin_row[0] if asin_row else None
        conn.execute(
            '''
            INSERT INTO jp_purchase_records
                (order_date, sd_reception_no, supplier_name, sd_product_no, product_name,
                 jan_code, variant, unit_price_jpy, quantity, amount_jpy, asin)
            VALUES (:orderDate, :sdReceptionNo, :supplierName, :sdProductNo, :productName,
                    :janCode, :variant, :unitPriceJpy, :quantity, :amountJpy, :asin)
            ''',
            {**record, 'asin': asin},
        )
    return True


def set_asin_jan_map(asin: str, jan_code: str) -> None:
    """出品確定時に1回手動で呼ぶ。以後のjp_purchase_records取り込みでASINが自動で埋まる。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO asin_jan_map (asin, jan_code) VALUES (?, ?)
            ON CONFLICT(asin) DO UPDATE SET jan_code = excluded.jan_code
            ''',
            (asin, jan_code),
        )
        # 既存の未リンク行があれば今回のマッピングで埋める。
        conn.execute(
            'UPDATE jp_purchase_records SET asin = ? WHERE jan_code = ? AND asin IS NULL',
            (asin, jan_code),
        )


def add_fixed_cost(name: str, monthly_amount_jpy: float, effective_from: str | None = None, note: str | None = None) -> None:
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO fixed_costs (name, monthly_amount_jpy, effective_from, note) VALUES (?, ?, ?, ?)',
            (name, monthly_amount_jpy, effective_from, note),
        )


def list_fixed_costs() -> list:
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            'SELECT id, name, monthly_amount_jpy, effective_from, note FROM fixed_costs ORDER BY id'
        ).fetchall()
    return [
        {'id': i, 'name': n, 'monthlyAmountJpy': a, 'effectiveFrom': ef, 'note': note}
        for i, n, a, ef, note in rows
    ]


def compute_finance_summary(days: int = 30, usd_to_jpy: float = 150.0) -> dict:
    """収支ページ用の集計。売上(sp_orders)・COGS(jp_purchase_records、ASINごと
    直近仕入単価)・実手数料(sp_financial_events)・固定費(fixed_costs月額)・
    純利益・固定費カバー率を返す。SP-API/Gmail未設定でsp_orders等が空でも
    0埋めで正常に返る(例外を投げない)。"""
    init_ops_tables()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        orders = conn.execute(
            'SELECT order_id, asin, quantity, item_price_usd FROM sp_orders WHERE purchase_date >= ?',
            (since,),
        ).fetchall()
        fees_by_order = {}
        for row in conn.execute(
            '''
            SELECT order_id, SUM(amount_usd) AS total
            FROM sp_financial_events
            WHERE order_id IN (SELECT order_id FROM sp_orders WHERE purchase_date >= ?)
            GROUP BY order_id
            ''',
            (since,),
        ):
            fees_by_order[row['order_id']] = row['total'] or 0.0
        cost_by_asin = {}
        for row in conn.execute(
            '''
            SELECT asin, unit_price_jpy
            FROM jp_purchase_records
            WHERE asin IS NOT NULL
            ORDER BY order_date ASC
            '''
        ):
            cost_by_asin[row['asin']] = row['unit_price_jpy']  # 後勝ちで直近単価が残る
        fixed_cost_rows = list_fixed_costs()
        monthly_fixed_jpy = sum(r['monthlyAmountJpy'] for r in fixed_cost_rows)

    revenue_usd = sum((o['item_price_usd'] or 0) * (o['quantity'] or 0) for o in orders)
    fees_usd = sum(fees_by_order.values())
    cogs_usd = sum(
        (cost_by_asin.get(o['asin'], 0) or 0) / usd_to_jpy * (o['quantity'] or 0) for o in orders
    )
    fixed_cost_period_usd = (monthly_fixed_jpy / usd_to_jpy) * (days / 30.0)
    net_profit_usd = revenue_usd - fees_usd - cogs_usd - fixed_cost_period_usd

    # 内訳(CEO: 「固定費の内訳もわかるようにして」)
    fixed_costs_breakdown = [
        {
            'id': r['id'],
            'name': r['name'],
            'monthlyAmountJpy': r['monthlyAmountJpy'],
            'periodUsd': round((r['monthlyAmountJpy'] / usd_to_jpy) * (days / 30.0), 2),
            'note': r['note'],
        }
        for r in fixed_cost_rows
    ]

    return {
        'periodDays': days,
        'orderCount': len(orders),
        'revenueUsd': round(revenue_usd, 2),
        'cogsUsd': round(cogs_usd, 2),
        'feesUsd': round(fees_usd, 2),
        'fixedCostUsd': round(fixed_cost_period_usd, 2),
        'fixedCosts': fixed_costs_breakdown,
        'netProfitUsd': round(net_profit_usd, 2),
        'fixedCostCoveragePct': round(100.0 * (revenue_usd - fees_usd - cogs_usd) / fixed_cost_period_usd, 1)
        if fixed_cost_period_usd > 0 else None,
    }


def get_sp_fba_inventory_with_days_of_stock(days_for_velocity: int = 30) -> list:
    """在庫日数付きのFBA在庫一覧。直近days_for_velocity日の販売ペースから算出。"""
    init_ops_tables()
    since = (datetime.now(timezone.utc) - timedelta(days=days_for_velocity)).date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        sold_by_asin = {
            row['asin']: row['total_qty']
            for row in conn.execute(
                'SELECT asin, SUM(quantity) AS total_qty FROM sp_orders WHERE purchase_date >= ? GROUP BY asin',
                (since,),
            )
        }
        inventory = conn.execute(
            'SELECT asin, sku, fnsku, fulfillable_quantity, snapshot_at FROM sp_fba_inventory'
        ).fetchall()

    result = []
    for row in inventory:
        sold = sold_by_asin.get(row['asin'], 0) or 0
        daily_rate = sold / days_for_velocity if days_for_velocity else 0
        days_of_stock = (row['fulfillable_quantity'] / daily_rate) if daily_rate > 0 else None
        result.append(
            {
                'asin': row['asin'],
                'sku': row['sku'],
                'fnsku': row['fnsku'],
                'fulfillableQuantity': row['fulfillable_quantity'],
                'snapshotAt': row['snapshot_at'],
                'soldLast30d': sold,
                'daysOfStock': round(days_of_stock, 1) if days_of_stock is not None else None,
            }
        )
    return result


if __name__ == '__main__':
    init_ops_tables()
    print(f'Ops/Finance テーブルを初期化しました: {DB_PATH}')
