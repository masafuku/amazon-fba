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

            -- Orders API (getOrderItems) の商品明細。getOrders自体にはASINが
            -- 含まれないため別テーブル(1注文=複数商品のこともあるため、sp_ordersに
            -- 直接持たせず1:多で持つ)。商品ごとのP&Lはこちらをsp_ordersとJOINして出す。
            CREATE TABLE IF NOT EXISTS sp_order_items (
                order_id TEXT NOT NULL,
                asin TEXT,
                sku TEXT,
                quantity INTEGER,
                item_price_usd REAL,
                PRIMARY KEY (order_id, asin)
            );
            CREATE INDEX IF NOT EXISTS idx_sp_order_items_asin ON sp_order_items(asin);

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

            -- Fulfillment Inbound API(v2024-03-20)。FBA納品便(Send to Amazonで作る
            -- 納品プラン)ごとのP&L用。1プラン=1納品便が現状の実運用(複数便への分割は
            -- 未対応、items は planId 単位でのみ取得できるため、分割時は全shipmentに
            -- 同じitems一覧が入る簡略実装)。
            CREATE TABLE IF NOT EXISTS sp_inbound_shipments (
                shipment_id TEXT PRIMARY KEY,
                plan_id TEXT,
                shipment_confirmation_id TEXT,  -- 例: FBA19RGV6RMN(セラーセントラル表示のID)
                status TEXT,
                destination_fc TEXT,
                delivery_window_start TEXT,
                delivery_window_end TEXT,
                created_at TEXT,
                synced_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sp_inbound_shipments_confirmation_id ON sp_inbound_shipments(shipment_confirmation_id);

            CREATE TABLE IF NOT EXISTS sp_inbound_shipment_items (
                shipment_id TEXT NOT NULL,
                asin TEXT,
                sku TEXT,
                quantity INTEGER,
                PRIMARY KEY (shipment_id, asin)
            );
            CREATE INDEX IF NOT EXISTS idx_sp_inbound_shipment_items_asin ON sp_inbound_shipment_items(asin);

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
                asin TEXT,                     -- asin_jan_map経由で手動リンク(未リンクならNULL)
                shipping_cost_jpy REAL         -- この行(商品)に配分された送料(円)。
                                                -- allocate_order_shipping()が、同じ
                                                -- supplier_name+order_dateの行に数量按分で
                                                -- 書き込む(CEO: 「送料は商品ごとに配分して」)。
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

            -- NETSEA(Buyer API)の全カタログのうち、JANコード付きのもの(セット単位)。
            -- scripts/sync_netsea_catalog.py が定期的に入れ替える。/items にJAN検索が無いため、
            -- 候補のJANとの突き合わせ(scripts/check_candidate_wholesale.py)はこの表に対して行う。
            CREATE TABLE IF NOT EXISTS netsea_catalog (
                supplier_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                direct_item_id TEXT NOT NULL,
                jan_code TEXT NOT NULL,
                shop_name TEXT,
                product_name TEXT,
                product_url TEXT,
                set_num INTEGER,                -- 1セットの個数(最小ロット)
                unit_price_jpy REAL,            -- 1個あたりの卸価格(税抜)
                set_price_jpy REAL,             -- 1セットの価格(税抜)
                sold_out INTEGER NOT NULL DEFAULT 0,
                image_copy_flag TEXT,
                direct_send_flag TEXT,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (supplier_id, product_id, direct_item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_netsea_catalog_jan ON netsea_catalog(jan_code);
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
        # 発注の優先度Tier(S/A+/A-/B+/B-/C+/C-/D)・完全除外の種別(food/drug_cosmetic/knife)・
        # フィギュアのフラグ。既存行は NULL/0 のまま(一括の再分類は
        # scripts/backfill_priority_tier.py で明示的に行う)。
        # keepa-csv-dashboard/sqlite_api_server.py にも同じマイグレーションがある。
        if 'priority_tier' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN priority_tier TEXT')
        if 'excluded_kind' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN excluded_kind TEXT')
        if 'is_figure' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN is_figure INTEGER NOT NULL DEFAULT 0')
        # Amazon.comでの出品可否(SP-APIのListings Restrictions APIで照会した結果:
        # ok / approval_required / not_eligible / restricted。NULL=未確認)と、その照会日時。
        # keepa-csv-dashboard/sqlite_api_server.py にも同じマイグレーションがある。
        if 'listing_status' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN listing_status TEXT')
        if 'listing_checked_at' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN listing_checked_at TEXT')
        # tierはpass/consider/reference/rejectという旧・利益率ベースの合否判定列。
        # 2026-09-28、priority_tier(発注優先度Tier)への統合でqualifiedの計算元から
        # 外れ、以後どこからも書き込まれない(過去データはそのまま残す。DROP COLUMN
        # はしない)。ADD COLUMN自体は、新規DBが本番と同じスキーマになるよう残す。
        if 'tier' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN tier TEXT')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_seller_id ON agent_candidates(seller_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_source_type ON agent_candidates(source_type)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_tier ON agent_candidates(tier)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_priority_tier ON agent_candidates(priority_tier)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_excluded_kind ON agent_candidates(excluded_kind)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_listing_status ON agent_candidates(listing_status)')

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

        jp_purchase_records_columns = {row[1] for row in conn.execute('PRAGMA table_info(jp_purchase_records)').fetchall()}
        if 'shipping_cost_jpy' not in jp_purchase_records_columns:
            conn.execute('ALTER TABLE jp_purchase_records ADD COLUMN shipping_cost_jpy REAL')

        sp_sync_state_columns = {row[1] for row in conn.execute('PRAGMA table_info(sp_sync_state)').fetchall()}
        if 'inbound_synced_at' not in sp_sync_state_columns:
            conn.execute('ALTER TABLE sp_sync_state ADD COLUMN inbound_synced_at TEXT')

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


# ---------------------------------------------------------------------------
# TNKの実際の運賃表(依頼があったときだけ送料を試算する用。calc_unit_profit()の
# 自動判定には使わない — CEO: 「重量は考えるのが難しいのでゼロのままでよい」
# 「送料は依頼したときに計算してください。計算の際には、TNKで一番お得になる
# 金額帯で計算して」2026-09-27)。
#
# 出典: 【2026年】TNK Logistics配送料金(Googleスプレッドシート、TNKから共有)
# https://docs.google.com/spreadsheets/d/1tFWiBODxykXF83-yTJQECioZk4gPvtklSAce8nbAHPU
# シート「[2026/6/1〜] TNK STANDARD」・主要エリア「アメリカ」。上のEMS表ベースの
# 見積もり(_shipping_cost_jpy_for_weight、単一の見積もりメールをスケールしたもの)
# より、TNKと直接契約した実際の運賃のほうが正確。表は0.5kg刻みで30kgまで
# (それ以降は未収録)。1kgあたりの単価は、重いほど下がり続け、収録範囲内で
# 最安なのは表の上限の30kg(¥938/kg)。
_TNK_STANDARD_US_RATE_TABLE_JPY: list[tuple[float, float]] = [
    (0.5, 4080), (1.0, 4409), (1.5, 4739), (2.0, 5066), (2.5, 5420),
    (3.0, 5880), (3.5, 6084), (4.0, 6535), (4.5, 6994), (5.0, 7465),
    (5.5, 9317), (6.0, 9688), (6.5, 10084), (7.0, 10456), (7.5, 10780),
    (8.0, 11031), (8.5, 11322), (9.0, 11577), (9.5, 12286), (10.0, 12586),
    (10.5, 13020), (11.0, 13318), (11.5, 13593), (12.0, 13863), (12.5, 14188),
    (13.0, 14229), (13.5, 14617), (14.0, 14872), (14.5, 15197), (15.0, 15554),
    (15.5, 15958), (16.0, 16156), (16.5, 16399), (17.0, 16815), (17.5, 17065),
    (18.0, 17259), (18.5, 17666), (19.0, 18059), (19.5, 18400), (20.0, 18818),
    (20.5, 20544), (21.0, 20867), (21.5, 21244), (22.0, 22167), (22.5, 22539),
    (23.0, 22862), (23.5, 23247), (24.0, 23667), (24.5, 24000), (25.0, 24436),
    (25.5, 24798), (26.0, 25231), (26.5, 25557), (27.0, 25938), (27.5, 26292),
    (28.0, 26699), (28.5, 27025), (29.0, 27409), (29.5, 27794), (30.0, 28149),
]
_TNK_CHEAPEST_RATE_JPY_PER_KG = (
    _TNK_STANDARD_US_RATE_TABLE_JPY[-1][1] / _TNK_STANDARD_US_RATE_TABLE_JPY[-1][0]
)  # 表の最安帯(30kg)のキロ単価。「一番お得になる金額帯」の基準値。
TNK_SHIPPING_QUOTE_DATE = '2026-06-01'   # この運賃表が適用される日付(それ以前は別表)


def estimate_tnk_shipping_cost_jpy_per_unit(weight_kg: float, use_cheapest_bracket: bool = True) -> dict:
    """依頼があったときに、TNKの実際の運賃表から、1個あたりの国際送料(円)を試算する。
    calc_unit_profit()には使わない(自動判定は送料ゼロのまま、CEO 2026-09-27)。

    use_cheapest_bracket=True(既定): 「TNKで一番お得になる金額帯」(表の最安、30kgの
    キロ単価¥938/kg)を使う想定 — 他の商品と一緒に発送してまとめて30kg以上にする前提。
    False: この商品だけを、その重量ぴったりで送る前提(表を線形補間、30kg超は末尾の
    傾きで延長)。

    戻り値: {'per_unit_jpy', 'rate_jpy_per_kg', 'basis'}"""
    if use_cheapest_bracket:
        return {
            'per_unit_jpy': round(weight_kg * _TNK_CHEAPEST_RATE_JPY_PER_KG, 1),
            'rate_jpy_per_kg': round(_TNK_CHEAPEST_RATE_JPY_PER_KG, 1),
            'basis': f'TNK STANDARD({TNK_SHIPPING_QUOTE_DATE}〜)の最安帯(30kg、¥{_TNK_CHEAPEST_RATE_JPY_PER_KG:.0f}/kg)',
        }
    table = _TNK_STANDARD_US_RATE_TABLE_JPY
    if weight_kg <= table[0][0]:
        total_jpy = table[0][1]
    elif weight_kg >= table[-1][0]:
        (w1, c1), (w2, c2) = table[-2], table[-1]
        slope = (c2 - c1) / (w2 - w1)
        total_jpy = c2 + slope * (weight_kg - w2)
    else:
        total_jpy = table[-1][1]
        for (w1, c1), (w2, c2) in zip(table, table[1:]):
            if w1 <= weight_kg <= w2:
                ratio = (weight_kg - w1) / (w2 - w1)
                total_jpy = c1 + ratio * (c2 - c1)
                break
    return {
        'per_unit_jpy': round(total_jpy, 1),
        'rate_jpy_per_kg': round(total_jpy / weight_kg, 1) if weight_kg else None,
        'basis': f'TNK STANDARD({TNK_SHIPPING_QUOTE_DATE}〜)、この商品単独の重量({weight_kg}kg)で按分',
    }


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
    # CEO指示(2026-09-22): 想定輸送費が妥当でないため0円とする。重量連動モデル
    # (_shipping_cost_jpy_for_weight)は残してあるが、利益計算では使わない。
    shipping_cost_usd = 0.0
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

# CEO: 「輸出ビジネスだと利益率よりも、ROIの方が適切な指標では？」— 資金を回転させて
# 増やしていくビジネスモデルでは、投下資本(JP原価)に対する回収率(ROI)が資金効率の
# 観点で本質的に重要。2026-09-28、CEO判断で旧・合否判定(tier: pass/consider/
# reference/reject、利益率の安全弁MIN_MARGIN_PCT込み)を廃止し、発注優先度Tier
# (priority_tier)に一本化した。qualifiedはpriority_tierから導出する
# (QUALIFIED_PRIORITY_TIERS / is_qualified_priority_tier、下記927行目付近で定義)。
MIN_ROI_PCT = 0.20                # priority_tierのROI帯境界(PRIORITY_B_SPLIT_ROI)の元値

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


# 発注の優先度Tier(S/A+/A-/B+/B-/C+/C-/D)。2026-09-28にCEOと再設計:
# 旧体系(S/A/B+/B-/C)は、monthly_soldの実数値(Keepa確定値)とBSR絶対値による
# 推定が区別されず、Sだけ利益$3以上という非対称な足切りがあり、メディアだけ
# 別基準(ランク変動30日)を使うなど一貫性を欠いていた。新体系は「実売の確度」
# (強い実売 > 実売あり > ランク変動のみ > 実売なし)と「ROI水準」の
# 4x4マトリクスで決まる、メディアの特別扱いは廃止(全商品共通の基準)。
# 競合(出品者)数は判定に入れない(メモ欄扱い。CEO確認済み)。日本の実売は使わない。
# 仕入れ先(卸)が確定しているかどうかもTierには反映しない(CEO確認済み、対象外)。
#
#     実売の確度 ＼ ROI  | >=100%  | 20~100% | 0~20% | <0%(赤字)
#     強い実売(>=100件)   |   S     |   A+    |  C+   |   C-
#     実売あり(30~99件)   |   A+    |   A-    |  C+   |   C-
#     ランク変動のみ       |   B+    |   B-    |  C+   |   C-
#     実売なし            |   D     |   D     |  D    |   D
#
# 実売なしはROIに関わらず無条件でD。roi_pctがNone(判定不能)の場合も一律D。
PRIORITY_S = 'S'
PRIORITY_A_PLUS = 'A+'
PRIORITY_A_MINUS = 'A-'
PRIORITY_B_PLUS = 'B+'
PRIORITY_B_MINUS = 'B-'
PRIORITY_C_PLUS = 'C+'
PRIORITY_C_MINUS = 'C-'
PRIORITY_D = 'D'
PRIORITY_TIER_ORDER = ('S', 'A+', 'A-', 'B+', 'B-', 'C+', 'C-', 'D')  # 優先度の高い順

PRIORITY_STRONG_MONTHLY_SOLD = 100      # 先月100点以上(実数値) = 強い実売
PRIORITY_REAL_MIN_MONTHLY_SOLD = 30     # 先月30点以上(実数値) = 実売あり
PRIORITY_HIGH_ROI = 1.0                 # ROI 100%以上の帯
PRIORITY_B_SPLIT_ROI = MIN_ROI_PCT      # ROI 20%(CEO指定 2026-09-28: 利益面の合格ライン)の帯
# ランク変動ベースの実売判定基準。旧・メディア専用の閾値だったが、2026-09-28に
# 全商品共通の基準として採用(メディアの特別扱いは廃止)。
PRIORITY_MIN_RANK_DROPS_30 = 10


def _classify_priority_tier(
    roi_pct: float | None,
    monthly_sold: int | None,
    sales_rank_drops_30: int | None,
) -> str:
    """発注の優先度Tier(S/A+/A-/B+/B-/C+/C-/D)を返す。上部のコメントにある
    4x4マトリクス(実売の確度 x ROI水準)そのものの実装。

    実売の確度(排他的、この優先順で判定):
    - 強い実売: monthly_soldが実数値 かつ >= 100
    - 実売あり: monthly_soldが実数値 かつ 30 <= monthly_sold < 100
    - ランク変動のみ: monthly_soldが無く(None)、sales_rank_drops_30 >= 10
    - 実売なし: 上記以外(monthly_soldが実数値だが30未満、または両方無し/不足)

    実売なしはROIに関わらず一律D。roi_pctがNone(判定不能)も一律D。
    """
    if monthly_sold is not None and monthly_sold >= PRIORITY_STRONG_MONTHLY_SOLD:
        evidence = 'strong'
    elif monthly_sold is not None and monthly_sold >= PRIORITY_REAL_MIN_MONTHLY_SOLD:
        evidence = 'real'
    elif monthly_sold is None and sales_rank_drops_30 is not None and sales_rank_drops_30 >= PRIORITY_MIN_RANK_DROPS_30:
        evidence = 'rank'
    else:
        evidence = 'none'

    if evidence == 'none':
        return PRIORITY_D
    if roi_pct is None:
        return PRIORITY_D

    if roi_pct >= PRIORITY_HIGH_ROI:
        if evidence == 'strong':
            return PRIORITY_S
        if evidence == 'real':
            return PRIORITY_A_PLUS
        return PRIORITY_B_PLUS  # rank
    if roi_pct >= PRIORITY_B_SPLIT_ROI:
        if evidence in ('strong', 'real'):
            return PRIORITY_A_MINUS if evidence == 'real' else PRIORITY_A_PLUS
        return PRIORITY_B_MINUS  # rank
    if roi_pct >= 0:
        return PRIORITY_C_PLUS
    return PRIORITY_C_MINUS


# 2026-09-28、CEO判断で旧・合否判定(tier: pass/consider/reference/reject)を廃止し、
# qualifiedはpriority_tierから直接導出する形に統合した。「実売証拠があり、ROIが
# 赤字でない」ものを合格とする(C+=ROI0〜20%も合格に含む、CEO: 「C+も合格では？」)。
QUALIFIED_PRIORITY_TIERS = ('S', 'A+', 'A-', 'B+', 'B-', 'C+')

# 8値のpriority_tierを優先度順に並べるSQLのCASE式。ops_finance.py側のORDER BYや、
# ダッシュボードAPI(sqlite_api_server.py)側で同じ並びを再現する際に使う
# (後者は独立スキーマの都合上インポートできないため、この文字列をコピーする)。
PRIORITY_TIER_SQL_ORDER = 'CASE priority_tier ' + ' '.join(
    f"WHEN '{tier}' THEN {index}" for index, tier in enumerate(PRIORITY_TIER_ORDER)
) + f' ELSE {len(PRIORITY_TIER_ORDER)} END'


def is_qualified_priority_tier(priority_tier: str | None) -> bool:
    """priority_tierから合格/不合格を判定する。None(完全除外品)・C-(赤字)・
    D(実売証拠なし)は不合格、それ以外(S/A+/A-/B+/B-/C+)は合格。"""
    return priority_tier in QUALIFIED_PRIORITY_TIERS


def _priority_rejection_reason(entry: dict) -> str | None:
    """不合格(is_qualified_priority_tier==False)の場合に表示する理由文言を返す。
    合格の場合はNone。旧tierと違い、利益率(margin_pct)には一切言及しない
    (2026-09-28、利益率の安全弁は廃止されたため)。"""
    priority_tier = entry.get('priority_tier')
    if is_qualified_priority_tier(priority_tier):
        return None
    roi_pct = entry.get('roi_pct')
    if priority_tier == PRIORITY_C_MINUS:
        return f"ROI {roi_pct:.0%}(赤字)" if roi_pct is not None else "ROI 赤字"
    # ここに来るのはD、またはpriority_tier自体が未計算(None、通常は起きないが念のため)
    if roi_pct is None:
        return "ROIを計算できない(価格・原価データ不足)"
    monthly_sold = entry.get('monthly_sold')
    drops = entry.get('sales_rank_drops_30')
    return (
        f"米国の実売の根拠なし(先月の販売 {monthly_sold if monthly_sold is not None else '-'}件"
        f" / ランク変動30日 {drops if drops is not None else '-'}回。"
        f"基準: 30件以上、または販売数不明でランク変動10回以上)"
    )


# 完全除外カテゴリ(輸出に課題があるため。CEO: 「食品、医薬品、刃物は輸出に課題が
# あるので完全除外」)。刃物は包丁・ナイフ類のみ(はさみ・カッターは除外しない)。
# キーワード検索では広い語(既存の_FOOD_AND_UNSUITABLE_KEYWORDSなど)で弾くが、
# 商品タイトルにそのまま当てると「rice cooker」「food scale」のような器具まで
# 誤検知するため、タイトル用は誤検知の少ない語に絞り、さらに器具・容器を示す語
# (_NON_CONSUMABLE_HINTS)を含むタイトルは除外しない。
EXCLUDED_FOOD = 'food'
EXCLUDED_DRUG_COSMETIC = 'drug_cosmetic'
EXCLUDED_KNIFE = 'knife'
EXCLUDED_HAZMAT = 'hazmat'   # 危険物(引火性液体等)。国際輸送・FBAの審査を通せないため完全除外
                             # (CEO確認 2026-09-27: SOFT99 ガラコ ロールオンのSDSで引火性液体H225を確認)

_FOOD_TITLE_KEYWORDS = (
    'snack', 'snacks', 'candy', 'candies', 'chocolate', 'chocolates', 'cracker', 'crackers',
    'green tea', 'black tea', 'oolong tea', 'tea bag', 'tea bags', 'tea leaves', 'loose tea',
    'matcha', 'cocoa', 'coffee beans', 'ground coffee', 'instant coffee',
    'noodle', 'noodles', 'ramen', 'udon', 'soba', 'soy sauce', 'seasoning', 'seasonings',
    'furikake', 'dashi', 'mochi', 'senbei', 'pocky', 'wagyu', 'seafood', 'onigiri', 'sushi',
    'gourmet food', 'grocery', 'groceries', 'liquor', 'whisky', 'whiskey', 'sake bottle',
)
# 医薬部外品(quasi-drug)の入浴剤等。日本の薬機法上の分類で、成分表示・承認のハードルが
# 化粧品と同様にあるため、_DRUG_COSMETIC_KEYWORDSに合流させる(CEO確認 2026-09-27:
# 花王バブは医薬部外品)。
_QUASI_DRUG_KEYWORDS = (
    'bath tablet', 'bath tablets', 'bath salt', 'bath salts', 'medicated bath', 'bath bomb',
    'kao babu', 'babu bath',   # 花王バブ(医薬部外品の入浴剤)。実例のAmazonタイトルは
                                # 機械翻訳で語順が崩れており「bath tablet」に一致しないため個別に追加。
)
_DRUG_COSMETIC_KEYWORDS = _QUASI_DRUG_KEYWORDS + (
    'supplement', 'supplements', 'vitamin', 'vitamins', 'medicine', 'medicines', 'medicated',
    'drug', 'drugs', 'pharmaceutical', 'cosmetic', 'cosmetics', 'makeup', 'skincare', 'skin care',
    'shampoo', 'conditioner', 'toothpaste', 'mouthwash', 'lotion', 'serum', 'sunscreen',
    'face mask', 'facial mask', 'moisturizer', 'moisturizing cream', 'cleansing oil',
    'cleansing foam', 'eye drops', 'hair dye', 'hair color', 'lip balm', 'lipstick',
)
_KNIFE_KEYWORDS = (
    'knife', 'knives', 'santoku', 'gyuto', 'nakiri', 'deba', 'yanagiba', 'kiritsuke',
    'kitchen knife', 'chef knife', "chef's knife", 'paring knife', 'bread knife',
)
# 危険物(引火性液体・エアゾール等)。国際輸送で航空便に載せられない、またはFBAの
# 危険物審査が必要になる製品群。ロールオン式のガラスコーティング剤(引火性溶剤入り)を
# 実例として確認したので、その系統の商品名をタイトル・キーワードの両方で弾く。
_HAZMAT_KEYWORDS = (
    'glaco', 'glass sealant', 'glass coating', 'rain repellent', 'windshield sealant',
    'lighter fluid', 'contact cement', 'spray paint', 'rust preventive spray',
)
_NON_CONSUMABLE_HINTS = (
    'holder', 'shelf', 'shelves', 'rack', 'dispenser', 'container', 'containers', 'storage',
    'organizer', 'kettle', 'kettles', 'scale', 'scales', 'cooker', 'maker', 'mold', 'molds',
    'grinder', 'mug', 'cup', 'cups', 'glass', 'glasses', 'sharpener', 'sharpening', 'case',
    'bottle', 'tray', 'stand', 'pouch', 'bag', 'brush', 'towel', 'mat', 'toy', 'sticker',
    # 本番データのドライラン(2026-09-26)で見つかった誤検知: ゲーム・食器・工具・書籍・
    # マスク・ヘアクリップなど、語を含むだけで消費物ではない商品。
    'game', 'bowl', 'bowls', 'blade', 'blades', 'colander', 'candle', 'candles', 'face guard', 'clip', 'clips',
    'thread', 'handbook', 'philosophy', 'reading', 'trainer', 'exerciser', 'keychain', 'stopper',
)
# 包丁類の除外から外す語。器具・付属品(砥石・ホルダー・鞘・ケア用品)、および
# 「カッター」(CEO: 刃物は包丁・ナイフ類のみ。はさみ・カッターは除外しない)
# ―― utility knife / cutter knife / snap-off / craft・hobby knife、バターナイフ(食卓用)。
_KNIFE_ACCESSORY_PATTERN = re.compile(
    r'\b(?:sharpener|sharpening|holder|sheath|guard|case|stand|rack|cover|care kit|'
    r'utility|cutter|snap-off|craft|hobby|butter knife)\b|knife block(?!\s+set)'
)


def _build_word_pattern(terms) -> 're.Pattern':
    return re.compile(
        r'\b(?:' + '|'.join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True)) + r')\b'
    )


