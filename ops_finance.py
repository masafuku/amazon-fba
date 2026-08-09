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
                status TEXT NOT NULL DEFAULT 'active'  -- active/paused
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

def calc_unit_profit(
    us_price_usd: float,
    jp_cost_jpy: float,
    weight_kg: float,
    exchange_rate: float = 150.0,      # 円/ドル
    amazon_fee_rate: float = 0.15,     # Amazon販売手数料(カテゴリにより8〜15%)
    fba_fee_usd: float = 3.5,          # FBAピック&パック手数料(サイズ依存、要調整)
    intl_shipping_per_kg_usd: float = 8.0,  # 日本→FBA倉庫の国際配送費/kg
) -> dict:
    """1個あたりの利益・利益率を計算する。

    候補が出た瞬間にこの関数を通し、利益率が閾値未満なら自動除外できる。
    """
    jp_cost_usd = jp_cost_jpy / exchange_rate
    amazon_fee_usd = us_price_usd * amazon_fee_rate
    shipping_cost_usd = weight_kg * intl_shipping_per_kg_usd

    unit_profit_usd = (
        us_price_usd - amazon_fee_usd - fba_fee_usd - shipping_cost_usd - jp_cost_usd
    )
    margin_pct = unit_profit_usd / us_price_usd if us_price_usd else 0.0

    return {
        'us_price_usd': round(us_price_usd, 2),
        'jp_cost_usd': round(jp_cost_usd, 2),
        'amazon_fee_usd': round(amazon_fee_usd, 2),
        'fba_fee_usd': round(fba_fee_usd, 2),
        'shipping_cost_usd': round(shipping_cost_usd, 2),
        'unit_profit_usd': round(unit_profit_usd, 2),
        'margin_pct': round(margin_pct, 4),
    }


def break_even_units(fixed_cost_jpy: float, unit_profit_usd: float, exchange_rate: float = 150.0) -> float:
    """固定費(リスティング制作費など)を回収するのに必要な販売個数。"""
    if unit_profit_usd <= 0:
        return float('inf')
    fixed_cost_usd = fixed_cost_jpy / exchange_rate
    return fixed_cost_usd / unit_profit_usd


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
MIN_MARGIN_PCT = 0.20             # これ未満は自動除外

# 合格ラインの多段階化(CEO: 「合格ラインは何段階かに分けてください。たとえば、
# US−JPがゼロ以上、つまり手数料が0円なら成立する、というのもみたいです」)。
# qualified/rejectedという2リストのメンバーシップ自体は変えない(tier==passが
# qualified、それ以外はすべてrejected、旧来のmargin_pct>=min_margin_pct判定と
# 完全に等価) - 各エントリに付与するtierフィールドが新しい情報として増えるだけ。
TIER_PASS = 'pass'            # 実質利益率 >= 20%(従来の合格ラインそのまま)
TIER_CONSIDER = 'consider'    # 実質利益率 0%以上20%未満(手数料込みでも黒字、ただし閾値未満)
TIER_REFERENCE = 'reference'  # 実質利益率マイナスだが、手数料を一切引かない粗差
                               # (US価格 - JP原価)が0以上(=手数料が0円なら成立する)
TIER_REJECT = 'reject'        # 上記のいずれでもない、または価格データ自体が無い


def _classify_tier(
    margin_pct: float | None,
    us_price_usd: float | None,
    jp_cost_usd: float | None,
    min_margin_pct: float = MIN_MARGIN_PCT,
) -> str:
    """calc_unit_profit()の結果から4段階のtierを判定する。"""
    if margin_pct is None:
        return TIER_REJECT
    if margin_pct >= min_margin_pct:
        return TIER_PASS
    if margin_pct >= 0:
        return TIER_CONSIDER
    if us_price_usd is not None and jp_cost_usd is not None and (us_price_usd - jp_cost_usd) >= 0:
        return TIER_REFERENCE
    return TIER_REJECT


