#!/usr/bin/env python3
import json
import gzip
import mimetypes
import os
import re
import sqlite3
import subprocess
import zlib
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

# このスクリプトはOS環境変数だけを見ており、リポジトリルートの .env を
# 自動では読み込んでいなかった(KEEPA_API_KEY等をシェルでexportしていないと
# 動かない状態だった)。「python3 sqlite_api_server.py」をREADME通りシステムの
# python3で起動するケースではpython-dotenvが入っていないこともあるため、
# 外部パッケージに頼らず config.py と同じ最小実装で .env を読み込む。
def _load_dotenv(path):
    if not path.exists():
        return
    with path.open('r', encoding='utf-8') as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


_load_dotenv(Path(__file__).resolve().parent.parent / '.env')

HOST = os.getenv('API_HOST', '0.0.0.0')
PORT = 8001
DB_PATH = Path(__file__).resolve().parent / 'keepa_imports.sqlite3'

# 本番デプロイ(AWS等)向け: `npm run build` の出力(dist/)がこの隣に
# あれば、/api/* 以外のリクエストをその静的ファイルとして配信する。
# ローカル開発時(Vite dev server + プロキシ)は dist/ が存在しないので
# 何も変わらない - 単に無視される。1プロセスで完結させることで、Node/nginx
# 等を別途動かさずに済む(メモリの小さいインスタンス向け)。
DIST_DIR = Path(__file__).resolve().parent / 'dist'
KEEPA_QUERY_URL = 'https://api.keepa.com/query'
KEEPA_PRODUCT_URL = 'https://api.keepa.com/product'
KEEPA_TOKEN_URL = 'https://api.keepa.com/token'


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
    # imagesCSV(存在すれば)はセミコロン区切りの裸のAmazon画像ID文字列。
    # imagesCSVが無い場合のフォールバックである images は構造が違い、
    # 各要素が {'l': 'xxx.jpg', 'm': 'yyy.jpg', ...} のような辞書のリスト
    # なので、素朴に str() すると辞書がそのまま文字列化されてURLが壊れる。
    images_csv = product.get('imagesCSV')
    if images_csv:
        image_value = str(images_csv).split(';')[0].strip()
    else:
        raw_images = product.get('images')
        image_value = ''
        if isinstance(raw_images, list) and raw_images:
            first = raw_images[0]
            if isinstance(first, dict):
                image_value = str(first.get('l') or first.get('hiRes') or first.get('m') or '').strip()
            else:
                image_value = str(first).strip()
        elif isinstance(raw_images, str):
            image_value = raw_images.split(';')[0].strip()

    if image_value.startswith(('http://', 'https://')):
        image_url = image_value
    elif image_value:
        suffix = '' if image_value.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')) else '.jpg'
        image_url = f'https://images-na.ssl-images-amazon.com/images/I/{image_value}{suffix}'
    else:
        image_url = ''
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


def request_keepa_token_status():
    """Keepaの/tokenは残高確認専用でトークンを消費しない(公式ドキュメント記載)。"""
    api_key = os.getenv('KEEPA_API_KEY', '').strip()
    if not api_key:
        raise ValueError('KEEPA_API_KEY is not set on the API server environment')

    query = f'{KEEPA_TOKEN_URL}?{urlencode({"key": api_key})}'
    request = Request(url=query, headers={'Accept-Encoding': 'gzip, deflate'}, method='GET')

    try:
        with urlopen(request, timeout=15) as response:
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

    return {
        'tokensLeft': keepa_json.get('tokensLeft'),
        'refillIn': keepa_json.get('refillIn'),
        'refillRate': keepa_json.get('refillRate'),
    }


# ダッシュボードから検索ループ(scripts/run_all_day.shを回し続けるsystemd
# サービス)を停止・再開するための操作。AWS等のsystemd常駐デプロイでのみ
# 意味を持つ(ローカルのVite開発環境ではサービス自体が存在しないので、
# systemctlが見つからない/失敗するだけで実害はない)。
#
# 「停止」は実行中のdaily_scan.pyを中断しない(CEOの希望: 使いかけの
# トークンを無駄にしたくない) - run_all_day.shが見るフラグファイルを
# 立てるだけで、今のサイクルが終わり次第、次のサイクルを開始せず
# run_all_day.sh自身が終了する(scripts/run_all_day.sh参照)。
SCAN_LOOP_SERVICE = 'fba-scan-loop.service'
SCAN_LOOP_STOP_FLAG = Path(__file__).resolve().parent.parent / '.scan_loop_stop_requested'

# セラーマイニング/キーワード検索モードの手動切り替え(CEOの指示「セラー
# マイニングと検索モードの手動切り替もできるようにして欲しい」)。
# scripts/run_all_day.shが同じファイルを読む(get_mode_override())。
# 'auto'はファイルを消すことで表現する(停止フラグのresumeがunlinkするのと対称)。
SCAN_LOOP_MODE_FILE = Path(__file__).resolve().parent.parent / '.scan_loop_mode_override'
SCAN_LOOP_VALID_MODES = ('auto', 'seller-mining', 'keyword-search')


def get_scan_loop_mode():
    """不正な内容(空・破損・想定外の文字列)は安全側に倒してautoにする。"""
    try:
        raw = SCAN_LOOP_MODE_FILE.read_text(encoding='utf-8').strip()
    except (FileNotFoundError, OSError):
        return 'auto'
    return raw if raw in ('seller-mining', 'keyword-search') else 'auto'


def set_scan_loop_mode(mode):
    if mode not in SCAN_LOOP_VALID_MODES:
        raise ValueError("mode must be one of 'auto', 'seller-mining', 'keyword-search'")
    if mode == 'auto':
        SCAN_LOOP_MODE_FILE.unlink(missing_ok=True)
    else:
        tmp_path = SCAN_LOOP_MODE_FILE.with_suffix('.tmp')
        tmp_path.write_text(mode, encoding='utf-8')
        tmp_path.replace(SCAN_LOOP_MODE_FILE)  # run_all_day.shが半端な書き込みを読まないようアトミックに置換
    return {'mode': mode}


def get_scan_loop_status():
    """状態確認はsudo不要(systemctl is-activeは誰でも実行可能)。
    modeの読み取りはsystemctlに依存しないため、全ての分岐で常に含める。"""
    stop_requested = SCAN_LOOP_STOP_FLAG.exists()
    mode = get_scan_loop_mode()
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', SCAN_LOOP_SERVICE],
            capture_output=True, text=True, timeout=10,
        )
        systemd_status = (result.stdout or '').strip() or 'unknown'
    except FileNotFoundError:
        return {
            'status': 'unavailable', 'running': False, 'stopRequested': stop_requested, 'mode': mode,
            'note': 'systemctl not found (not a systemd deployment)',
        }
    except Exception as exc:
        return {'status': 'unknown', 'running': False, 'stopRequested': stop_requested, 'mode': mode, 'error': str(exc)}

    is_active = systemd_status == 'active'
    if is_active and stop_requested:
        status = 'stopping'  # 今のサイクルが終わり次第止まる
    elif is_active:
        status = 'running'
    else:
        status = 'stopped'
    return {'status': status, 'running': is_active, 'stopRequested': stop_requested, 'mode': mode}


