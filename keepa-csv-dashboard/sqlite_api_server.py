#!/usr/bin/env python3
import json
import gzip
import os
import sqlite3
import zlib
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

HOST = os.getenv('API_HOST', '0.0.0.0')
PORT = 8001
DB_PATH = Path(__file__).resolve().parent / 'keepa_imports.sqlite3'
KEEPA_QUERY_URL = 'https://api.keepa.com/query'
KEEPA_PRODUCT_URL = 'https://api.keepa.com/product'


def to_int_or_default(value, default_value):
    try:
        return int(value)
    except Exception:
        return default_value


def build_keepa_finder_selection(keyword, min_new_price_jpy, max_sales_rank, per_page, page):
    return {
        'productType': ['0'],
        'title': keyword,
        'current_AMAZON_gte': -1,
        'current_AMAZON_lte': -1,
        'current_BUY_BOX_SHIPPING_gte': 3000,
        'monthlySoldPeak_gte': 10,
        'sort': [['current_SALES', 'asc'], ['monthlySold', 'desc']],
        'perPage': max(50, min(10000, per_page)),
        'page': max(0, page),
    }


def keepa_product_finder_search(payload):
    api_key = os.getenv('KEEPA_API_KEY', '').strip()
    if not api_key:
        raise ValueError('KEEPA_API_KEY is not set on the API server environment')

    keyword = str(payload.get('title') or payload.get('keyword') or '').strip()
    if not keyword:
        raise ValueError('title is required')

    domain = to_int_or_default(payload.get('domain'), 5)
    min_new_price_jpy = to_int_or_default(payload.get('minNewPriceYen'), 3000)
    max_sales_rank = to_int_or_default(payload.get('maxSalesRank'), 50000)
    per_page = to_int_or_default(payload.get('perPage'), 50)
    page = to_int_or_default(payload.get('page'), 0)
    include_stats = bool(payload.get('stats'))

    selection = build_keepa_finder_selection(
        keyword=keyword,
        min_new_price_jpy=min_new_price_jpy,
        max_sales_rank=max_sales_rank,
        per_page=per_page,
        page=page,
    )

    query = f'{KEEPA_QUERY_URL}?domain={domain}&key={api_key}'
    if include_stats:
        query += '&stats=1'

    request = Request(
        url=query,
        data=json.dumps(selection, ensure_ascii=False).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Accept-Encoding': 'gzip, deflate',
        },
        method='POST',
    )

    try:
        with urlopen(request, timeout=25) as response:
            raw_body = response.read()
            content_encoding = str(response.headers.get('Content-Encoding', '')).lower()

            if 'gzip' in content_encoding:
                decoded_body = gzip.decompress(raw_body)
            elif 'deflate' in content_encoding:
                decoded_body = zlib.decompress(raw_body)
            else:
                decoded_body = raw_body

            body = decoded_body.decode('utf-8')
            keepa_json = json.loads(body)
    except HTTPError as error:
        detail = error.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'Keepa HTTPError {error.code}: {detail}') from error
    except URLError as error:
        raise RuntimeError(f'Keepa request failed: {error}') from error

    saved = persist_finder_result(payload, keepa_json)

    return {
        'ok': True,
        'query': {
            'domain': domain,
            'selection': selection,
        },
        'keepa': keepa_json,
        'saved': saved,
    }


def decode_keepa_history(history):
    if not isinstance(history, list) or len(history) < 2:
        return None
    value = history[-1]
    return value if isinstance(value, (int, float)) and value >= 0 else None


def keepa_price_for_market(value, domain):
    if value is None:
        return None
    return value if int(domain) == 5 else value / 100