_FOOD_TITLE_PATTERN = _build_word_pattern(_FOOD_TITLE_KEYWORDS)
_DRUG_COSMETIC_PATTERN = _build_word_pattern(_DRUG_COSMETIC_KEYWORDS)
_KNIFE_PATTERN = _build_word_pattern(_KNIFE_KEYWORDS)
_HAZMAT_PATTERN = _build_word_pattern(_HAZMAT_KEYWORDS)
# 化粧品・ヘアケアのブランド名。検索キーワードに使うと、結果は全件が化粧品(完全除外)で
# 弾かれ、Keepaのトークンを無駄にするため、キーワード判定(is_title=False)でだけ使う。
# (タイトル判定には使わない: ブランド名だけでは化粧品と限らない商品が混ざるため)
_COSMETIC_BRAND_KEYWORDS = (
    'dhc', 'biore', 'canmake', 'hada labo', 'kanebo', 'kose', 'sk-ii', 'sk ii', 'skii',
    'bihada ichizoku', 'shiseido', 'kracie', 'lululun', 'milbon', 'senka', 'rohto',
)
_COSMETIC_BRAND_PATTERN = _build_word_pattern(_COSMETIC_BRAND_KEYWORDS)
_NON_CONSUMABLE_PATTERN = _build_word_pattern(_NON_CONSUMABLE_HINTS)