def control_scan_loop(action):
    """action='stop': フラグファイルを立てるだけ(実行中のサイクルは
    中断しない)。action='resume': フラグファイルを消し、サービスが
    (フラグにより)既に終了していれば `sudo systemctl start` で起動し直す
    (既に稼働中ならstartは無害な no-op)。
    """
    if action not in ('stop', 'resume'):
        raise ValueError("action must be 'stop' or 'resume'")

    if action == 'stop':
        SCAN_LOOP_STOP_FLAG.touch()
        return {'ok': True, **get_scan_loop_status()}

    # resume
    SCAN_LOOP_STOP_FLAG.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'start', SCAN_LOOP_SERVICE],
            capture_output=True, text=True, timeout=20,
        )
    except FileNotFoundError:
        return {'ok': False, 'error': 'systemctl not found (not a systemd deployment)'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'systemctl start timed out'}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        return {'ok': False, 'error': detail or f'systemctl start failed (exit {result.returncode})'}
    return {'ok': True, **get_scan_loop_status()}


# セラーマイニング(ダッシュボードから直接、合格候補のセラーを特定→その
# 出品を評価する)。このAPIサーバー自体はstdlib-onlyで動く前提(mcp
# パッケージを直接importできない)なので、.venv/bin/pythonで
# scripts/seller_mine_cli.pyをサブプロセスとして呼ぶ - control_scan_loop()の
# systemctl呼び出しと同じ「別プロセスに任せる」設計。
VENV_PYTHON = Path(__file__).resolve().parent.parent / '.venv' / 'bin' / 'python'
SELLER_MINE_CLI = Path(__file__).resolve().parent.parent / 'scripts' / 'seller_mine_cli.py'
SELLER_MINE_TIMEOUT_SECONDS = 180  # 最大候補数×2回のKeepa往復を見込んだ余裕


def _run_seller_mine_cli(args):
    if not VENV_PYTHON.exists():
        return {'ok': False, 'error': f'{VENV_PYTHON} が見つかりません(.venvのセットアップが必要です)'}
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), str(SELLER_MINE_CLI), *args],
            capture_output=True, text=True, timeout=SELLER_MINE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'セラーマイニング処理がタイムアウトしました({SELLER_MINE_TIMEOUT_SECONDS}秒)'}
    stdout = (result.stdout or '').strip()
    if stdout:
        # seller_mine_cli.py はJSON1行だけを出す設計だが、依存パッケージ側の
        # 警告出力などが紛れ込む可能性を考慮し、末尾の行だけをパースする。
        last_line = stdout.splitlines()[-1]
        try:
            return json.loads(last_line)
        except ValueError:
            pass
    detail = (result.stderr or stdout or '').strip()
    return {'ok': False, 'error': detail or f'seller_mine_cli.py が異常終了しました(exit {result.returncode})'}


def discover_sellers_for_asin(payload):
    asin = str(payload.get('asin') or '').strip().upper()
    if not asin:
        raise ValueError('asin is required')
    max_sellers = to_int_or_default(payload.get('maxSellers'), 5)
    return _run_seller_mine_cli(['discover-sellers', '--asin', asin, '--max-sellers', str(max_sellers)])


def expand_from_seller_action(payload):
    seller_id = str(payload.get('sellerId') or '').strip()
    if not seller_id:
        raise ValueError('sellerId is required')
    max_candidates = to_int_or_default(payload.get('maxCandidates'), 15)
    args = ['expand', '--seller-id', seller_id, '--max-candidates', str(max_candidates)]
    seed_asin = str(payload.get('seedAsin') or '').strip().upper()
    if seed_asin:
        args += ['--seed-asin', seed_asin]
    return _run_seller_mine_cli(args)


# ASIN指定調査(CEO: 「ASIN指定で調査する入力UIを追加できますか？」)。
# AgentPage.jsxの入力欄・CandidateDetailPage.jsxの「未調査」フォールバック
# ボタンの両方から呼ばれる。seller_mine_cli.pyと同じサブプロセス橋渡し。
ASIN_LOOKUP_CLI = Path(__file__).resolve().parent.parent / 'scripts' / 'asin_lookup_cli.py'
ASIN_LOOKUP_TIMEOUT_SECONDS = 60  # 単一ASINの評価(最大US+JP2回のKeepa往復)なのでseller_mineより短くてよい


def _run_asin_lookup_cli(args):
    if not VENV_PYTHON.exists():
        return {'ok': False, 'error': f'{VENV_PYTHON} が見つかりません(.venvのセットアップが必要です)'}
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), str(ASIN_LOOKUP_CLI), *args],
            capture_output=True, text=True, timeout=ASIN_LOOKUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'ASIN調査処理がタイムアウトしました({ASIN_LOOKUP_TIMEOUT_SECONDS}秒)'}
    stdout = (result.stdout or '').strip()
    if stdout:
        last_line = stdout.splitlines()[-1]
        try:
            return json.loads(last_line)
        except ValueError:
            pass
    detail = (result.stderr or stdout or '').strip()
    return {'ok': False, 'error': detail or f'asin_lookup_cli.py が異常終了しました(exit {result.returncode})'}


def lookup_asin_action(payload):
    asin = str(payload.get('asin') or '').strip().upper()
    if not asin:
        raise ValueError('asin is required')
    return _run_asin_lookup_cli(['lookup', '--asin', asin])


# CandidateDetailPageの「セラー数」「在庫」取得ボタン専用(CEO: 「候補商品に対して、
# セラーの数、在庫...を取得できますか？」「セラー数が38となっていますが、keepaで
# 直接見た値と明らかに違います」)。v2: セラー数はoffers配列の水増しバグが判明した
# ため、通常の商品取得だけで再計算する軽いコマンドに変更 - 既存の値の有無に関わらず
# 「再取得」ボタンから常に呼べる(誤った値を上書き訂正するため)。在庫は逆に新規の
# トークン消費が発生するため、値が無い候補にのみボタンを出す。
# asin_lookup_cliと同じサブプロセス橋渡しパターン。
SELLER_COUNT_CLI = Path(__file__).resolve().parent.parent / 'scripts' / 'seller_count_cli.py'
SELLER_COUNT_TIMEOUT_SECONDS = 30  # v2: 通常の商品取得のみ(offers不要)なので短くてよい
STOCK_TIMEOUT_SECONDS = 60  # offers+stock取得(1回のKeepa往復)なのでseller_mineより短くてよい