def extract_product_fields(product, domain):
    csv_history = product.get('csv') or []

    def latest_csv_value(index):
        history = csv_history[index] if len(csv_history) > index else None
        return decode_keepa_history(history)

    sales_rank = None
    sales_ranks = product.get('salesRanks') or {}
    reference = product.get('salesRankReference')
    rank_history = sales_ranks.get(str(reference)) if reference is not None else None
    if rank_history is None and sales_ranks:
        rank_history = next(iter(sales_ranks.values()))
    if isinstance(rank_history, list):
        sales_rank = decode_keepa_history(rank_history)

    category_tree = product.get('categoryTree') or []
    images = product.get('imagesCSV') or product.get('images') or ''
    if isinstance(images, list):
        images = ';'.join(str(image) for image in images)
    image_value = str(images).split(';')[0].strip()
    image_url = image_value if image_value.startswith(('http://', 'https://')) else (
        f'https://images-na.ssl-images-amazon.com/images/I/{image_value}.jpg' if image_value else ''
    )
    return {
        'title': product.get('title') or '',
        'imageUrl': image_url,
        'productType': product.get('productType'),
        'brand': product.get('brand'),
        'manufacturer': product.get('manufacturer'),
        'productCategory': category_tree[-1].get('name', '') if category_tree else '',
        'currentAmazonPrice': keepa_price_for_market(latest_csv_value(0), domain),
        'currentNewPrice': keepa_price_for_market(latest_csv_value(1), domain),
        'currentBuyBoxPrice': keepa_price_for_market(latest_csv_value(18), domain),
        'amazonAvailability': product.get('availabilityAmazon'),
        'monthlySold': product.get('monthlySold'),
        'salesRank': sales_rank,
        'lastUpdate': product.get('lastUpdate'),
        'lastSoldUpdate': product.get('lastSoldUpdate'),
        'referralFeePercentage': product.get('referralFeePercentage'),
        'fbaPickAndPackFee': keepa_price_for_market((product.get('fbaFees') or {}).get('pickAndPackFee'), domain),
    }


def request_keepa_product(asin, domain):
    api_key = os.getenv('KEEPA_API_KEY', '').strip()
    query = f'{KEEPA_PRODUCT_URL}?{urlencode({"key": api_key, "domain": domain, "asin": asin, "stats": 30, "history": 1})}'
    request = Request(url=query, headers={'Accept-Encoding': 'gzip, deflate'}, method='GET')

    try:
        with urlopen(request, timeout=30) as response:
            raw_body = response.read()
            content_encoding = str(response.headers.get('Content-Encoding', '')).lower()
            if 'gzip' in content_encoding:
                raw_body = gzip.decompress(raw_body)
            elif 'deflate' in content_encoding:
                raw_body = zlib.decompress(raw_body)
            keepa_json = json.loads(raw_body.decode('utf-8'))
    except HTTPError as error:
        detail = error.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'Keepa HTTPError {error.code}: {detail}') from error
    except URLError as error:
        raise RuntimeError(f'Keepa request failed: {error}') from error

    product = next((item for item in (keepa_json.get('products') or []) if item.get('asin') == asin), None)
    if product is None:
        raise RuntimeError(f'Keepa returned no product for ASIN {asin}')
    return keepa_json, product