def _excluded_kind(text: str | None, is_title: bool = False) -> str | None:
    """食品・医薬品/化粧品・刃物(包丁・ナイフ類)・危険物のいずれかに当たれば、その種別
    ('food'/'drug_cosmetic'/'knife'/'hazmat')を返す。当たらなければNone。
    is_title=True は商品タイトルに使う(誤検知を減らすため、狭い語だけを使い、
    器具・容器を示す語を含むタイトルは除外しない)。False は検索キーワード用
    (既存の広い食品リストも使う)。危険物(_HAZMAT_PATTERN)は器具語による除外の
    対象にしない(輸送上の危険性はタイトルの器具語と無関係なため)。"""
    if not text:
        return None
    lowered = text.strip().lower()
    if is_title:
        # フィギュア/コレクタブルは、優先度Tierを付けて表示のオン/オフで扱う(完全除外にしない)。
        # 「Noodle Stopper Figure」「Snack Series Blind Box」のような、食品語を含む
        # フィギュアの誤検知を避ける。
        if _is_figure_or_collectible_keyword(lowered):
            return None
        if _HAZMAT_PATTERN.search(lowered):
            return EXCLUDED_HAZMAT
        if _NON_CONSUMABLE_PATTERN.search(lowered):
            # 例: 「Food Scale」「Tea Kettle」「Shampoo Holder」「Knife Sharpener」。
            # ただし包丁そのもの(「Chef's Knife Set」等)は器具語を含まないので除外される。
            pass
        else:
            if _FOOD_TITLE_PATTERN.search(lowered):
                return EXCLUDED_FOOD
            if _DRUG_COSMETIC_PATTERN.search(lowered):
                return EXCLUDED_DRUG_COSMETIC
        if _KNIFE_PATTERN.search(lowered) and not _KNIFE_ACCESSORY_PATTERN.search(lowered):
            return EXCLUDED_KNIFE
        return None
    if _HAZMAT_PATTERN.search(lowered):
        return EXCLUDED_HAZMAT
    if _is_food_or_unsuitable_keyword(lowered):
        return EXCLUDED_FOOD
    if _DRUG_COSMETIC_PATTERN.search(lowered) or _COSMETIC_BRAND_PATTERN.search(lowered):
        return EXCLUDED_DRUG_COSMETIC
    if _KNIFE_PATTERN.search(lowered):
        return EXCLUDED_KNIFE
    return None


# 出品制限(ゲーティング)で、Amazon.comに新規出品できないと確認したブランド。
# CEOがSeller Centralで確認したものだけを入れる(推測で増やさない)。
#   - タカラトミー(ベイブレードX等): 2026-09-26、ブランドの出品許可が無く出品申請が受理されない
#   - HARIO: 2026-09-27、「現在、この商品の新しい出品情報は受け付けておりません」
# 該当する商品は、評価・優先度Tierは通常どおり付けるが、ダッシュボードでは
# 「出品制限ブランドを隠す」(既定オン)で隠し、検索キーワードからは外す。
# keepa-csv-dashboard/sqlite_api_server.py にも同じリストがある(2ファイルの並行管理)。
GATED_BRAND_TERMS = {
    'HARIO': ('hario', 'ハリオ'),
    'タカラトミー': ('takara tomy', 'takaratomy', 'タカラトミー', 'beyblade', 'ベイブレード'),
    # MUJI: 卸ルートが無く、ネットストアの規約が転売目的の購入を禁止(2026-09-27、CEO判断で見送り)。
    '無印良品(MUJI)': ('muji', '無印良品', '良品計画'),
}