def _run_seller_count_cli(args):
    if not VENV_PYTHON.exists():
        return {'ok': False, 'error': f'{VENV_PYTHON} が見つかりません(.venvのセットアップが必要です)'}
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), str(SELLER_COUNT_CLI), *args],
            capture_output=True, text=True, timeout=SELLER_COUNT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'セラー数取得処理がタイムアウトしました({SELLER_COUNT_TIMEOUT_SECONDS}秒)'}
    stdout = (result.stdout or '').strip()
    if stdout:
        last_line = stdout.splitlines()[-1]
        try:
            return json.loads(last_line)
        except ValueError:
            pass
    detail = (result.stderr or stdout or '').strip()
    return {'ok': False, 'error': detail or f'seller_count_cli.py が異常終了しました(exit {result.returncode})'}


def fetch_and_save_seller_count(run_id: str, asin: str):
    """Keepaへ実際に問い合わせてセラー数を取得し、その(run_id, asin)行の
    data_jsonへ書き戻す(他のフィールドは保持するread-modify-write)。"""
    run_id = str(run_id or '').strip()
    asin = str(asin or '').strip().upper()
    if not run_id or not asin:
        raise ValueError('runId and asin are required')

    result = _run_seller_count_cli(['fetch', '--asin', asin])
    if not result.get('ok'):
        return result

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT data_json FROM agent_candidates WHERE run_id = ? AND asin = ?',
            (run_id, asin),
        ).fetchone()
        if row is None:
            return {'ok': False, 'error': f'候補が見つかりません(run_id={run_id}, asin={asin})'}
        data = json.loads(row[0])
        data['competitor_seller_count'] = result.get('competitorSellerCount')
        conn.execute(
            'UPDATE agent_candidates SET data_json = ? WHERE run_id = ? AND asin = ?',
            (json.dumps(data, ensure_ascii=False), run_id, asin),
        )
        conn.commit()

    return {'ok': True, 'competitorSellerCount': result.get('competitorSellerCount')}


def _run_stock_cli(args):
    if not VENV_PYTHON.exists():
        return {'ok': False, 'error': f'{VENV_PYTHON} が見つかりません(.venvのセットアップが必要です)'}
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), str(SELLER_COUNT_CLI), *args],
            capture_output=True, text=True, timeout=STOCK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'在庫取得処理がタイムアウトしました({STOCK_TIMEOUT_SECONDS}秒)'}
    stdout = (result.stdout or '').strip()
    if stdout:
        last_line = stdout.splitlines()[-1]
        try:
            return json.loads(last_line)
        except ValueError:
            pass
    detail = (result.stderr or stdout or '').strip()
    return {'ok': False, 'error': detail or f'seller_count_cli.py が異常終了しました(exit {result.returncode})'}


def fetch_and_save_stock(run_id: str, asin: str):
    """Keepaへ実際に問い合わせて在庫合計を取得し(offers+stock、新規トークン消費あり)、
    その(run_id, asin)行のdata_jsonへ書き戻す(他のフィールドは保持するread-modify-write)。
    セラー数の再取得とは別ルート(セラー数は無料の再計算、在庫は有料の新規取得なので
    ボタンを混同させない)。"""
    run_id = str(run_id or '').strip()
    asin = str(asin or '').strip().upper()
    if not run_id or not asin:
        raise ValueError('runId and asin are required')

    result = _run_stock_cli(['fetch-stock', '--asin', asin])
    if not result.get('ok'):
        return result

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT data_json FROM agent_candidates WHERE run_id = ? AND asin = ?',
            (run_id, asin),
        ).fetchone()
        if row is None:
            return {'ok': False, 'error': f'候補が見つかりません(run_id={run_id}, asin={asin})'}
        data = json.loads(row[0])
        data['competitor_stock_total'] = result.get('competitorStockTotal')
        conn.execute(
            'UPDATE agent_candidates SET data_json = ? WHERE run_id = ? AND asin = ?',
            (json.dumps(data, ensure_ascii=False), run_id, asin),
        )
        conn.commit()

    return {'ok': True, 'competitorStockTotal': result.get('competitorStockTotal')}


# CandidateDetailPageの「詳細データ取得」ボタン専用。seller_mine_cli.pyと
# 同じサブプロセス橋渡しパターン(このAPIサーバーはstdlib-onlyでkeepa_mcpを
# 直接importできないため)。GET /api/agent/candidate-history はこのCLIを
# 一切呼ばず、product_historyテーブルを読むだけ(CEOの明示的な指示:
# 「一度データ取得したものは...明示的にボタンを押さない限り、再取得しない」)。
PRODUCT_HISTORY_CLI = Path(__file__).resolve().parent.parent / 'scripts' / 'product_history_cli.py'
PRODUCT_HISTORY_TIMEOUT_SECONDS = 60  # 単一ASINの1回きりのfetchなのでseller_mineより短くてよい


def load_product_history(asin: str):
    """DBに保存済みの履歴データを返すだけ(Keepaへは一切問い合わせない)。
    未取得なら {'asin': asin, 'history': None} を返す。"""
    asin = (asin or '').strip().upper()
    if not asin:
        return {'asin': asin, 'history': None}
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT domain, price_history_json, rank_history_json, fetched_at FROM product_history WHERE asin = ?',
            (asin,),
        ).fetchone()
    if row is None:
        return {'asin': asin, 'history': None}
    domain, price_history_json, rank_history_json, fetched_at = row
    return {
        'asin': asin,
        'history': {
            'domain': domain,
            'priceHistory': json.loads(price_history_json) if price_history_json else None,
            'rankHistory': json.loads(rank_history_json) if rank_history_json else None,
            'fetchedAt': fetched_at,
        },
    }