def keepa_product_request(payload):
    api_key = os.getenv('KEEPA_API_KEY', '').strip()
    if not api_key:
        raise ValueError('KEEPA_API_KEY is not set on the API server environment')

    asin = str(payload.get('asin') or '').strip().upper()
    if not asin or ',' in asin or len(asin) > 20:
        raise ValueError('one valid ASIN is required')

    responses = {}
    fields_by_market = {}
    products_by_market = {}
    for market, domain in (('US', 1), ('JP', 5)):
        keepa_json, product = request_keepa_product(asin, domain)
        responses[market] = keepa_json
        products_by_market[market] = product
        fields_by_market[market] = extract_product_fields(product, domain)

    fetched_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            UPDATE keepa_finder_items
            SET title = ?, product_type = ?, brand = ?, manufacturer = ?, product_category = ?,
                us_current_amazon_price = ?, us_current_new_price = ?, us_current_buy_box_price = ?,
                us_referral_fee_percentage = ?, us_fba_pick_and_pack_fee = ?, us_monthly_sold = ?,
                us_sales_rank = ?, jp_current_amazon_price = ?, jp_current_new_price = ?,
                jp_current_buy_box_price = ?, jp_monthly_sold = ?, jp_sales_rank = ?,
                product_json = ?, product_fetched_at = ?, product_error = NULL
            WHERE run_id = ? AND asin = ?
            ''',
            (
                fields_by_market['US']['title'], fields_by_market['US']['productType'], fields_by_market['US']['brand'],
                fields_by_market['US']['manufacturer'], fields_by_market['US']['productCategory'],
                fields_by_market['US']['currentAmazonPrice'], fields_by_market['US']['currentNewPrice'],
                fields_by_market['US']['currentBuyBoxPrice'], fields_by_market['US']['referralFeePercentage'],
                fields_by_market['US']['fbaPickAndPackFee'], fields_by_market['US']['monthlySold'],
                fields_by_market['US']['salesRank'], fields_by_market['JP']['currentAmazonPrice'],
                fields_by_market['JP']['currentNewPrice'], fields_by_market['JP']['currentBuyBoxPrice'],
                fields_by_market['JP']['monthlySold'], fields_by_market['JP']['salesRank'],
                json.dumps(products_by_market, ensure_ascii=False), fetched_at,
                str(payload.get('runId') or ''), asin,
            ),
        )

    return {
        'ok': True,
        'asin': asin,
        'markets': fields_by_market,
        'products': products_by_market,
        'tokensConsumed': {market: responses[market].get('tokensConsumed') for market in responses},
        'fetchedAt': fetched_at,
    }


def domain_to_market(domain_value):
    if int(domain_value) == 5:
        return 'JP'
    return 'US'


def persist_finder_result(payload, keepa_json):
    created_at = datetime.now(timezone.utc).isoformat()
    run_id = f"finder-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    domain = to_int_or_default(payload.get('domain'), 5)
    keyword = str(payload.get('keyword') or '').strip()
    min_new_price_yen = to_int_or_default(payload.get('minNewPriceYen'), 0)
    max_sales_rank = to_int_or_default(payload.get('maxSalesRank'), 99999999)
    page = to_int_or_default(payload.get('page'), 0)
    per_page = to_int_or_default(payload.get('perPage'), 50)
    stats = 1 if bool(payload.get('stats')) else 0

    asin_list = list(keepa_json.get('asinList') or [])
    total_results = to_int_or_default(keepa_json.get('totalResults'), 0)
    market = domain_to_market(domain)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO keepa_finder_runs (
                run_id, domain, market, keyword, min_new_price_yen, max_sales_rank,
                page, per_page, stats, total_results, asin_count,
                request_json, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                run_id,
                domain,
                market,
                keyword,
                min_new_price_yen,
                max_sales_rank,
                page,
                per_page,
                stats,
                total_results,
                len(asin_list),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(keepa_json, ensure_ascii=False),
                created_at,
            ),
        )

        item_rows = [
            (
                run_id,
                market,
                asin,
                f'https://www.amazon.com/dp/{asin}',
                f'https://www.amazon.co.jp/dp/{asin}',
                f'https://keepa.com/#!product/{domain}-{asin}',
                created_at,
            )
            for asin in asin_list
            if isinstance(asin, str) and asin.strip()
        ]

        if item_rows:
            conn.executemany(
                '''
                INSERT OR IGNORE INTO keepa_finder_items (
                    run_id, market, asin, amazon_us_url, amazon_jp_url, keepa_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                item_rows,
            )

    return {
        'runId': run_id,
        'savedAsinCount': len(item_rows),
        'totalResults': total_results,
        'market': market,
    }