def evaluate_mcp_candidates(
    mcp_result: dict,
    min_margin_pct: float = MIN_MARGIN_PCT,
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
            'price_diff_rate_gross': candidate.get('price_diff_rate'),  # 手数料・送料考慮前
            'price_volatility_90d': candidate.get('price_volatility_90d'),
            'weight_kg': weight_kg,
            'weight_estimated': used_fallback_weight,
            'fee_estimated': used_fallback_fee,
            **profit,  # unit_profit_usd, margin_pct など (jp_cost_usd 含む)
            'jp_cost_jpy': cost['price'],
        }
        entry['tier'] = _classify_tier(profit['margin_pct'], profit['us_price_usd'], profit['jp_cost_usd'], min_margin_pct)

        if entry['tier'] == TIER_PASS:
            qualified.append(entry)
        else:
            entry['reason'] = f"実質利益率 {profit['margin_pct']:.1%} が閾値 {min_margin_pct:.0%} 未満"
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
            'price_diff_rate_gross': None,
            'price_volatility_90d': skip.get('price_volatility_90d'),
            'weight_kg': skip.get('weight_kg'),
            'weight_estimated': False,
            'fee_estimated': False,
            'us_price_usd': us_price,
            'jp_cost_jpy': jp_price,
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
            entry['tier'] = _classify_tier(profit['margin_pct'], profit['us_price_usd'], profit['jp_cost_usd'], min_margin_pct)

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

def add_keywords(keywords, source: str, seed_keyword: str = None) -> int:
    """キーワードをプールに追加する(既存のものはスキップ)。追加できた件数を返す。"""
    init_ops_tables()
    keywords = [str(k).strip() for k in keywords if str(k or '').strip()]
    if not keywords:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.executemany(
            '''
            INSERT OR IGNORE INTO keyword_pool (keyword, source, seed_keyword, added_at, times_used, total_qualified, status)
            VALUES (?, ?, ?, ?, 0, 0, 'active')
            ''',
            [(keyword, source, seed_keyword, now) for keyword in keywords],
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


def _is_searchable_keyword(candidate: str) -> bool:
    """お気に入りから拾った文字列が、Keepaのtitleキーワード検索(常にUS側の
    タイトルに対して行われる)として使えそうかを判定する: Amazonの大分類名
    そのものではないか、日本語表記ではないか。"""
    stripped = candidate.strip()
    if stripped.lower() in _AMAZON_TOP_LEVEL_CATEGORIES:
        return False
    if _contains_japanese(stripped):
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


def pick_next_keyword() -> str:
    """次にdaily_scan.pyで使うキーワードを選ぶ。一度も使っていないものを
    優先し、次に最後に使ってから時間が経っているものを優先する。
    プールが空の場合は None を返す(呼び出し側でフォールバックする)。
    """
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            '''
            SELECT keyword FROM keyword_pool
            WHERE status = 'active'
            ORDER BY times_used ASC, COALESCE(last_used_at, '') ASC
            LIMIT 1
            '''
        ).fetchone()
    return row[0] if row else None


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
                   times_used, total_qualified, status
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
        }
        for keyword, source, seed_keyword, added_at, last_used_at, times_used, total_qualified, status in rows
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


def pick_next_seller() -> str:
    """次にマイニングするセラーを選ぶ。一度も調べていないものを優先し、
    次に最後に調べてから時間が経っているものを優先する。
    プールが空の場合は None を返す(呼び出し側でスキップする)。
    """
    init_ops_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            '''
            SELECT seller_id FROM seller_pool
            WHERE status = 'active'
            ORDER BY times_mined ASC, COALESCE(last_mined_at, '') ASC
            LIMIT 1
            '''
        ).fetchone()
    return row[0] if row else None


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


if __name__ == '__main__':
    init_ops_tables()
    print(f'Ops/Finance テーブルを初期化しました: {DB_PATH}')