def _gated_brand(*texts) -> str | None:
    """タイトル・ブランド名などのテキストが、出品制限ブランドに当たれば、そのブランドの
    表示名を返す(当たらなければNone)。英字の語は前後が英数字でないときだけ一致
    (例: 'hario' が 'mario' に誤マッチしない)。"""
    haystack = ' '.join(t for t in texts if t).lower()
    if not haystack:
        return None
    for label, terms in GATED_BRAND_TERMS.items():
        for term in terms:
            if term.isascii():
                if re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', haystack):
                    return label
            elif term in haystack:
                return label
    return None


_MEDIA_TITLE_PATTERN = re.compile(
    r'\bdvd\b|blu-?ray|\bcd\b|\bsoundtrack\b|bonus track|\bost\b|\balbum\b|\bshm\b|'
    r'\bremaster(?:ed)?\b|japanese edition|\(vo japonais\)',
    re.IGNORECASE,
)


def _is_media(asin: str | None, title: str | None) -> bool:
    """本・DVD/Blu-ray・CD(メディア)らしい商品かどうか。ASINが10桁の数字(ISBN-10)なら本。
    タイトルに DVD/Blu-ray/CD/サウンドトラック/Japanese Edition などを含むものも該当。
    メディアのランキングは「そのカテゴリ内の順位」で、雑貨と同じ基準では実売を判断できない
    ため、朝のLINE通知などから外す(CEO指示 2026-09-27)。推定なので誤検知はあり得る。"""
    if asin and re.fullmatch(r'\d{9}[\dX]', asin.strip().upper()):
        return True
    return bool(title and _MEDIA_TITLE_PATTERN.search(title))


def _apply_priority_fields(entry: dict) -> dict:
    """候補entryに、優先度Tier(priority_tier)・完全除外の種別(excluded_kind)・
    フィギュアのフラグ(is_figure)を付ける(in-place。entryを返す)。
    完全除外に当たる候補は priority_tier を付けない。"""
    title = entry.get('title') or ''
    kind = _excluded_kind(title, is_title=True)
    entry['excluded_kind'] = kind
    entry['is_figure'] = bool(_is_figure_or_collectible_keyword(title))
    entry['priority_tier'] = None if kind else _classify_priority_tier(
        entry.get('roi_pct'), entry.get('monthly_sold'), entry.get('sales_rank_drops_30'),
    )
    return entry


def _qualified_sort_key(entry: dict) -> tuple:
    """qualifiedリストの並び順: priority_tier(S>A+>...>D)昇順、
    同Tier内はroi_pct降順(Noneは最後)。"""
    tier_index = PRIORITY_TIER_ORDER.index(entry['priority_tier']) if entry.get('priority_tier') in PRIORITY_TIER_ORDER else len(PRIORITY_TIER_ORDER)
    roi_pct = entry.get('roi_pct')
    return (tier_index, roi_pct is None, -roi_pct if roi_pct is not None else 0)