def fetch_and_save_product_history(asin: str):
    """「取得」/「再取得」ボタンから呼ばれる、唯一Keepaへ実際に問い合わせる
    経路。product_history_cli.py をサブプロセス実行し、成功したらDBに
    INSERT OR REPLACEで保存する(既存行があれば上書き = 明示的な再取得)。"""
    asin = (asin or '').strip().upper()
    if not asin:
        raise ValueError('asin is required')
    if not VENV_PYTHON.exists():
        return {'ok': False, 'error': f'{VENV_PYTHON} が見つかりません(.venvのセットアップが必要です)'}

    try:
        result = subprocess.run(
            [str(VENV_PYTHON), str(PRODUCT_HISTORY_CLI), 'fetch-history', '--asin', asin],
            capture_output=True, text=True, timeout=PRODUCT_HISTORY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'履歴データの取得がタイムアウトしました({PRODUCT_HISTORY_TIMEOUT_SECONDS}秒)'}

    stdout = (result.stdout or '').strip()
    output = None
    if stdout:
        last_line = stdout.splitlines()[-1]
        try:
            output = json.loads(last_line)
        except ValueError:
            pass
    if output is None:
        detail = (result.stderr or stdout or '').strip()
        return {'ok': False, 'error': detail or f'product_history_cli.py が異常終了しました(exit {result.returncode})'}
    if not output.get('ok'):
        return output
    if not output.get('found'):
        return {'ok': True, 'asin': asin, 'found': False, 'error': output.get('error')}

    fetched_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            '''
            INSERT INTO product_history (asin, domain, price_history_json, rank_history_json, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(asin) DO UPDATE SET
                domain = excluded.domain,
                price_history_json = excluded.price_history_json,
                rank_history_json = excluded.rank_history_json,
                fetched_at = excluded.fetched_at
            ''',
            (
                asin, output.get('domain') or 'US',
                json.dumps(output.get('priceHistory')), json.dumps(output.get('rankHistory')),
                fetched_at,
            ),
        )

    return {
        'ok': True,
        'asin': asin,
        'found': True,
        'history': {
            'domain': output.get('domain') or 'US',
            'priceHistory': output.get('priceHistory'),
            'rankHistory': output.get('rankHistory'),
            'fetchedAt': fetched_at,
        },
    }


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
                image_url TEXT,
                us_url TEXT,
                jp_asin TEXT,
                jp_url TEXT,
                us_price_usd REAL,
                jp_cost_jpy REAL,
                sales_rank INTEGER,
                review_count INTEGER,
                monthly_sold INTEGER,
                price_volatility_90d REAL,
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
        # 既存DBに対する後方互換マイグレーション(CREATE TABLE IF NOT EXISTSは
        # 既存テーブルに新カラムを追加してくれないため)。
        agent_candidates_columns = {row[1] for row in conn.execute('PRAGMA table_info(agent_candidates)').fetchall()}
        if 'image_url' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN image_url TEXT')
        if 'price_volatility_90d' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN price_volatility_90d REAL')
        if 'monthly_sold' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN monthly_sold INTEGER')
        # セラーマイニング由来の候補を区別する列(ops_finance.pyのinit_ops_tables()と
        # 揃える - 定義元はops_finance.py側)。
        if 'source_type' not in agent_candidates_columns:
            conn.execute("ALTER TABLE agent_candidates ADD COLUMN source_type TEXT NOT NULL DEFAULT 'keyword'")
        if 'seller_id' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN seller_id TEXT')
        if 'seller_name' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN seller_name TEXT')
        if 'seed_asin' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN seed_asin TEXT')
        if 'tier' not in agent_candidates_columns:
            conn.execute('ALTER TABLE agent_candidates ADD COLUMN tier TEXT')
            # ops_finance.py側と同じ一度きりのバックフィル(この2ファイルの並行
            # スキーマ管理という既存の規約通り)。
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
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_run_id ON agent_candidates(run_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_created_at ON agent_candidates(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_candidates_tier ON agent_candidates(tier)')
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
                notify_error TEXT,
                status TEXT NOT NULL DEFAULT 'running'
            )
            '''
        )
        agent_runs_columns = {row[1] for row in conn.execute('PRAGMA table_info(agent_runs)').fetchall()}
        if 'status' not in agent_runs_columns:
            conn.execute("ALTER TABLE agent_runs ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
        if 'source_type' not in agent_runs_columns:
            conn.execute("ALTER TABLE agent_runs ADD COLUMN source_type TEXT NOT NULL DEFAULT 'keyword'")
        if 'seller_id' not in agent_runs_columns:
            conn.execute('ALTER TABLE agent_runs ADD COLUMN seller_id TEXT')
        if 'seller_name' not in agent_runs_columns:
            conn.execute('ALTER TABLE agent_runs ADD COLUMN seller_name TEXT')
        if 'seed_asin' not in agent_runs_columns:
            conn.execute('ALTER TABLE agent_runs ADD COLUMN seed_asin TEXT')
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
        # Keyword/Categoryエージェントのキーワードプール。定義元はops_finance.py
        # 側だが、agent_runs と同じ理由でここにも同じ定義を用意しておく。
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS keyword_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                seed_keyword TEXT,
                added_at TEXT NOT NULL,
                last_used_at TEXT,
                times_used INTEGER NOT NULL DEFAULT 0,
                total_qualified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                price_min INTEGER
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_keyword_pool_status ON keyword_pool(status)')
        keyword_pool_columns = {row[1] for row in conn.execute('PRAGMA table_info(keyword_pool)').fetchall()}
        if 'price_min' not in keyword_pool_columns:
            conn.execute('ALTER TABLE keyword_pool ADD COLUMN price_min INTEGER')

        # Sellerエージェントのセラープール。定義元はops_finance.py側だが、
        # keyword_pool と同じ理由でここにも同じ定義を用意しておく。
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS seller_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id TEXT NOT NULL UNIQUE,
                seller_name TEXT,
                source TEXT NOT NULL,
                seed_asin TEXT,
                seed_keyword TEXT,
                added_at TEXT NOT NULL,
                last_mined_at TEXT,
                times_mined INTEGER NOT NULL DEFAULT 0,
                total_qualified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_seller_pool_status ON seller_pool(status)')

        seller_pool_columns = {row[1] for row in conn.execute('PRAGMA table_info(seller_pool)').fetchall()}
        if 'seed_keyword' not in seller_pool_columns:
            conn.execute('ALTER TABLE seller_pool ADD COLUMN seed_keyword TEXT')

        # seller_poolの一度きりの自動バックフィル(ops_finance.py側と同じロジック、
        # どちらのプロセスが先に起動しても安全 - INSERT OR IGNOREのため二重実行しても実害なし)。
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

        # CandidateDetailPageの「詳細データ取得」ボタン専用のオンデマンド
        # キャッシュ。daily_scan.py/ops_finance.pyの自動パイプラインからは
        # 一切触らないため(keyword_pool/seller_poolと違い)ops_finance.py
        # 側にはミラーしない——このサーバーだけで完結する。
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS product_history (
                asin TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                price_history_json TEXT,
                rank_history_json TEXT,
                fetched_at TEXT NOT NULL
            )
            '''
        )

        # ===================================================================
        # 収支・在庫ダッシュボード(SP-API連携)用テーブル群。ops_finance.py側の
        # init_ops_tables()と同一定義をここにもミラーする(CLAUDE.md記載の既知
        # パターン、二つのPythonエントリポイントはお互いをimportしない)。
        # スキーマを変更する場合は必ず両方を更新すること。Keepaには依存しない。
        # ===================================================================
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS sp_orders (
                order_id TEXT PRIMARY KEY,
                purchase_date TEXT,
                asin TEXT,
                sku TEXT,
                quantity INTEGER,
                item_price_usd REAL,
                order_status TEXT,
                updated_at TEXT
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sp_orders_asin ON sp_orders(asin)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sp_orders_purchase_date ON sp_orders(purchase_date)')

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS sp_financial_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                posted_date TEXT
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sp_financial_events_order_id ON sp_financial_events(order_id)')

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS sp_fba_inventory (
                asin TEXT PRIMARY KEY,
                sku TEXT,
                fnsku TEXT,
                fulfillable_quantity INTEGER,
                snapshot_at TEXT
            )
            '''
        )

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS sp_sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                orders_synced_at TEXT,
                finances_synced_at TEXT,
                inventory_synced_at TEXT
            )
            '''
        )

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS fixed_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                monthly_amount_jpy REAL NOT NULL,
                effective_from TEXT,
                note TEXT
            )
            '''
        )

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS jp_purchase_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_date TEXT,
                sd_reception_no TEXT UNIQUE,
                supplier_name TEXT,
                sd_product_no TEXT,
                product_name TEXT,
                jan_code TEXT,
                variant TEXT,
                unit_price_jpy REAL,
                quantity INTEGER,
                amount_jpy REAL,
                asin TEXT
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_jp_purchase_records_jan_code ON jp_purchase_records(jan_code)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_jp_purchase_records_asin ON jp_purchase_records(asin)')

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS asin_jan_map (
                asin TEXT PRIMARY KEY,
                jan_code TEXT NOT NULL
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_asin_jan_map_jan_code ON asin_jan_map(jan_code)')

        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS sd_parse_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_parsed_at TEXT
            )
            '''
        )


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