def load_finder_run(run_id):
    if not run_id:
        raise ValueError('runId is required')

    with sqlite3.connect(DB_PATH) as conn:
        run_row = conn.execute(
            '''
            SELECT run_id, response_json, total_results, asin_count, market
            FROM keepa_finder_runs
            WHERE run_id = ?
            ''',
            (run_id,),
        ).fetchone()
        item_rows = conn.execute(
            '''
            SELECT asin, product_json, product_fetched_at, product_error
            FROM keepa_finder_items
            WHERE run_id = ?
            ORDER BY id ASC
            ''',
            (run_id,),
        ).fetchall()

    if not run_row:
        raise ValueError('Finder run was not found')

    saved_run_id, response_json, total_results, asin_count, market = run_row
    try:
        keepa_json = json.loads(response_json)
    except Exception:
        keepa_json = {'asinList': []}

    markets_by_asin = {}
    debug_by_asin = {}
    errors_by_asin = {}
    fetched_at_by_asin = {}
    for asin, product_json, product_fetched_at, product_error in item_rows:
        if product_json:
            try:
                products = json.loads(product_json)
                markets_by_asin[asin] = {
                    market_name: extract_product_fields(product, 1 if market_name == 'US' else 5)
                    for market_name, product in products.items()
                    if isinstance(product, dict)
                }
                debug_by_asin[asin] = {
                    'status': 200,
                    'command': '復元: SQLiteに保存されたProduct APIレスポンス',
                    'responseText': json.dumps(products, ensure_ascii=False, indent=2),
                }
            except Exception:
                pass
        if product_fetched_at:
            fetched_at_by_asin[asin] = product_fetched_at
        if product_error:
            errors_by_asin[asin] = product_error

    return {
        'ok': True,
        'keepa': keepa_json,
        'saved': {
            'runId': saved_run_id,
            'savedAsinCount': asin_count,
            'totalResults': total_results,
            'market': market,
        },
        'marketsByAsin': markets_by_asin,
        'debugByAsin': debug_by_asin,
        'errorsByAsin': errors_by_asin,
        'fetchedAtByAsin': fetched_at_by_asin,
    }