def evaluate_mcp_candidates(
    mcp_result: dict,
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

    合否(qualified)はpriority_tierから導出する(is_qualified_priority_tier、
    2026-09-28に旧・利益率ベースのtier判定から統合)。

    Returns:
        {
          'qualified': [priority_tierが合格範囲の候補 + profit詳細],
          'rejected':  [priority_tierが不合格範囲だった候補 + 理由],
          'weight_missing': [重量データが無く仮値で計算した候補のASIN一覧],
          'fee_missing': [手数料データが無く仮値で計算した候補のASIN一覧],
        }
    """
    qualified, rejected, weight_missing, fee_missing = [], [], [], []

    for candidate in mcp_result.get('candidates', []):
        sell = candidate['sell']
        cost = candidate['cost']
        asin = sell['asin']

        # ブランド名等の広いキーワード("Sanrio"等)で検索すると、食品・医薬品/
        # 化粧品・包丁類(輸出に課題があり、CEOが完全除外と決めたカテゴリ)も
        # ヒットしてしまうため、キーワード自体が問題ない場合でも商品タイトル
        # 段階でここで弾く(価格計算もせず、優先度Tierも付けない。ダッシュボード
        # には出さない)。フィギュア/コレクタブルは、以前はここで弾いていたが、
        # CEO指示(2026-09-26)で優先度Tierを付けるようになったため、弾かずに
        # 通常どおり評価し、is_figureの印だけを付ける(ダッシュボードの
        # 「フィギュアを隠す」で表示をオン/オフする)。
        title = sell.get('title') or ''
        excluded_kind = _excluded_kind(title, is_title=True)
        if excluded_kind:
            rejected.append({
                'asin': asin,
                'title': title,
                'url': sell.get('url'),
                'image_url': sell.get('image_url'),
                'us_price_usd': sell.get('price'),
                'jp_cost_jpy': cost.get('price'),
                'reason': f'excluded_{excluded_kind}',
                'excluded_kind': excluded_kind,
                'is_figure': False,
                'priority_tier': None,
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
        _apply_priority_fields(entry)

        if is_qualified_priority_tier(entry['priority_tier']):
            qualified.append(entry)
        else:
            entry['reason'] = _priority_rejection_reason(entry)
            rejected.append(entry)

    qualified.sort(key=_qualified_sort_key)

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

        # 粗選別で落ちた行は、価格が両方揃ってpriority_tierが合格範囲になったとしても
        # qualifiedには入れない(粗選別自体の判断=価格差率や変動率の問題を優先する)。
        # reasonは上で設定した「粗選別で除外」のまま変えない。
        _apply_priority_fields(entry)
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


LISTING_CHECK_TIERS = ('S', 'A+', 'A-', 'B+')   # 出品可否を照会する優先度Tier(照会の件数を抑えるため上位だけ)


def load_asins_needing_listing_check(limit: int = 30, max_age_days: int = 7, all_tiers: bool = False) -> list:
    """出品可否を照会すべきASINを返す: 優先度Tier S/A+/A-/B+ で、完全除外・フィギュア・メディア・
    出品制限ブランドではなく、未確認、または前回の照会から max_age_days 日以上たったもの。
    Tierの高い順、同じTierでは新しい順。

    all_tiers=True のときは、優先度Tierに関係なく、また、メディア・フィギュア・出品制限ブランドも
    含めて、完全除外(食品・医薬品/化粧品・刃物)以外の全ASINを対象にする(全件の一括照会用)。"""
    init_ops_tables()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    placeholders = ', '.join('?' for _ in LISTING_CHECK_TIERS)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f'''
            WITH ranked AS (
                SELECT asin, title, priority_tier, listing_status, listing_checked_at, created_at, data_json,
                    ROW_NUMBER() OVER (PARTITION BY asin ORDER BY created_at DESC) AS rn
                FROM agent_candidates
                WHERE excluded_kind IS NULL {'' if all_tiers else 'AND COALESCE(is_figure, 0) = 0'}
            )
            SELECT asin, title, priority_tier, data_json FROM ranked
            WHERE rn = 1 {'' if all_tiers else f'AND priority_tier IN ({placeholders})'}
              AND (listing_checked_at IS NULL OR listing_checked_at < ?)
            ORDER BY CASE priority_tier WHEN 'S' THEN 0 WHEN 'A+' THEN 1 WHEN 'A-' THEN 2 WHEN 'B+' THEN 3 WHEN 'B-' THEN 4 WHEN 'C+' THEN 5 WHEN 'C-' THEN 6 WHEN 'D' THEN 7 ELSE 8 END, created_at DESC
            ''',
            (cutoff,) if all_tiers else (*LISTING_CHECK_TIERS, cutoff),
        ).fetchall()
    asins = []
    for asin, title, _tier, data_json in rows:
        try:
            brand = (json.loads(data_json) if data_json else {}).get('brand')
        except Exception:
            brand = None
        if not all_tiers and (_is_media(asin, title) or _gated_brand(title, brand)):
            continue
        asins.append(asin)
        if len(asins) >= limit:
            break
    return asins


def save_listing_status(asin: str, status: str) -> int:
    """そのASINの全行に、出品可否(listing_status)と照会日時を保存する。更新した行数を返す。"""
    init_ops_tables()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            'UPDATE agent_candidates SET listing_status = ?, listing_checked_at = ? WHERE asin = ?',
            (status, now, asin),
        )
        return cursor.rowcount


# ---------------------------------------------------------------------------
# NETSEAの卸価格の付与(カタログ同期 -> 候補のJANで突き合わせ)
# ---------------------------------------------------------------------------

WHOLESALE_CHECK_TIERS = ('S', 'A+', 'A-', 'B+')


def netsea_rows_from_items(items: list, fetched_at: str) -> list:
    """NETSEA /items の商品(dict)の一覧を、netsea_catalog の行(タプル)にする。
    JANコードの無いセットは対象外。JANは商品直下より、セット(バリエーション)側を優先する。
    1個あたりの卸価格は set_price_without_tax / set_num(なければ price)。"""
    rows = []
    for item in items:
        for variant in item.get('set') or []:
            jan = str(variant.get('jan_code') or item.get('jan_code') or '').strip()
            if not jan:
                continue
            set_num = variant.get('set_num') or 1
            set_price = variant.get('set_price_without_tax')
            if set_price is None:
                set_price = (variant.get('price') or 0) * set_num if variant.get('price') is not None else None
            if set_price is None:
                continue
            rows.append((
                str(item.get('supplier_id')), str(item.get('product_id')), str(variant.get('direct_item_id')),
                jan, item.get('shop_name'), item.get('product_name'), item.get('product_url'),
                int(set_num), float(set_price) / int(set_num), float(set_price),
                1 if variant.get('sold_out_flag') == 'Y' else 0,
                item.get('image_copy_flag'), item.get('direct_send_flag'), fetched_at,
            ))
    return rows


def replace_netsea_catalog(supplier_ids: list, rows: list) -> int:
    """指定サプライヤーの商品を、rowsで置き換える(1トランザクション)。挿入した行数を返す。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.executemany('DELETE FROM netsea_catalog WHERE supplier_id = ?', [(str(sid),) for sid in supplier_ids])
        conn.executemany(
            '''
            INSERT OR REPLACE INTO netsea_catalog
                (supplier_id, product_id, direct_item_id, jan_code, shop_name, product_name, product_url,
                 set_num, unit_price_jpy, set_price_jpy, sold_out, image_copy_flag, direct_send_flag, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            rows,
        )
    return len(rows)


def upsert_netsea_rows(rows: list) -> int:
    """netsea_catalog の行を追加・更新する(削除はしない)。ページ単位の書き込み用。

    同期中はスキャンループ(daily_scan.py)が同じDBに頻繁に書き込んでおり、SQLiteは同時書き込みを
    1つしか許さないため、既定の5秒タイムアウトだと「database is locked」で頻繁に失敗する
    (2026-09-28、CEOに再同期を頼まれた直後に実際に発生・原因調査済み)。timeout=30で緩和する。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.executemany(
            '''
            INSERT OR REPLACE INTO netsea_catalog
                (supplier_id, product_id, direct_item_id, jan_code, shop_name, product_name, product_url,
                 set_num, unit_price_jpy, set_price_jpy, sold_out, image_copy_flag, direct_send_flag, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            rows,
        )
    return len(rows)


def delete_stale_netsea_rows(supplier_ids: list, before_iso: str) -> int:
    """指定サプライヤーの行のうち、before_iso より前に取得したもの(今回の同期で見つからなかった
    =出品終了した商品)を削除する。削除した行数を返す。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        deleted = 0
        for sid in supplier_ids:
            deleted += conn.execute(
                'DELETE FROM netsea_catalog WHERE supplier_id = ? AND fetched_at < ?', (str(sid), before_iso)
            ).rowcount
    return deleted


def _normalize_jan(value) -> str | None:
    """13桁の数字ならJANとして返す(それ以外はNone)。"""
    digits = re.sub(r'\D', '', str(value or ''))
    return digits if len(digits) == 13 else None


def find_netsea_match(jans: list) -> dict | None:
    """JANのどれかに一致する在庫ありのNETSEA商品のうち、1個あたりの卸価格が最安のものを返す。"""
    codes = [j for j in (_normalize_jan(x) for x in jans) if j]
    if not codes:
        return None
    init_ops_tables()
    placeholders = ', '.join('?' for _ in codes)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f'''
            SELECT * FROM netsea_catalog
            WHERE jan_code IN ({placeholders}) AND sold_out = 0
            ORDER BY unit_price_jpy ASC LIMIT 1
            ''',
            codes,
        ).fetchone()
    return dict(row) if row else None


def load_candidates_needing_wholesale(limit: int = 20, max_age_days: int = 7) -> list:
    """NETSEAの卸価格を照会すべき候補(ASINごとの最新行)を返す: 優先度Tier S/A+/A-/B+ で出品可
    (listing_status='ok')、完全除外・フィギュア・NETSEA由来ではなく、未照会または
    max_age_days日以上前のもの。Tierの高い順。戻り値: [{'id', 'asin', 'jans'}]"""
    init_ops_tables()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    placeholders = ', '.join('?' for _ in WHOLESALE_CHECK_TIERS)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f'''
            WITH ranked AS (
                SELECT id, asin, title, priority_tier, data_json, created_at,
                    ROW_NUMBER() OVER (PARTITION BY asin ORDER BY created_at DESC, id DESC) AS rn
                FROM agent_candidates
                WHERE excluded_kind IS NULL AND COALESCE(is_figure, 0) = 0 AND listing_status = 'ok'
            )
            SELECT id, asin, title, data_json FROM ranked
            WHERE rn = 1 AND priority_tier IN ({placeholders})
              AND json_extract(data_json, '$.netsea_jan') IS NULL
              AND (json_extract(data_json, '$.wholesale_checked_at') IS NULL
                   OR json_extract(data_json, '$.wholesale_checked_at') < ?)
            ORDER BY CASE priority_tier WHEN 'S' THEN 0 WHEN 'A+' THEN 1 WHEN 'A-' THEN 2 WHEN 'B+' THEN 3 WHEN 'B-' THEN 4 WHEN 'C+' THEN 5 WHEN 'C-' THEN 6 WHEN 'D' THEN 7 ELSE 8 END, created_at DESC
            ''',
            (*WHOLESALE_CHECK_TIERS, cutoff),
        ).fetchall()
        candidates = []
        for row_id, asin, title, data_json in rows:
            try:
                data = json.loads(data_json) if data_json else {}
            except Exception:
                data = {}
            if _is_media(asin, title) or _gated_brand(title, data.get('brand')):
                continue
            jans = [data.get('ean')]
            mapped = conn.execute('SELECT jan_code FROM asin_jan_map WHERE asin = ?', (asin,)).fetchone()
            if mapped:
                jans.append(mapped[0])
            jans = list(dict.fromkeys(j for j in (_normalize_jan(x) for x in jans) if j))
            if not jans:
                continue   # JANが無い候補は突き合わせられない
            candidates.append({'id': row_id, 'asin': asin, 'jans': jans})
            if len(candidates) >= limit:
                break
    return candidates


def _recompute_cost_and_tier(
    data: dict, title: str | None, monthly_sold, sales_rank, tier: str | None, excluded_kind: str | None,
    old_cost_jpy: float | None, new_cost_jpy: float | None, new_source: str | None = None,
) -> tuple[dict, dict]:
    """新しい原価候補(new_cost_jpy、税基準は揃え済み)が、今の原価(old_cost_jpy)より
    安ければ、利益・ROI・優先度Tierを再計算する(手数料・為替・重量は、dataに保存済みの
    値をそのまま使う)。安くなければ、dataは変更せず、DB更新の必要な列もない。

    apply_wholesale_result(NETSEA自動連携)とadd_manual_supplier(手動追加)の、
    どちらから呼ばれても同じ判定・同じ計算になるよう共通化したもの(抽出前と挙動は
    変えていない - test_netsea_wholesale.pyがそのまま通ることで担保)。

    戻り値: (更新後のdata, {'recalculated': bool, 'roi_before', 'roi_after', 'tier_before',
    'tier_after', 'db_updates': {jp_cost_jpy/unit_profit_usd/margin_pct/priority_tier/
    qualified/reason}})。再計算しなかった場合、'db_updates'は空dict。

    2026-09-28: qualified/reasonもここで再計算してdb_updatesに含める(旧・非同期バグの
    修正 - 以前はpriority_tierだけ更新され、qualifiedは仕入れ先確定前の古い値のまま
    だった)。

    2026-09-28: 同額タイブレーク(CEO:「netseaとsdが同じ値段ならsdを優先して」) -
    new_source='sd'かつ現在の原価の出所(data['jp_cost_source'])がsdでない場合、
    金額が同じ(0.5円以内)でも情報源をSDに差し替える(採用原価の数値は変わらないが、
    どの仕入れ先を正としてdata_jsonに記録するかが変わる)。"""
    result = {'recalculated': False}
    usable = all(data.get(k) for k in ('us_price_usd', 'jp_cost_usd')) and data.get('fba_fee_usd') is not None \
        and data.get('amazon_fee_usd') is not None and data.get('weight_kg') is not None and old_cost_jpy
    is_cheaper = new_cost_jpy is not None and new_cost_jpy < old_cost_jpy - 0.5 if old_cost_jpy else False
    is_tie_sd_preferred = (
        new_source == 'sd' and data.get('jp_cost_source') != 'sd'
        and new_cost_jpy is not None and old_cost_jpy is not None and abs(new_cost_jpy - old_cost_jpy) <= 0.5
    )
    if not (usable and new_cost_jpy is not None and (is_cheaper or is_tie_sd_preferred)):
        return data, result

    exchange_rate = old_cost_jpy / data['jp_cost_usd']
    profit = calc_unit_profit(
        us_price_usd=data['us_price_usd'], jp_cost_jpy=new_cost_jpy, weight_kg=data['weight_kg'],
        exchange_rate=exchange_rate,
        amazon_fee_rate=data['amazon_fee_usd'] / data['us_price_usd'],
        fba_fee_usd=data['fba_fee_usd'],
    )
    result.update({'recalculated': True, 'roi_before': data.get('roi_pct'), 'tier_before': tier})
    data = dict(data)
    data['jp_cost_jpy_before_wholesale'] = old_cost_jpy
    data.update(profit)
    data['jp_cost_jpy'] = new_cost_jpy
    if new_source:
        data['jp_cost_source'] = new_source
    entry = {**data, 'title': title, 'monthly_sold': monthly_sold, 'sales_rank': sales_rank}
    _apply_priority_fields(entry)
    new_tier = None if excluded_kind else entry['priority_tier']
    entry['priority_tier'] = new_tier
    new_qualified = 0 if excluded_kind else int(is_qualified_priority_tier(new_tier))
    new_reason = None if new_qualified else _priority_rejection_reason(entry)
    db_updates = {
        'jp_cost_jpy': new_cost_jpy, 'unit_profit_usd': profit['unit_profit_usd'],
        'margin_pct': profit['margin_pct'], 'priority_tier': new_tier,
        'qualified': new_qualified, 'reason': new_reason,
    }
    result.update({
        'roi_after': profit['roi_pct'], 'tier_after': new_tier,
        'qualified_before': None, 'qualified_after': new_qualified,
        'db_updates': db_updates,
    })
    return data, result


def apply_wholesale_result(row_id: int, match: dict | None) -> dict:
    """候補の行(agent_candidates.id)に、NETSEAの照会結果を保存する。matchがNone(該当なし)でも
    照会日時(wholesale_checked_at)は保存する。卸価格の方が今の原価より安ければ、原価・利益・
    ROI・優先度Tierを再計算する(手数料・為替・重量は、保存済みの値をそのまま使う)。
    戻り値: {'matched': bool, 'recalculated': bool, 'roi_before': ..., 'roi_after': ..., 'tier_before': ..., 'tier_after': ...}"""
    init_ops_tables()
    now = datetime.now(timezone.utc).isoformat()
    result = {'matched': match is not None, 'recalculated': False}
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        row = conn.execute(
            'SELECT asin, title, monthly_sold, sales_rank, jp_cost_jpy, unit_profit_usd, margin_pct, '
            'priority_tier, excluded_kind, qualified, data_json FROM agent_candidates WHERE id = ?',
            (row_id,),
        ).fetchone()
        if row is None:
            return result
        (asin, title, monthly_sold, sales_rank, jp_cost_jpy, unit_profit_usd, margin_pct, tier,
         excluded_kind, qualified_before, data_json) = row
        data = json.loads(data_json) if data_json else {}
        data['wholesale_checked_at'] = now
        db_updates = {}
        if match is not None:
            wholesale = float(match['unit_price_jpy'])
            data.update({
                'wholesale_cost_jpy': wholesale,
                'netsea_shop_name': match.get('shop_name'),
                'netsea_product_url': match.get('product_url'),
                'netsea_set_num': match.get('set_num'),
                'netsea_matched_jan': match.get('jan_code'),
            })
            new_cost = normalize_jp_cost_for_tax(wholesale, None)
            old_cost = jp_cost_jpy if jp_cost_jpy is not None else data.get('jp_cost_jpy')
            data, recompute_result = _recompute_cost_and_tier(
                data, title, monthly_sold, sales_rank, tier, excluded_kind, old_cost, new_cost,
                new_source='netsea',
            )
            db_updates = recompute_result.pop('db_updates', {})
            recompute_result['qualified_before'] = qualified_before
            result.update(recompute_result)
        assignments = ', '.join(f'{k} = ?' for k in ('data_json', *db_updates))
        conn.execute(
            f'UPDATE agent_candidates SET {assignments} WHERE id = ?',
            (json.dumps(data, ensure_ascii=False), *db_updates.values(), row_id),
        )
    return result


def add_manual_supplier(
    asin: str, source: str, shop_name: str, price_jpy: float,
    url: str | None = None, min_qty: int | None = None, note: str | None = None,
) -> dict:
    """手動(Claudeがブラウザ等で調べた結果)で見つけた仕入れ先を、指定ASINの全行の
    data_json['manual_suppliers']に追加する(NETSEA自動連携=wholesale_cost_jpy等の
    既存フィールドとは別のリストで、上書きしない)。同じsource+shop_nameの既存エントリが
    あれば置き換える(重複させない)。

    price_jpy(税抜想定)は、NETSEAと同じくnormalize_jp_cost_for_taxで税基準を揃えたうえで、
    現在のjp_cost_jpy(=「今分かっている中で一番安い原価」を常に表す列)より安ければ、
    _recompute_cost_and_tier()で利益・ROI・優先度Tierを再計算する(CEO: 「手動で仕入れ先を
    追加したとき、利益・ROIも自動で再計算する」2026-09-27)。この基準列を介するため、
    NETSEA自動連携が先でも後でも、常に両方のうち安い方が採用される。

    戻り値: apply_wholesale_result()と同じ形({'matched': True, 'recalculated', 'roi_before',
    'roi_after', 'tier_before', 'tier_after'})に加えて'rows_updated'(対象になった行数)。
    最新行(created_at最大)の再計算結果を代表として返す。ASINが1件も無ければ
    {'matched': False, 'recalculated': False, 'rows_updated': 0}。
    """
    init_ops_tables()
    now = datetime.now(timezone.utc).isoformat()
    supplier_entry = {
        'source': source, 'shop_name': shop_name, 'url': url,
        'price_jpy': float(price_jpy), 'min_qty': min_qty, 'note': note, 'checked_at': now,
    }
    new_cost = normalize_jp_cost_for_tax(float(price_jpy), None)
    result = {'matched': False, 'recalculated': False, 'rows_updated': 0}
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute(
            'SELECT id, title, monthly_sold, sales_rank, jp_cost_jpy, priority_tier, excluded_kind, '
            'qualified, data_json, created_at '
            'FROM agent_candidates WHERE asin = ? ORDER BY created_at DESC',
            (asin,),
        ).fetchall()
        if not rows:
            return result
        result['matched'] = True
        for index, (row_id, title, monthly_sold, sales_rank, jp_cost_jpy, tier, excluded_kind,
                    qualified_before, data_json, _created_at) in enumerate(rows):
            data = json.loads(data_json) if data_json else {}
            suppliers = [
                s for s in (data.get('manual_suppliers') or [])
                if not (s.get('source') == source and s.get('shop_name') == shop_name)
            ]
            suppliers.append(supplier_entry)
            data['manual_suppliers'] = suppliers
            old_cost = jp_cost_jpy if jp_cost_jpy is not None else data.get('jp_cost_jpy')
            data, recompute_result = _recompute_cost_and_tier(
                data, title, monthly_sold, sales_rank, tier, excluded_kind, old_cost, new_cost,
                new_source=source,
            )
            db_updates = recompute_result.pop('db_updates', {})
            recompute_result['qualified_before'] = qualified_before
            if index == 0:   # 最新行の結果を代表として返す
                result.update(recompute_result)
            assignments = ', '.join(f'{k} = ?' for k in ('data_json', *db_updates))
            conn.execute(
                f'UPDATE agent_candidates SET {assignments} WHERE id = ?',
                (json.dumps(data, ensure_ascii=False), *db_updates.values(), row_id),
            )
            result['rows_updated'] += 1
    return result


DIGEST_PRIORITY_TIERS = ('S', 'A+', 'A-', 'B+')   # LINEの朝/夜の通知に載せる優先度Tier(2026-09-28 Tier再設計)
_DIGEST_TIER_ORDER = {tier: index for index, tier in enumerate(DIGEST_PRIORITY_TIERS)}


def load_digest_window(since_iso: str | None):
    """前回ダイジェスト送信以降(初回はsince_iso=None、直近24時間扱い)の
    データをまとめて返す: 優先度Tier S/A+/A-/B+ の候補(ASIN重複除去・複数回見つかった
    場合は最新のものを採用)と、その間に検索したキーワード一覧。

    完全除外(食品・医薬品/化粧品・刃物)・フィギュア・メディア(本/DVD/CD)・
    出品制限ブランド(HARIO・タカラトミー等)・SP-APIで商品単位の承認(Transparency等)が
    必要、または出品不可と確認できたものは載せない。**ブランドの承認のみ
    (approval_required)は除外しない**(CEO: 「ブランド申請は出品にはほぼ全てあるので、
    隠す設定は不要」2026-09-27) - ほぼ全商品に該当するため、通知には載せたうえで
    「要承認(ブランド)」と表示する(build_daily_digest_message参照)。出品可否が
    未確認のものも載せて、通知に「未確認」と出す。並びは Tier 順(S→A→B+)、
    同じTierではROIの高い順。
    """
    init_ops_tables()
    if since_iso is None:
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    placeholders = ', '.join('?' for _ in DIGEST_PRIORITY_TIERS)
    with sqlite3.connect(DB_PATH) as conn:
        candidate_rows = conn.execute(
            f'''
            WITH ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (PARTITION BY asin ORDER BY created_at DESC) AS rn
                FROM agent_candidates
                WHERE created_at > ?
                  AND excluded_kind IS NULL AND COALESCE(is_figure, 0) = 0
            )
            SELECT asin, title, us_url, jp_url, us_price_usd, jp_cost_jpy,
                   sales_rank, review_count, margin_pct, unit_profit_usd,
                   weight_estimated, category, created_at, tier,
                   priority_tier, monthly_sold, data_json, listing_status
            FROM ranked
            WHERE rn = 1 AND priority_tier IN ({placeholders})
              AND COALESCE(listing_status, 'unknown') NOT IN ('product_approval_required', 'brand_and_product_approval_required', 'not_eligible', 'restricted')
            ''',
            (since_iso, *DIGEST_PRIORITY_TIERS),
        ).fetchall()

        keyword_rows = conn.execute(
            'SELECT DISTINCT keyword FROM agent_runs WHERE started_at > ? AND keyword IS NOT NULL ORDER BY keyword',
            (since_iso,),
        ).fetchall()

    candidates = []
    for (asin, title, us_url, jp_url, us_price_usd, jp_cost_jpy, sales_rank, review_count,
         margin_pct, unit_profit_usd, weight_estimated, category, created_at, tier,
         priority_tier, monthly_sold, data_json, listing_status) in candidate_rows:
        try:
            data = json.loads(data_json) if data_json else {}
        except Exception:
            data = {}
        if _is_media(asin, title) or _gated_brand(title, data.get('brand')):
            continue
        candidates.append({
            'asin': asin, 'title': title, 'us_url': us_url, 'jp_url': jp_url,
            'us_price_usd': us_price_usd, 'jp_cost_jpy': jp_cost_jpy,
            'sales_rank': sales_rank, 'review_count': review_count,
            'margin_pct': margin_pct, 'unit_profit_usd': unit_profit_usd,
            'weight_estimated': bool(weight_estimated), 'category': category,
            'created_at': created_at, 'tier': tier,
            'priority_tier': priority_tier, 'monthly_sold': monthly_sold,
            'listing_status': listing_status,
            'roi_pct': data.get('roi_pct'),
            'competitor_seller_count': data.get('competitor_seller_count'),
        })
    candidates.sort(key=lambda c: (_DIGEST_TIER_ORDER.get(c['priority_tier'], 99), -(c['roi_pct'] if c['roi_pct'] is not None else -9)))
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
        lines.append("優先度Tier S/A+/A-/B+ の候補はありませんでした。")
    else:
        tier_counts = ' / '.join(
            f"{tier}: {sum(1 for c in candidates if c.get('priority_tier') == tier)}件"
            for tier in DIGEST_PRIORITY_TIERS
        )
        lines.append(f"優先度Tier {tier_counts}(重複除く。本・DVD/CD・フィギュア・出品制限ブランドは除外)")
        for item in candidates[:max_items]:
            weight_note = '(重量は仮値)' if item['weight_estimated'] else ''
            tier_label = f"【Tier {item.get('priority_tier') or '-'}】"
            lines.append('---')
            lines.append(f"{tier_label} ASIN: {item['asin']}")
            lines.append(f"{item['title'] or ''}")
            profit_jpy = round((item['unit_profit_usd'] or 0) * exchange_rate)
            margin = item['margin_pct']
            roi = item.get('roi_pct')
            roi_str = f" / ROI: {roi:.0%}" if roi is not None else ''
            lines.append(
                f"利益率: {margin:.1%}{roi_str} / 1個あたり利益: ¥{profit_jpy:,}" if margin is not None else "利益率: -"
            )
            sold = item.get('monthly_sold')
            comp = item.get('competitor_seller_count')
            listing_status = item.get('listing_status')
            # CEO: 「ブランド申請は出品にはほぼ全てあるので、隠す設定は不要」(2026-09-27) -
            # approval_required(ブランドの承認のみ)は、この時点でwhere句から除外していない
            # (下のload_digest_window()参照)ため、「未確認」と紛れないよう専用の表示にする。
            if listing_status == 'ok':
                listing_str = '可'
            elif listing_status == 'approval_required':
                listing_str = '要承認(ブランド)'
            else:
                listing_str = '未確認'
            lines.append(
                f"先月の購入: {sold if sold is not None else '-'} / 競合(出品者): {comp if comp is not None else '-'}"
                f" / 出品: {listing_str}"
            )
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
                item.get('reason'),
                json.dumps(item, ensure_ascii=False),
                created_at,
                source_type,
                seller_id,
                seller_name,
                seed_asin,
                item.get('priority_tier'),
                item.get('excluded_kind'),
                1 if item.get('is_figure') else 0,
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
                    qualified, reason, data_json, created_at,
                    source_type, seller_id, seller_name, seed_asin,
                    priority_tier, excluded_kind, is_figure
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    # フィギュア系のメーカー/ブランド/シリーズ名(「figure」を名乗らないため上の語で
    # 取りこぼしていた。本番のキーワードプールに残っていた: Sofubi figure以外の
    # Kaiyodo Revoltech / Tamashii Nations / S.H.Figuarts / Ichiban Kuji /
    # Good Smile Company / Banpresto)。
    'sofubi', 'revoltech', 'kaiyodo', 'tamashii nations', 'figuarts', 'banpresto',
    'good smile', 'ichiban kuji', 'kuji', 'sonny angel', 'pokemon japanese card',
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
    # 食品・医薬品/化粧品・包丁類は、輸出に課題があるため完全除外(CEO指示
    # 2026-09-26)。上の_is_food_or_unsuitable_keywordと重なる分は同じ結果になる。
    if _excluded_kind(stripped):
        return False
    # 出品制限ブランド(HARIO・タカラトミーなど)は、検索しても出品できないので外す。
    if _gated_brand(stripped):
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
        rows = conn.execute(
            '''
            SELECT keyword, price_min FROM keyword_pool
            WHERE status = 'active'
            ORDER BY times_used ASC, COALESCE(last_used_at, '') ASC
            '''
        ).fetchall()
    # キーワードの除外判定(_is_searchable_keyword)は追加時にしか効かないため、
    # 判定が厳しくなる前にプールへ入ったキーワード(フィギュア・食品・医薬品・
    # 包丁類など)は、ここでも弾く(プールの行自体は変更しない)。
    for keyword, price_min in rows:
        if _is_searchable_keyword(keyword):
            return (keyword, price_min)
    return (None, None)


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
            'SELECT orders_synced_at, finances_synced_at, inventory_synced_at, inbound_synced_at FROM sp_sync_state WHERE id = 1'
        ).fetchone()
    if not row:
        return {'ordersSyncedAt': None, 'financesSyncedAt': None, 'inventorySyncedAt': None, 'inboundSyncedAt': None}
    return {'ordersSyncedAt': row[0], 'financesSyncedAt': row[1], 'inventorySyncedAt': row[2], 'inboundSyncedAt': row[3]}


def set_sp_sync_state(*, orders_synced_at=None, finances_synced_at=None, inventory_synced_at=None, inbound_synced_at=None) -> None:
    """渡されたフィールドだけ更新する(未指定のフィールドは既存値を保持)。"""
    init_ops_tables()
    current = get_sp_sync_state()
    orders_synced_at = orders_synced_at if orders_synced_at is not None else current['ordersSyncedAt']
    finances_synced_at = finances_synced_at if finances_synced_at is not None else current['financesSyncedAt']
    inventory_synced_at = inventory_synced_at if inventory_synced_at is not None else current['inventorySyncedAt']
    inbound_synced_at = inbound_synced_at if inbound_synced_at is not None else current['inboundSyncedAt']
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO sp_sync_state (id, orders_synced_at, finances_synced_at, inventory_synced_at, inbound_synced_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                orders_synced_at = excluded.orders_synced_at,
                finances_synced_at = excluded.finances_synced_at,
                inventory_synced_at = excluded.inventory_synced_at,
                inbound_synced_at = excluded.inbound_synced_at
            ''',
            (orders_synced_at, finances_synced_at, inventory_synced_at, inbound_synced_at),
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


def upsert_sp_order_items(order_id: str, items: list) -> int:
    """items: [{asin, sku, quantity, itemPriceUsd}, ...]。既存の同order_id分は洗い替え
    (getOrderItemsを再取得するたびに最新の明細で置き換える)。"""
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('DELETE FROM sp_order_items WHERE order_id = ?', (order_id,))
        conn.executemany(
            '''
            INSERT INTO sp_order_items (order_id, asin, sku, quantity, item_price_usd)
            VALUES (:orderId, :asin, :sku, :quantity, :itemPriceUsd)
            ''',
            [{**i, 'orderId': order_id} for i in items],
        )
    return len(items)


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
            INSERT OR REPLACE INTO sp_fba_inventory (asin, sku, fnsku, fulfillable_quantity, snapshot_at)
            VALUES (:asin, :sku, :fnsku, :fulfillableQuantity, :snapshotAt)
            ''',
            [{**i, 'snapshotAt': now} for i in items],
        )
    return len(items)


def upsert_sp_inbound_shipment(shipment: dict, items: list) -> None:
    """shipment: {shipmentId, planId, shipmentConfirmationId, status, destinationFc,
    deliveryWindowStart, deliveryWindowEnd, createdAt}。
    items: [{asin, sku, quantity}, ...] (そのプラン全体のitems一覧。1プラン=1便運用の
    前提での簡略実装、複数便分割時は同一items一覧が全便に入る - Phase4のNote参照)。"""
    init_ops_tables()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO sp_inbound_shipments (
                shipment_id, plan_id, shipment_confirmation_id, status, destination_fc,
                delivery_window_start, delivery_window_end, created_at, synced_at
            )
            VALUES (:shipmentId, :planId, :shipmentConfirmationId, :status, :destinationFc,
                    :deliveryWindowStart, :deliveryWindowEnd, :createdAt, :syncedAt)
            ON CONFLICT(shipment_id) DO UPDATE SET
                plan_id = excluded.plan_id,
                shipment_confirmation_id = excluded.shipment_confirmation_id,
                status = excluded.status,
                destination_fc = excluded.destination_fc,
                delivery_window_start = excluded.delivery_window_start,
                delivery_window_end = excluded.delivery_window_end,
                created_at = excluded.created_at,
                synced_at = excluded.synced_at
            ''',
            {**shipment, 'syncedAt': now},
        )
        conn.execute('DELETE FROM sp_inbound_shipment_items WHERE shipment_id = ?', (shipment['shipmentId'],))
        conn.executemany(
            '''
            INSERT OR REPLACE INTO sp_inbound_shipment_items (shipment_id, asin, sku, quantity)
            VALUES (:shipmentId, :asin, :sku, :quantity)
            ''',
            [{**i, 'shipmentId': shipment['shipmentId']} for i in items],
        )


def list_sp_inbound_shipments() -> list:
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            '''
            SELECT shipment_id, plan_id, shipment_confirmation_id, status, destination_fc,
                   delivery_window_start, delivery_window_end, created_at
            FROM sp_inbound_shipments ORDER BY created_at DESC
            '''
        ).fetchall()
        items_by_shipment: dict = {}
        for row in conn.execute('SELECT shipment_id, asin, sku, quantity FROM sp_inbound_shipment_items'):
            items_by_shipment.setdefault(row['shipment_id'], []).append(
                {'asin': row['asin'], 'sku': row['sku'], 'quantity': row['quantity']}
            )
    return [
        {
            'shipmentId': r['shipment_id'],
            'planId': r['plan_id'],
            'shipmentConfirmationId': r['shipment_confirmation_id'],
            'status': r['status'],
            'destinationFc': r['destination_fc'],
            'deliveryWindowStart': r['delivery_window_start'],
            'deliveryWindowEnd': r['delivery_window_end'],
            'createdAt': r['created_at'],
            'items': items_by_shipment.get(r['shipment_id'], []),
        }
        for r in rows
    ]


def get_shipment_pnl(usd_to_jpy: float = 150.0) -> list:
    """FBA納品便(sp_inbound_shipments)ごとのP&L概算。

    原価: 便の各ASINの数量 × jp_purchase_recordsのその便に最も近い日付の仕入単価
    (+送料按分、既存のcost_by_asinロジックと同じ)。

    売上: 正確なロット追跡はできない(Amazonはどの納品便由来の在庫が売れたかを
    教えない)ため、ASINごとに納品便を納品期間開始日の古い順に並べ、
    sp_order_itemsの売上(注文日が納品期間開始日以降のもの)を数量ベースの
    FIFO近似で先頭の便から順に割り当てる、という概算を行う。UI側で
    「概算」である旨を明記すること。"""
    init_ops_tables()
    shipments = list_sp_inbound_shipments()
    if not shipments:
        return []

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cost_by_asin_rows = conn.execute(
            '''
            SELECT asin, order_date, unit_price_jpy, quantity, COALESCE(shipping_cost_jpy, 0) AS shipping_cost_jpy
            FROM jp_purchase_records WHERE asin IS NOT NULL ORDER BY order_date ASC
            '''
        ).fetchall()
        sold_items = conn.execute(
            '''
            SELECT oi.asin AS asin, oi.quantity AS quantity, oi.item_price_usd AS item_price_usd,
                   o.purchase_date AS purchase_date
            FROM sp_order_items oi
            JOIN sp_orders o ON o.order_id = oi.order_id
            WHERE oi.asin IS NOT NULL
            ORDER BY o.purchase_date ASC
            '''
        ).fetchall()

    # ASINごとの仕入単価履歴(着地原価)。便の納品期間開始日に最も近い(かつそれ以前の)
    # 仕入れ記録を使う。無ければ直近(最後)の記録にフォールバック。
    purchases_by_asin: dict = {}
    for row in cost_by_asin_rows:
        shipping_per_unit = (row['shipping_cost_jpy'] / row['quantity']) if row['quantity'] else 0
        landed = (row['unit_price_jpy'] or 0) + shipping_per_unit
        purchases_by_asin.setdefault(row['asin'], []).append((row['order_date'] or '', landed))

    def landed_cost_for(asin: str, as_of_date: str | None) -> float:
        history = purchases_by_asin.get(asin)
        if not history:
            return 0.0
        if not as_of_date:
            return history[-1][1]
        candidates = [cost for date, cost in history if date <= as_of_date]
        return candidates[-1] if candidates else history[0][1]

    # ASINごとの未消化販売キュー(FIFO)。(quantity, unit_revenue_usd)
    sales_queue: dict = {}
    for row in sold_items:
        sales_queue.setdefault(row['asin'], []).append(
            {'qty': row['quantity'] or 0, 'unitRevenue': row['item_price_usd'] or 0}
        )

    # 便をASINごとに納品期間開始日の古い順で処理するため、まずASIN×便の一覧を作る
    shipments_sorted = sorted(shipments, key=lambda s: s['deliveryWindowStart'] or s['createdAt'] or '')

    result = []
    for shipment in shipments_sorted:
        as_of = shipment['deliveryWindowStart'] or shipment['createdAt']
        total_cost_usd = 0.0
        total_revenue_usd = 0.0
        total_units_shipped = 0
        total_units_sold = 0
        for item in shipment['items']:
            asin = item['asin']
            qty_shipped = item['quantity'] or 0
            total_units_shipped += qty_shipped
            total_cost_usd += (landed_cost_for(asin, as_of) / usd_to_jpy) * qty_shipped

            remaining = qty_shipped
            queue = sales_queue.get(asin, [])
            while remaining > 0 and queue:
                sale = queue[0]
                take = min(remaining, sale['qty'])
                total_revenue_usd += take * sale['unitRevenue']
                total_units_sold += take
                sale['qty'] -= take
                remaining -= take
                if sale['qty'] <= 0:
                    queue.pop(0)

        net_profit_usd = total_revenue_usd - total_cost_usd
        result.append({
            'shipmentId': shipment['shipmentId'],
            'shipmentConfirmationId': shipment['shipmentConfirmationId'],
            'status': shipment['status'],
            'destinationFc': shipment['destinationFc'],
            'deliveryWindowStart': shipment['deliveryWindowStart'],
            'deliveryWindowEnd': shipment['deliveryWindowEnd'],
            'unitsShipped': total_units_shipped,
            'unitsSold': total_units_sold,
            'costUsd': round(total_cost_usd, 2),
            'revenueUsd': round(total_revenue_usd, 2),
            'netProfitUsd': round(net_profit_usd, 2),
        })
    return result


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


def allocate_order_shipping(supplier_name: str, order_date: str, total_shipping_jpy: float) -> list:
    """1回の発注(同じsupplier_name + order_date)にかかった送料(国内送料・
    分かっていれば国際送料も合算した額)を、その発注内の各商品行に数量按分で
    書き込む(CEO: 「送料は商品ごとに配分して」)。按分は数量ベース
    (同発注内の商品は小型文具で単価も近く、重量按分より単純な数量按分で
    実用上十分と判断)。

    複数回呼ぶと直近の呼び出しで上書きされる(例: 国内送料だけで一度配分した後、
    TNKの国際送料が確定したら合計額で再度呼べば良い)。

    Returns: 更新した jp_purchase_records の行(id・quantity・配分後shipping_cost_jpy)のリスト。
    """
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT id, quantity FROM jp_purchase_records
            WHERE supplier_name = ? AND order_date = ?
            ''',
            (supplier_name, order_date),
        ).fetchall()
        total_quantity = sum(qty or 0 for _, qty in rows)
        if total_quantity <= 0:
            return []
        per_unit_shipping = total_shipping_jpy / total_quantity
        updated = []
        for row_id, quantity in rows:
            allocated = round(per_unit_shipping * (quantity or 0), 2)
            conn.execute(
                'UPDATE jp_purchase_records SET shipping_cost_jpy = ? WHERE id = ?',
                (allocated, row_id),
            )
            updated.append({'id': row_id, 'quantity': quantity, 'shippingCostJpy': allocated})
    return updated


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
        order_count = conn.execute(
            'SELECT COUNT(*) AS c FROM sp_orders WHERE purchase_date >= ?', (since,),
        ).fetchone()['c']
        # 売上/COGSはsp_order_items(商品明細)ベース。getOrderItemsをまだ同期していない
        # 注文(order_itemsが無い)は、商品ごとの内訳が無いだけで合計からは除外される
        # (2026-09-30以前の挙動もitem_price_usd/quantityが常にNULLだったため実質0円計上
        # だった。同期が進めば自然に解消する)。
        orders = conn.execute(
            '''
            SELECT oi.asin AS asin, oi.quantity AS quantity, oi.item_price_usd AS item_price_usd
            FROM sp_order_items oi
            JOIN sp_orders o ON o.order_id = oi.order_id
            WHERE o.purchase_date >= ?
            ''',
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
        # 着地原価(landed cost) = 商品単価 + その行に配分された送料/数量
        # (CEO: 「送料は商品ごとに配分して」— allocate_order_shipping()が
        # shipping_cost_jpyに書き込む。未配分(NULL)ならCOALESCEで0扱い)。
        cost_by_asin = {}
        for row in conn.execute(
            '''
            SELECT asin, unit_price_jpy, quantity, COALESCE(shipping_cost_jpy, 0) AS shipping_cost_jpy
            FROM jp_purchase_records
            WHERE asin IS NOT NULL
            ORDER BY order_date ASC
            '''
        ):
            shipping_per_unit = (row['shipping_cost_jpy'] / row['quantity']) if row['quantity'] else 0
            cost_by_asin[row['asin']] = (row['unit_price_jpy'] or 0) + shipping_per_unit  # 後勝ちで直近単価が残る
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
        'orderCount': order_count,
        'revenueUsd': round(revenue_usd, 2),
        'cogsUsd': round(cogs_usd, 2),
        'feesUsd': round(fees_usd, 2),
        'fixedCostUsd': round(fixed_cost_period_usd, 2),
        'fixedCosts': fixed_costs_breakdown,
        'netProfitUsd': round(net_profit_usd, 2),
        'fixedCostCoveragePct': round(100.0 * (revenue_usd - fees_usd - cogs_usd) / fixed_cost_period_usd, 1)
        if fixed_cost_period_usd > 0 else None,
    }


def get_per_product_pnl(days: int = 30, usd_to_jpy: float = 150.0) -> list:
    """商品(ASIN)ごとのP&L。sp_order_items(数量・売上)、sp_financial_events(手数料、
    order_id経由でASINへ配賦)、jp_purchase_records(原価)をASIN単位に集計して返す。
    1注文に複数ASINが含まれる場合、その注文の手数料合計を数量按分でASINごとに配る
    (Finances APIの各手数料明細は商品単位だが、getOrderItemsとの突合は行っていない
    MVP実装のため、まずは按分で近似する)。"""
    init_ops_tables()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        items = conn.execute(
            '''
            SELECT oi.order_id AS order_id, oi.asin AS asin, oi.quantity AS quantity,
                   oi.item_price_usd AS item_price_usd
            FROM sp_order_items oi
            JOIN sp_orders o ON o.order_id = oi.order_id
            WHERE o.purchase_date >= ? AND oi.asin IS NOT NULL
            ''',
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
            SELECT asin, unit_price_jpy, quantity, COALESCE(shipping_cost_jpy, 0) AS shipping_cost_jpy
            FROM jp_purchase_records WHERE asin IS NOT NULL ORDER BY order_date ASC
            '''
        ):
            shipping_per_unit = (row['shipping_cost_jpy'] / row['quantity']) if row['quantity'] else 0
            cost_by_asin[row['asin']] = (row['unit_price_jpy'] or 0) + shipping_per_unit

    # 注文ごとの数量合計(手数料の按分に使う)
    qty_by_order: dict = {}
    for it in items:
        qty_by_order[it['order_id']] = qty_by_order.get(it['order_id'], 0) + (it['quantity'] or 0)

    by_asin: dict = {}
    for it in items:
        asin = it['asin']
        qty = it['quantity'] or 0
        order_total_qty = qty_by_order.get(it['order_id']) or 0
        order_fee = fees_by_order.get(it['order_id'], 0.0)
        allocated_fee = (order_fee * qty / order_total_qty) if order_total_qty else 0.0
        entry = by_asin.setdefault(asin, {'units': 0, 'revenueUsd': 0.0, 'feesUsd': 0.0})
        entry['units'] += qty
        entry['revenueUsd'] += (it['item_price_usd'] or 0) * qty
        entry['feesUsd'] += allocated_fee

    result = []
    for asin, entry in by_asin.items():
        cogs_jpy_per_unit = cost_by_asin.get(asin, 0) or 0
        cogs_usd = (cogs_jpy_per_unit / usd_to_jpy) * entry['units']
        net_profit_usd = entry['revenueUsd'] - entry['feesUsd'] - cogs_usd
        result.append({
            'asin': asin,
            'units': entry['units'],
            'revenueUsd': round(entry['revenueUsd'], 2),
            'feesUsd': round(entry['feesUsd'], 2),
            'cogsUsd': round(cogs_usd, 2),
            'netProfitUsd': round(net_profit_usd, 2),
            'marginPct': round(100.0 * net_profit_usd / entry['revenueUsd'], 1) if entry['revenueUsd'] else None,
        })
    result.sort(key=lambda r: r['revenueUsd'], reverse=True)
    return result


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