def load_finance_summary(days: int = 30, usd_to_jpy: float = 150.0):
    """収支ページ用の集計。ops_finance.pyのcompute_finance_summary()と同一ロジック
    (二つのPythonエントリポイントはお互いをimportしないという既存方針のため、
    ここにも複製する)。SP-API/Gmail未設定でデータが0件でも例外を投げず0埋めで返す。"""
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
            'SELECT asin, unit_price_jpy FROM jp_purchase_records WHERE asin IS NOT NULL ORDER BY order_date ASC'
        ):
            cost_by_asin[row['asin']] = row['unit_price_jpy']
        fixed_cost_rows = conn.execute(
            'SELECT id, name, monthly_amount_jpy, effective_from, note FROM fixed_costs ORDER BY id'
        ).fetchall()
        monthly_fixed_jpy = sum(row['monthly_amount_jpy'] for row in fixed_cost_rows)

    revenue_usd = sum((o['item_price_usd'] or 0) * (o['quantity'] or 0) for o in orders)
    fees_usd = sum(fees_by_order.values())
    cogs_usd = sum((cost_by_asin.get(o['asin'], 0) or 0) / usd_to_jpy * (o['quantity'] or 0) for o in orders)
    fixed_cost_period_usd = (monthly_fixed_jpy / usd_to_jpy) * (days / 30.0)
    net_profit_usd = revenue_usd - fees_usd - cogs_usd - fixed_cost_period_usd

    # 内訳(CEO: 「固定費の内訳もわかるようにして」) - 各項目の月額と、
    # 選択中の期間(days)に按分した金額の両方を返す。
    fixed_costs_breakdown = [
        {
            'id': row['id'],
            'name': row['name'],
            'monthlyAmountJpy': row['monthly_amount_jpy'],
            'periodUsd': round((row['monthly_amount_jpy'] / usd_to_jpy) * (days / 30.0), 2),
            'note': row['note'],
        }
        for row in fixed_cost_rows
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


def load_fba_inventory(days_for_velocity: int = 30):
    """在庫日数付きのFBA在庫一覧。ops_finance.pyのget_sp_fba_inventory_with_days_of_stock()と同一ロジック。"""
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
        result.append({
            'asin': row['asin'],
            'sku': row['sku'],
            'fnsku': row['fnsku'],
            'fulfillableQuantity': row['fulfillable_quantity'],
            'snapshotAt': row['snapshot_at'],
            'soldLast30d': sold,
            'daysOfStock': round(days_of_stock, 1) if days_of_stock is not None else None,
        })
    return result


def load_sp_orders(days: int = 30, limit: int = 100):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT order_id, purchase_date, asin, sku, quantity, item_price_usd, order_status
            FROM sp_orders WHERE purchase_date >= ?
            ORDER BY purchase_date DESC LIMIT ?
            ''',
            (since, limit),
        ).fetchall()
    return [
        {
            'orderId': order_id, 'purchaseDate': purchase_date, 'asin': asin, 'sku': sku,
            'quantity': quantity, 'itemPriceUsd': item_price_usd, 'orderStatus': order_status,
        }
        for order_id, purchase_date, asin, sku, quantity, item_price_usd, order_status in rows
    ]


def load_agent_candidates(days: int = 7):
    """Researchエージェントが調べた候補一覧(直近 days 日分)を、
    favoritesに既に追加済みかどうかのフラグ付きで返す。

    同じASINが複数回のスキャンで見つかった場合、agent_candidates には
    実行ごとに別の行として記録されている(agent_runs の集計を正確に保つ
    ため生ログとして全件残す設計)。表示上は同じ商品が重複して並ぶと
    分かりにくいので、ASINごとに最新1件だけに絞り込み、何回見つかったか
    (timesSeen)を添えて返す。
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            WITH ranked AS (
                SELECT
                    ac.*,
                    ROW_NUMBER() OVER (PARTITION BY ac.asin ORDER BY ac.created_at DESC) AS rn,
                    COUNT(*) OVER (PARTITION BY ac.asin) AS times_seen
                FROM agent_candidates ac
                WHERE ac.created_at >= ?
            )
            SELECT
                r.run_id, r.category, r.asin, r.title, r.image_url, r.us_url, r.jp_asin, r.jp_url,
                r.us_price_usd, r.jp_cost_jpy, r.sales_rank, r.review_count, r.monthly_sold, r.price_volatility_90d,
                r.weight_kg, r.weight_estimated, r.fee_estimated,
                r.price_diff_rate_gross, r.unit_profit_usd, r.margin_pct,
                r.qualified, r.tier, r.reason, r.data_json, r.created_at, r.times_seen,
                r.source_type, r.seller_id, r.seller_name, r.seed_asin,
                CASE WHEN f.asin IS NULL THEN 0 ELSE 1 END AS already_favorited
            FROM ranked r
            LEFT JOIN favorites f ON f.asin = r.asin
            WHERE r.rn = 1
            ORDER BY r.qualified DESC, r.margin_pct DESC, r.created_at DESC
            ''',
            (since,),
        ).fetchall()

    candidates = []
    for row in rows:
        (run_id, category, asin, title, image_url, us_url, jp_asin, jp_url,
         us_price_usd, jp_cost_jpy, sales_rank, review_count, monthly_sold, price_volatility_90d,
         weight_kg, weight_estimated, fee_estimated,
         price_diff_rate_gross, unit_profit_usd, margin_pct,
         qualified, tier, reason, data_json, created_at, times_seen,
         source_type, seller_id, seller_name, seed_asin, already_favorited) = row
        try:
            data = json.loads(data_json)
        except Exception:
            data = {}
        candidates.append({
            'runId': run_id,
            'category': category,
            'asin': asin,
            'title': title,
            'imageUrl': image_url,
            'usUrl': us_url,
            'jpAsin': jp_asin,
            'jpUrl': jp_url,
            'usPriceUsd': us_price_usd,
            'jpCostJpy': jp_cost_jpy,
            'salesRank': sales_rank,
            'reviewCount': review_count,
            'monthlySold': monthly_sold,
            'priceVolatility90d': price_volatility_90d,
            'weightKg': weight_kg,
            'weightEstimated': bool(weight_estimated),
            'feeEstimated': bool(fee_estimated),
            'priceDiffRateGross': price_diff_rate_gross,
            'unitProfitUsd': unit_profit_usd,
            'marginPct': margin_pct,
            'qualified': bool(qualified),
            'tier': tier or ('pass' if qualified else 'reject'),  # 安全側フォールバック(基本Noneにならない)
            'reason': reason,
            'data': data,
            'createdAt': created_at,
            'timesSeen': times_seen,
            'sourceType': source_type or 'keyword',
            'sellerId': seller_id,
            'sellerName': seller_name,
            'seedAsin': seed_asin,
            'alreadyFavorited': bool(already_favorited),
        })
    return candidates