def extract_product_category(row):
    if not isinstance(row, dict):
        return ''

    keys = list(row.keys())
    target_keywords = ['売れ筋ランキング: 参照', '売れ筋ランキング', '参照', 'Best Sellers Rank']

    for keyword in target_keywords:
        for key in keys:
            if key == keyword:
                value = row.get(key)
                if value is not None:
                    return str(value).strip()

    for keyword in target_keywords:
        for key in keys:
            if keyword in key:
                value = row.get(key)
                if value is not None:
                    return str(value).strip()

    return ''


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS keepa_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT,
                title TEXT,
                market TEXT NOT NULL,
                product_category TEXT,
                imported_at TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                source_file_name TEXT,
                row_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            '''
        )
        columns = {row[1] for row in conn.execute('PRAGMA table_info(keepa_items)').fetchall()}
        if 'product_category' not in columns:
            conn.execute('ALTER TABLE keepa_items ADD COLUMN product_category TEXT')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_keepa_batch ON keepa_items(batch_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_keepa_market ON keepa_items(market)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_keepa_imported_at ON keepa_items(imported_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_keepa_product_category ON keepa_items(product_category)')
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS keepa_finder_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                domain INTEGER NOT NULL,
                market TEXT NOT NULL,
                keyword TEXT NOT NULL,
                min_new_price_yen INTEGER NOT NULL,
                max_sales_rank INTEGER NOT NULL,
                page INTEGER NOT NULL,
                per_page INTEGER NOT NULL,
                stats INTEGER NOT NULL,
                total_results INTEGER NOT NULL,
                asin_count INTEGER NOT NULL,
                request_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            '''
        )
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS favorites (
                asin TEXT PRIMARY KEY,
                title TEXT,
                source TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            '''
        )
        # ops_finance.py (Researchエージェント/daily_scan.py) が書き込む先。
        # スキーマの定義元は ops_finance.py 側だが、このサーバーだけを先に
        # 起動した場合でも /api/agent/candidates がエラーにならないよう
        # ここでも同じ定義を用意しておく(どちらが先に作っても同じ結果)。
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS agent_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                category TEXT,
                asin TEXT NOT NULL,
                title TEXT,
                us_url TEXT,
                jp_asin TEXT,
                jp_url TEXT,
                us_price_usd REAL,
                jp_cost_jpy REAL,
                sales_rank INTEGER,
                review_count INTEGER,
                weight_kg REAL,
                weight_estimated INTEGER,
                fee_estimated INTEGER,
                price_diff_rate_gross REAL,
                unit_profit_usd REAL,
                margin_pct REAL,
                qualified INTEGER NOT NULL,
                reason TEXT,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_run_id ON agent_candidates(run_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_created_at ON agent_candidates(created_at)')
        # ops_finance.py (daily_scan.py) が書き込む実行履歴。定義元はops_finance.py
        # 側だが、agent_candidates と同じ理由でここにも同じ定義を用意しておく。
        conn.execute(
            '''
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
                notify_status TEXT,
                notify_error TEXT
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent_runs(started_at)')
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS keepa_finder_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                market TEXT NOT NULL,
                asin TEXT NOT NULL,
                title TEXT,
                product_type INTEGER,
                brand TEXT,
                manufacturer TEXT,
                product_category TEXT,
                current_amazon_price REAL,
                current_new_price REAL,
                current_buy_box_price REAL,
                amazon_availability INTEGER,
                monthly_sold INTEGER,
                sales_rank INTEGER,
                last_update INTEGER,
                last_sold_update INTEGER,
                product_json TEXT,
                product_fetched_at TEXT,
                product_error TEXT,
                amazon_us_url TEXT,
                amazon_jp_url TEXT,
                keepa_url TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (run_id, asin)
            )
            '''
        )
        item_columns = {row[1] for row in conn.execute('PRAGMA table_info(keepa_finder_items)').fetchall()}
        item_column_definitions = {
            'title': 'TEXT',
            'product_type': 'INTEGER',
            'brand': 'TEXT',
            'manufacturer': 'TEXT',
            'product_category': 'TEXT',
            'current_amazon_price': 'REAL',
            'current_new_price': 'REAL',
            'current_buy_box_price': 'REAL',
            'amazon_availability': 'INTEGER',
            'monthly_sold': 'INTEGER',
            'sales_rank': 'INTEGER',
            'last_update': 'INTEGER',
            'last_sold_update': 'INTEGER',
            'product_json': 'TEXT',
            'product_fetched_at': 'TEXT',
            'product_error': 'TEXT',
            'us_current_amazon_price': 'REAL',
            'us_current_new_price': 'REAL',
            'us_current_buy_box_price': 'REAL',
            'us_referral_fee_percentage': 'REAL',
            'us_fba_pick_and_pack_fee': 'REAL',
            'us_monthly_sold': 'INTEGER',
            'us_sales_rank': 'INTEGER',
            'jp_current_amazon_price': 'REAL',
            'jp_current_new_price': 'REAL',
            'jp_current_buy_box_price': 'REAL',
            'jp_monthly_sold': 'INTEGER',
            'jp_sales_rank': 'INTEGER',
        }
        for column, definition in item_column_definitions.items():
            if column not in item_columns:
                conn.execute(f'ALTER TABLE keepa_finder_items ADD COLUMN {column} {definition}')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_keepa_finder_runs_created_at ON keepa_finder_runs(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_keepa_finder_items_run_id ON keepa_finder_items(run_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_keepa_finder_items_asin ON keepa_finder_items(asin)')


def insert_rows(rows, metadata):
    imported_at = metadata.get('importedAt') or datetime.now(timezone.utc).isoformat()
    batch_id = metadata.get('batchId') or f"{imported_at}-batch"
    market = metadata.get('market') or 'US'
    source_file_name = metadata.get('sourceFileName') or ''
    created_at = datetime.now(timezone.utc).isoformat()

    records = []
    for row in rows:
        asin = row.get('asin') or row.get('ASIN') or ''
        title = row.get('title') or row.get('Title') or row.get('商品名') or ''
        product_category = row.get('productCategory') or row.get('product_category') or extract_product_category(row)
        records.append(
            (
                asin,
                title,
                market,
                product_category,
                imported_at,
                batch_id,
                source_file_name,
                json.dumps(row, ensure_ascii=False),
                created_at,
            )
        )

    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            '''
            INSERT INTO keepa_items (
                asin, title, market, product_category, imported_at, batch_id, source_file_name, row_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            records,
        )

    return {'insertedCount': len(records), 'batchId': batch_id}


