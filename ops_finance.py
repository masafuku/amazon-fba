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

既存コードには手を加えず、このモジュールを import して使う。
DBパスは sqlite_api_server.py と同じ場所を参照する。
"""

import sqlite3
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

            CREATE INDEX IF NOT EXISTS idx_daily_costs_date ON daily_costs(date);
            CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(date);
            CREATE INDEX IF NOT EXISTS idx_sales_asin ON sales(asin);
            CREATE INDEX IF NOT EXISTS idx_inventory_log_asin ON inventory_log(asin);
            '''
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


def evaluate_mcp_candidates(
    mcp_result: dict,
    min_margin_pct: float = MIN_MARGIN_PCT,
    exchange_rate: float = 150.0,
) -> dict:
    """keepa_mcp.server.find_arbitrage_candidates() の戻り値を受け取り、
    各候補に calc_unit_profit() を通して実利益ベースでフィルタする。

    MCP側の price_diff_rate は FBA手数料・送料・関税を含まないため、
    ここで初めて「本当に儲かるか」を判定する。

    Returns:
        {
          'qualified': [利益率が閾値以上の候補 + profit詳細],
          'rejected':  [利益率が閾値未満だった候補 + 理由],
          'weight_missing': [重量データが無く仮値で計算した候補のASIN一覧],
        }
    """
    qualified, rejected, weight_missing = [], [], []

    for candidate in mcp_result.get('candidates', []):
        sell = candidate['sell']
        cost = candidate['cost']
        asin = sell['asin']

        weight_kg = sell.get('weight_kg')
        used_fallback_weight = weight_kg is None
        if used_fallback_weight:
            weight_kg = DEFAULT_WEIGHT_KG_FALLBACK
            weight_missing.append(asin)

        # cost['price'] は JP円、sell['price'] はUSD想定(sell_domain=US前提)
        profit = calc_unit_profit(
            us_price_usd=sell['price'],
            jp_cost_jpy=cost['price'],
            weight_kg=weight_kg,
            exchange_rate=exchange_rate,
        )

        entry = {
            'asin': asin,
            'title': sell.get('title'),
            'url': sell.get('url'),
            'sales_rank': sell.get('sales_rank'),
            'review_count': sell.get('review_count'),
            'price_diff_rate_gross': candidate.get('price_diff_rate'),  # 手数料・送料考慮前
            'weight_kg': weight_kg,
            'weight_estimated': used_fallback_weight,
            **profit,  # unit_profit_usd, margin_pct など
        }

        if profit['margin_pct'] >= min_margin_pct:
            qualified.append(entry)
        else:
            entry['reason'] = f"実質利益率 {profit['margin_pct']:.1%} が閾値 {min_margin_pct:.0%} 未満"
            rejected.append(entry)

    qualified.sort(key=lambda e: e['margin_pct'], reverse=True)

    return {
        'qualified': qualified,
        'rejected': rejected,
        'weight_missing': weight_missing,
        'evaluated': len(mcp_result.get('candidates', [])),
    }


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


if __name__ == '__main__':
    init_ops_tables()
    print(f'Ops/Finance テーブルを初期化しました: {DB_PATH}')