def load_seller_candidates(seller_id: str):
    """SellerDetailPage用: 指定セラー(source_type='seller'かつseller_id一致)が
    出品していた商品を、load_agent_candidates()と同じASIN重複排除(最新1件+
    発見回数)で返す。CEO: 「セラーが売っている一覧とそのパフォーマンスを
    表形式で確認して、何が良いのかを理解しやすくしてほしい」への対応。

    load_agent_candidates()と違い、期間(days)では絞らない - そのセラーを
    再訪すべきかの参考情報として、過去に見つかった全商品を対象にする
    (seller_pool の集計が全期間であるのと同じ考え方)。
    """
    seller_id = (seller_id or '').strip()
    if not seller_id:
        return []

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            WITH ranked AS (
                SELECT
                    ac.*,
                    ROW_NUMBER() OVER (PARTITION BY ac.asin ORDER BY ac.created_at DESC) AS rn,
                    COUNT(*) OVER (PARTITION BY ac.asin) AS times_seen
                FROM agent_candidates ac
                WHERE ac.source_type = 'seller' AND ac.seller_id = ?
            )
            SELECT
                r.run_id, r.category, r.asin, r.title, r.image_url, r.us_url, r.jp_asin, r.jp_url,
                r.us_price_usd, r.jp_cost_jpy, r.sales_rank, r.review_count, r.monthly_sold, r.price_volatility_90d,
                r.weight_kg, r.weight_estimated, r.fee_estimated,
                r.price_diff_rate_gross, r.unit_profit_usd, r.margin_pct,
                r.qualified, r.tier, r.reason, r.data_json, r.created_at, r.times_seen,
                r.source_type, r.seller_id, r.seller_name, r.seed_asin,
                CASE WHEN f.asin IS NULL THEN 0 ELSE 1 END AS already_favorited
            FROM ranked r
            LEFT JOIN favorites f ON f.asin = r.asin
            WHERE r.rn = 1
            ORDER BY r.qualified DESC, r.margin_pct DESC, r.created_at DESC
            ''',
            (seller_id,),
        ).fetchall()

    candidates = []
    for row in rows:
        (run_id, category, asin, title, image_url, us_url, jp_asin, jp_url,
         us_price_usd, jp_cost_jpy, sales_rank, review_count, monthly_sold, price_volatility_90d,
         weight_kg, weight_estimated, fee_estimated,
         price_diff_rate_gross, unit_profit_usd, margin_pct,
         qualified, tier, reason, data_json, created_at, times_seen,
         source_type, row_seller_id, seller_name, seed_asin, already_favorited) = row
        try:
            data = json.loads(data_json)
        except Exception:
            data = {}
        candidates.append({
            'runId': run_id,
            'category': category,
            'asin': asin,
            'title': title,
            'imageUrl': image_url,
            'usUrl': us_url,
            'jpAsin': jp_asin,
            'jpUrl': jp_url,
            'usPriceUsd': us_price_usd,
            'jpCostJpy': jp_cost_jpy,
            'salesRank': sales_rank,
            'reviewCount': review_count,
            'monthlySold': monthly_sold,
            'priceVolatility90d': price_volatility_90d,
            'weightKg': weight_kg,
            'weightEstimated': bool(weight_estimated),
            'feeEstimated': bool(fee_estimated),
            'priceDiffRateGross': price_diff_rate_gross,
            'unitProfitUsd': unit_profit_usd,
            'marginPct': margin_pct,
            'qualified': bool(qualified),
            'tier': tier or ('pass' if qualified else 'reject'),
            'reason': reason,
            'data': data,
            'createdAt': created_at,
            'timesSeen': times_seen,
            'sourceType': source_type or 'seller',
            'sellerId': row_seller_id,
            'sellerName': seller_name,
            'seedAsin': seed_asin,
            'alreadyFavorited': bool(already_favorited),
        })
    return candidates


def load_agent_candidate_detail(asin: str):
    """CandidateDetailPage用: 1つのASINについて、直近のスキャンで見つかった
    最新の評価結果を1件返す(見つからなければNone)。load_agent_candidates()
    と同じCTE構造だが、daysの期間制限は付けない(過去に見つかった合格商品を
    後からいつでも参照できるようにするため)。
    """
    asin = (asin or '').strip().upper()
    if not asin:
        return None

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            '''
            WITH ranked AS (
                SELECT
                    ac.*,
                    ROW_NUMBER() OVER (PARTITION BY ac.asin ORDER BY ac.created_at DESC) AS rn,
                    COUNT(*) OVER (PARTITION BY ac.asin) AS times_seen
                FROM agent_candidates ac
                WHERE ac.asin = ?
            )
            SELECT
                r.run_id, r.category, r.asin, r.title, r.image_url, r.us_url, r.jp_asin, r.jp_url,
                r.us_price_usd, r.jp_cost_jpy, r.sales_rank, r.review_count, r.monthly_sold, r.price_volatility_90d,
                r.weight_kg, r.weight_estimated, r.fee_estimated,
                r.price_diff_rate_gross, r.unit_profit_usd, r.margin_pct,
                r.qualified, r.tier, r.reason, r.data_json, r.created_at, r.times_seen,
                r.source_type, r.seller_id, r.seller_name, r.seed_asin,
                CASE WHEN f.asin IS NULL THEN 0 ELSE 1 END AS already_favorited
            FROM ranked r
            LEFT JOIN favorites f ON f.asin = r.asin
            WHERE r.rn = 1
            ''',
            (asin,),
        ).fetchone()

    if row is None:
        return None

    (run_id, category, asin, title, image_url, us_url, jp_asin, jp_url,
     us_price_usd, jp_cost_jpy, sales_rank, review_count, monthly_sold, price_volatility_90d,
     weight_kg, weight_estimated, fee_estimated,
     price_diff_rate_gross, unit_profit_usd, margin_pct,
     qualified, tier, reason, data_json, created_at, times_seen,
     source_type, seller_id, seller_name, seed_asin, already_favorited) = row
    try:
        data = json.loads(data_json)
    except Exception:
        data = {}
    return {
        'runId': run_id,
        'category': category,
        'asin': asin,
        'title': title,
        'imageUrl': image_url,
        'usUrl': us_url,
        'jpAsin': jp_asin,
        'jpUrl': jp_url,
        'usPriceUsd': us_price_usd,
        'jpCostJpy': jp_cost_jpy,
        'salesRank': sales_rank,
        'reviewCount': review_count,
        'monthlySold': monthly_sold,
        'priceVolatility90d': price_volatility_90d,
        'weightKg': weight_kg,
        'weightEstimated': bool(weight_estimated),
        'feeEstimated': bool(fee_estimated),
        'priceDiffRateGross': price_diff_rate_gross,
        'unitProfitUsd': unit_profit_usd,
        'marginPct': margin_pct,
        'qualified': bool(qualified),
        'tier': tier or ('pass' if qualified else 'reject'),
        'reason': reason,
        'data': data,
        'createdAt': created_at,
        'timesSeen': times_seen,
        'sourceType': source_type or 'keyword',
        'sellerId': seller_id,
        'sellerName': seller_name,
        'seedAsin': seed_asin,
        'alreadyFavorited': bool(already_favorited),
    }


def load_agent_runs(days: int = 30):
    """daily_scan.py の実行履歴(直近 days 日分)を新しい順で返す。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            SELECT run_id, started_at, duration_seconds, keyword, category, category_id,
                   max_candidates, wait_for_tokens, evaluated, mcp_matched,
                   qualified_count, rejected_count, stopped_early_for_tokens,
                   error, notify_status, notify_error, status,
                   source_type, seller_id, seller_name, seed_asin
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
         error, notify_status, notify_error, status,
         source_type, seller_id, seller_name, seed_asin) = row
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
            'status': status or 'completed',
            'sourceType': source_type or 'keyword',
            'sellerId': seller_id,
            'sellerName': seller_name,
            'seedAsin': seed_asin,
        })
    return runs


def load_keyword_pool():
    """Keyword/Categoryエージェントのキーワードプール全体を返す
    (daily_scan.py --list-keywords / ops_finance.list_keyword_pool() と同じ並び順)。"""
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


# 食品・飲料・サプリなど、FBA輸出(日本→米国の小口国際発送)に向かない
# カテゴリのキーワードを除外する(CEO: 「食品などfba輸出に向かない物は検索
# から除外してください」)。定義元はops_finance.py側(_FOOD_AND_UNSUITABLE_KEYWORDS
# / _is_food_or_unsuitable_keyword)、この2ファイルの並行スキーマ管理という
# 既存の規約に従い、ダッシュボードからの手動追加パスにも同じフィルタを
# ミラーしておく。
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


def _is_food_or_unsuitable_keyword(candidate):
    lowered = candidate.strip().lower()
    return any(
        re.search(r'\b' + re.escape(term) + r'\b', lowered)
        for term in _FOOD_AND_UNSUITABLE_KEYWORDS
    )


def add_keyword_pool_entry(payload):
    """ダッシュボードからのマニュアル追加。既存キーワードは無視(重複追加しない)。"""
    keyword = str(payload.get('keyword') or '').strip()
    if not keyword:
        raise ValueError('keyword is required')
    if _is_food_or_unsuitable_keyword(keyword):
        raise ValueError(f'「{keyword}」は食品などFBA輸出に向かないカテゴリのため追加できません。')

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            '''
            INSERT OR IGNORE INTO keyword_pool
                (keyword, source, seed_keyword, added_at, times_used, total_qualified, status)
            VALUES (?, 'manual', NULL, ?, 0, 0, 'active')
            ''',
            (keyword, now),
        )
    return {'ok': True, 'added': cursor.rowcount > 0, 'keyword': keyword}


def delete_keyword_pool_entry(keyword):
    keyword = str(keyword or '').strip()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute('DELETE FROM keyword_pool WHERE keyword = ?', (keyword,))
    return {'ok': True, 'deleted': cursor.rowcount > 0, 'keyword': keyword}


def load_seller_pool():
    """Sellerエージェントのセラープール全体を、全期間の実績統計付きで返す
    (daily_scan.py --list-sellers / ops_finance.list_seller_pool() とは並び順は
    同じだが、こちらのみproduct_count等の集計列を追加で持つ - ダッシュボード
    「セラー別統計」専用、常に全期間集計(「このセラーを再訪すべきか」を判断する
    恒久的な参考情報のため、直近N日ではなく全履歴を見る)。

    agent_candidates は同じセラーの同じASINが複数回のマイニングで重複して
    残ることがある(load_agent_candidates()と同じ理由)ため、(seller_id, asin)
    単位で最新1件にデデュープしてから集計する。
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            '''
            WITH deduped AS (
                SELECT
                    ac.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY ac.seller_id, ac.asin
                        ORDER BY ac.created_at DESC
                    ) AS rn
                FROM agent_candidates ac
                WHERE ac.source_type = 'seller' AND ac.seller_id IS NOT NULL AND ac.seller_id != ''
            ),
            stats AS (
                SELECT
                    seller_id,
                    COUNT(DISTINCT asin) AS product_count,
                    SUM(CASE WHEN tier = 'pass' THEN 1 ELSE 0 END) AS pass_count,
                    SUM(CASE WHEN tier = 'consider' THEN 1 ELSE 0 END) AS consider_count,
                    SUM(CASE WHEN tier = 'reference' THEN 1 ELSE 0 END) AS reference_count,
                    SUM(CASE WHEN tier = 'reject' OR tier IS NULL THEN 1 ELSE 0 END) AS reject_count,
                    SUM(monthly_sold) AS total_monthly_sold,
                    AVG(margin_pct) AS avg_margin_pct
                FROM deduped
                WHERE rn = 1
                GROUP BY seller_id
            )
            SELECT sp.seller_id, sp.seller_name, sp.source, sp.seed_asin, sp.seed_keyword,
                   sp.added_at, sp.last_mined_at, sp.times_mined, sp.total_qualified, sp.status,
                   COALESCE(s.product_count, 0), COALESCE(s.pass_count, 0),
                   COALESCE(s.consider_count, 0), COALESCE(s.reference_count, 0),
                   COALESCE(s.reject_count, 0), s.total_monthly_sold, s.avg_margin_pct
            FROM seller_pool sp
            LEFT JOIN stats s ON s.seller_id = sp.seller_id
            ORDER BY sp.times_mined ASC, COALESCE(sp.last_mined_at, '') ASC
            '''
        ).fetchall()
    return [
        {
            'sellerId': seller_id,
            'sellerName': seller_name,
            'source': source,
            'seedAsin': seed_asin,
            'seedKeyword': seed_keyword,
            'addedAt': added_at,
            'lastMinedAt': last_mined_at,
            'timesMined': times_mined,
            'totalQualified': total_qualified,
            'status': status,
            'productCount': product_count,
            'passCount': pass_count,
            'considerCount': consider_count,
            'referenceCount': reference_count,
            'rejectCount': reject_count,
            'totalMonthlySold': total_monthly_sold,
            'avgMarginPct': avg_margin_pct,
        }
        for (seller_id, seller_name, source, seed_asin, seed_keyword, added_at, last_mined_at,
             times_mined, total_qualified, status, product_count, pass_count, consider_count,
             reference_count, reject_count, total_monthly_sold, avg_margin_pct) in rows
    ]


def add_seller_pool_entry(payload):
    """ダッシュボードからのマニュアル追加。既存セラーは無視(重複追加しない)。"""
    seller_id = str(payload.get('sellerId') or '').strip()
    if not seller_id:
        raise ValueError('sellerId is required')

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            '''
            INSERT OR IGNORE INTO seller_pool
                (seller_id, source, seed_asin, added_at, times_mined, total_qualified, status)
            VALUES (?, 'manual', NULL, ?, 0, 0, 'active')
            ''',
            (seller_id, now),
        )
    return {'ok': True, 'added': cursor.rowcount > 0, 'sellerId': seller_id}


def delete_seller_pool_entry(seller_id):
    seller_id = str(seller_id or '').strip()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute('DELETE FROM seller_pool WHERE seller_id = ?', (seller_id,))
    return {'ok': True, 'deleted': cursor.rowcount > 0, 'sellerId': seller_id}


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

    def _serve_static(self, url_path):
        """dist/ からの静的ファイル配信(本番デプロイ向け、_load_dotenv同様
        DIST_DIRが存在しない場合は何もしない)。SPAはハッシュルーティング
        (#agentなど)なので、/api/以外は基本的に常にindex.htmlを返せばよい。
        """
        if not DIST_DIR.is_dir():
            self._send_json(404, {'error': 'Not found'})
            return

        # ディレクトリトラバーサル対策: 解決後のパスがDIST_DIR配下か必ず確認する。
        relative = url_path.lstrip('/') or 'index.html'
        candidate = (DIST_DIR / relative).resolve()
        if not str(candidate).startswith(str(DIST_DIR.resolve()) + os.sep) and candidate != DIST_DIR.resolve():
            candidate = DIST_DIR / 'index.html'
        if not candidate.is_file():
            candidate = DIST_DIR / 'index.html'  # SPAフォールバック

        content_type, _ = mimetypes.guess_type(str(candidate))
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        # index.html自体はキャッシュさせない(デプロイのたびに更新されるため)。
        # ハッシュ付きファイル名のassetsは長期キャッシュしてよい。
        if candidate.name == 'index.html':
            self.send_header('Cache-Control', 'no-cache')
        else:
            self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
        self.end_headers()
        self.wfile.write(body)

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

        if parsed.path == '/api/agent/candidate':
            asin = (params.get('asin') or [''])[0]
            candidate = load_agent_candidate_detail(asin) if asin else None
            self._send_json(200, {'ok': True, 'candidate': candidate})
            return

        if parsed.path == '/api/agent/seller-candidates':
            seller_id = (params.get('sellerId') or [''])[0]
            self._send_json(200, {'ok': True, 'candidates': load_seller_candidates(seller_id)})
            return

        if parsed.path == '/api/agent/candidate-history':
            asin = (params.get('asin') or [''])[0]
            if not asin:
                self._send_json(400, {'error': 'asin is required'})
                return
            self._send_json(200, {'ok': True, **load_product_history(asin)})
            return

        if parsed.path == '/api/keepa/token':
            try:
                self._send_json(200, {'ok': True, **request_keepa_token_status()})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if parsed.path == '/api/agent/scan-loop':
            self._send_json(200, {'ok': True, **get_scan_loop_status()})
            return

        if parsed.path == '/api/finance/summary':
            days = to_int_or_default((params.get('days') or [None])[0], 30)
            try:
                self._send_json(200, {'ok': True, **load_finance_summary(days=days)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if parsed.path == '/api/finance/inventory':
            try:
                self._send_json(200, {'ok': True, 'inventory': load_fba_inventory()})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if parsed.path == '/api/finance/orders':
            days = to_int_or_default((params.get('days') or [None])[0], 30)
            try:
                self._send_json(200, {'ok': True, 'orders': load_sp_orders(days=days)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
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

        if parsed.path == '/api/keyword-pool':
            self._send_json(200, {'ok': True, 'keywords': load_keyword_pool()})
            return

        if parsed.path == '/api/seller-pool':
            self._send_json(200, {'ok': True, 'sellers': load_seller_pool()})
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

        if parsed.path.startswith('/api/'):
            self._send_json(404, {'error': 'Not found'})
            return

        self._serve_static(parsed.path)

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

        if self.path == '/api/keyword-pool':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                self._send_json(200, add_keyword_pool_entry(payload))
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/seller-pool':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                self._send_json(200, add_seller_pool_entry(payload))
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/agent/scan-loop':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                action = payload.get('action')
                mode = payload.get('mode')
                if action is None and mode is None:
                    raise ValueError("body must include 'action' and/or 'mode'")
                if action is not None:
                    control_scan_loop(str(action).strip())
                if mode is not None:
                    set_scan_loop_mode(str(mode).strip())
                result = get_scan_loop_status()
                result['ok'] = True
                self._send_json(200, result)
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/seller-mining/discover-sellers':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                self._send_json(200, discover_sellers_for_asin(payload))
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/seller-mining/expand':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                self._send_json(200, expand_from_seller_action(payload))
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/agent/candidate-history':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                asin = str(payload.get('asin') or '').strip().upper()
                if not asin:
                    raise ValueError('asin is required')
                self._send_json(200, fetch_and_save_product_history(asin))
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/agent/asin-lookup':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                self._send_json(200, lookup_asin_action(payload))
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/agent/seller-count':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                self._send_json(200, fetch_and_save_seller_count(payload.get('runId'), payload.get('asin')))
            except ValueError as exc:
                self._send_json(400, {'error': str(exc)})
            except Exception as exc:
                self._send_json(500, {'error': str(exc)})
            return

        if self.path == '/api/agent/stock':
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                self._send_json(200, fetch_and_save_stock(payload.get('runId'), payload.get('asin')))
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

        if parsed.path == '/api/keyword-pool':
            keyword = (parse_qs(parsed.query).get('keyword') or [''])[0]
            self._send_json(200, delete_keyword_pool_entry(keyword))
            return

        if parsed.path == '/api/seller-pool':
            seller_id = (parse_qs(parsed.query).get('sellerId') or [''])[0]
            self._send_json(200, delete_seller_pool_entry(seller_id))
            return

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