def save_favorite(payload):
    asin = str(payload.get('asin') or '').strip().upper()
    if not asin:
        raise ValueError('asin is required')

    title = str(payload.get('title') or '').strip()
    source = str(payload.get('source') or 'unknown').strip()
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO favorites (asin, title, source, data_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asin) DO UPDATE SET
                title = excluded.title,
                source = excluded.source,
                data_json = excluded.data_json,
                updated_at = excluded.updated_at
            ''',
            (asin, title, source, json.dumps(data, ensure_ascii=False), now, now),
        )
    return {'ok': True, 'asin': asin}


def load_favorites():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            'SELECT asin, title, source, data_json, created_at, updated_at FROM favorites ORDER BY updated_at DESC'
        ).fetchall()

    favorites = []
    for asin, title, source, data_json, created_at, updated_at in rows:
        try:
            data = json.loads(data_json)
        except Exception:
            data = {}
        favorites.append({
            'asin': asin,
            'title': title,
            'source': source,
            'data': data,
            'createdAt': created_at,
            'updatedAt': updated_at,
        })
    return favorites


def load_agent_candidates(days: int = 7):
    """Researchエージェントが調べた候補一覧(直近 days 日分)を、
    favoritesに既に追加済みかどうかのフラグ付きで返す。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT
                ac.run_id, ac.category, ac.asin, ac.title, ac.us_url, ac.jp_asin, ac.jp_url,
                ac.us_price_usd, ac.jp_cost_jpy, ac.sales_rank, ac.review_count,
                ac.weight_kg, ac.weight_estimated, ac.fee_estimated,
                ac.price_diff_rate_gross, ac.unit_profit_usd, ac.margin_pct,
                ac.qualified, ac.reason, ac.data_json, ac.created_at,
                CASE WHEN f.asin IS NULL THEN 0 ELSE 1 END AS already_favorited
            FROM agent_candidates ac
            LEFT JOIN favorites f ON f.asin = ac.asin
            WHERE ac.created_at >= ?
            ORDER BY ac.qualified DESC, ac.margin_pct DESC, ac.created_at DESC
            ''',
            (since,),
        ).fetchall()

    candidates = []
    for row in rows:
        (run_id, category, asin, title, us_url, jp_asin, jp_url,
         us_price_usd, jp_cost_jpy, sales_rank, review_count,
         weight_kg, weight_estimated, fee_estimated,
         price_diff_rate_gross, unit_profit_usd, margin_pct,
         qualified, reason, data_json, created_at, already_favorited) = row
        try:
            data = json.loads(data_json)
        except Exception:
            data = {}
        candidates.append({
            'runId': run_id,
            'category': category,
            'asin': asin,
            'title': title,
            'usUrl': us_url,
            'jpAsin': jp_asin,
            'jpUrl': jp_url,
            'usPriceUsd': us_price_usd,
            'jpCostJpy': jp_cost_jpy,
            'salesRank': sales_rank,
            'reviewCount': review_count,
            'weightKg': weight_kg,
            'weightEstimated': bool(weight_estimated),
            'feeEstimated': bool(fee_estimated),
            'priceDiffRateGross': price_diff_rate_gross,
            'unitProfitUsd': unit_profit_usd,
            'marginPct': margin_pct,
            'qualified': bool(qualified),
            'reason': reason,
            'data': data,
            'createdAt': created_at,
            'alreadyFavorited': bool(already_favorited),
        })
    return candidates


def load_agent_runs(days: int = 30):
    """daily_scan.py の実行履歴(直近 days 日分)を新しい順で返す。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT run_id, started_at, duration_seconds, keyword, category, category_id,
                   max_candidates, wait_for_tokens, evaluated, mcp_matched,
                   qualified_count, rejected_count, stopped_early_for_tokens,
                   error, notify_status, notify_error
            FROM agent_runs
            WHERE started_at >= ?
            ORDER BY started_at DESC
            ''',
            (since,),
        ).fetchall()

    runs = []
    for row in rows:
        (run_id, started_at, duration_seconds, keyword, category, category_id,
         max_candidates, wait_for_tokens, evaluated, mcp_matched,
         qualified_count, rejected_count, stopped_early_for_tokens,
         error, notify_status, notify_error) = row
        runs.append({
            'runId': run_id,
            'startedAt': started_at,
            'durationSeconds': duration_seconds,
            'keyword': keyword,
            'category': category,
            'categoryId': category_id,
            'maxCandidates': max_candidates,
            'waitForTokens': bool(wait_for_tokens),
            'evaluated': evaluated,
            'mcpMatched': mcp_matched,
            'qualifiedCount': qualified_count,
            'rejectedCount': rejected_count,
            'stoppedEarlyForTokens': bool(stopped_early_for_tokens),
            'error': error,
            'notifyStatus': notify_status,
            'notifyError': notify_error,
        })
    return runs


def delete_favorite(asin):
    asin = str(asin or '').strip().upper()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute('DELETE FROM favorites WHERE asin = ?', (asin,))
    return {'ok': True, 'deleted': cursor.rowcount > 0, 'asin': asin}


def load_latest_market_rows(market):
    with sqlite3.connect(DB_PATH) as conn:
        latest_imported = conn.execute(
            '''
            SELECT imported_at
            FROM keepa_items
            WHERE market = ?
            ORDER BY imported_at DESC
            LIMIT 1
            ''',
            (market,),
        ).fetchone()

        if not latest_imported:
            return {'rows': [], 'batchId': None, 'importedAt': None}

        imported_at = latest_imported[0]
        raw_rows = conn.execute(
            '''
            SELECT asin, row_json, imported_at, batch_id, id
            FROM keepa_items
            WHERE market = ?
            ORDER BY imported_at DESC, id DESC
            ''',
            (market,),
        ).fetchall()

    seen_asins = set()
    rows = []
    batch_ids = set()
    for asin, raw_json, row_imported_at, row_batch_id, _row_id in raw_rows:
        asin_key = (asin or '').strip()
        if not asin_key:
            continue
        if asin_key in seen_asins:
            continue

        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                parsed['importedAt'] = row_imported_at
                rows.append(parsed)
                seen_asins.add(asin_key)
                batch_ids.add(row_batch_id)
        except Exception:
            continue

    batch_id = f'multi:{len(batch_ids)}'
    return {'rows': rows, 'batchId': batch_id, 'importedAt': imported_at}


def list_batches():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT market, batch_id, imported_at, source_file_name, COUNT(*) AS row_count
            FROM keepa_items
            GROUP BY market, batch_id, imported_at, source_file_name
            ORDER BY imported_at DESC, batch_id DESC
            '''
        ).fetchall()

    result = []
    for market, batch_id, imported_at, source_file_name, row_count in rows:
        result.append(
            {
                'market': market,
                'batchId': batch_id,
                'importedAt': imported_at,
                'sourceFileName': source_file_name,
                'rowCount': row_count,
            }
        )
    return result


def get_db_stats():
    with sqlite3.connect(DB_PATH) as conn:
        row_count = conn.execute('SELECT COUNT(*) FROM keepa_items').fetchone()[0]

    return {
        'dbPath': str(DB_PATH),
        'dbFileName': DB_PATH.name,
        'rowCount': row_count,
    }


def load_batch_rows(market, batch_id):
    with sqlite3.connect(DB_PATH) as conn:
        raw_rows = conn.execute(
            '''
            SELECT row_json
            FROM keepa_items
            WHERE market = ? AND batch_id = ?
            ORDER BY id ASC
            ''',
            (market, batch_id),
        ).fetchall()

    rows = []
    for (raw_json,) in raw_rows:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                rows.append(parsed)
        except Exception:
            continue
    return rows


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send_json(200, {'ok': True})

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == '/api/health':
            stats = get_db_stats()
            self._send_json(200, {'ok': True, **stats})
            return

        if parsed.path == '/api/favorites':
            self._send_json(200, {'ok': True, 'favorites': load_favorites()})
            return

        if parsed.path == '/api/agent/candidates':
            days = to_int_or_default((params.get('days') or [None])[0], 7)
            self._send_json(200, {'ok': True, 'candidates': load_agent_candidates(days=days)})
            return

        if parsed.path == '/api/agent/runs':
            days = to_int_or_default((params.get('days') or [None])[0], 30)
            self._send_json(200, {'ok': True, 'runs': load_agent_runs(days=days)})
            return

        if parsed.path == '/api/latest':
            us_data = load_latest_market_rows('US')
            jp_data = load_latest_market_rows('JP')
            self._send_json(
                200,
                {
                    'ok': True,
                    'US': us_data,
                    'JP': jp_data,
                },
            )
            return

        if parsed.path == '/api/batches':
            self._send_json(200, {'ok': True, 'batches': list_batches()})
            return

        if parsed.path == '/api/batch':
            market = (params.get('market') or [''])[0].upper()
            batch_id = (params.get('batchId') or [''])[0]
            if market not in {'US', 'JP'} or not batch_id:
                self._send_json(400, {'error': 'market and batchId are required'})
                return

            rows = load_batch_rows(market, batch_id)
            self._send_json(
                200,
                {
                    'ok': True,
                    'market': market,
                    'batchId': batch_id,
                    'rows': rows,
                },
            )
            return

        if parsed.path == '/api/keepa/finder-run':
            run_id = (params.get('runId') or [''])[0]
            try:
                self._send_json(200, load_finder_run(run_id))
            except ValueError as exc:
                self._send_json(404, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        self._send_json(404, {'error': 'Not found'})

    def do_POST(self):
        if self.path == '/api/favorites':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                self._send_json(200, save_favorite(payload))
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/keepa/product-finder':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)

            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                result = keepa_product_finder_search(payload)
                self._send_json(200, result)
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/keepa/product':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)

            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                result = keepa_product_request(payload)
                self._send_json(200, result)
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path != '/api/import':
            self._send_json(404, {'error': 'Not found'})
            return

        length = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(length)

        try:
            payload = json.loads(raw.decode('utf-8'))
            rows = payload.get('rows') or []
            metadata = payload.get('metadata') or {}
            result = insert_rows(rows, metadata)
            self._send_json(200, result)
        except Exception as exc:
            self._send_json(500, {'error': str(exc)})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path != '/api/favorites':
            self._send_json(404, {'error': 'Not found'})
            return

        asin = (parse_qs(parsed.query).get('asin') or [''])[0]
        self._send_json(200, delete_favorite(asin))


if __name__ == '__main__':
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'SQLite API server started: http://{HOST}:{PORT}')
    print(f'DB file: {DB_PATH}')
    server.serve_forever()
