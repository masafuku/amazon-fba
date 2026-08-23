"""Pure helper functions for turning raw Keepa product payloads into the
metrics the arbitrage search cares about. No network calls in this module.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .keepa_client import CsvType, CURRENCY_DIVISOR, NO_DATA_SENTINELS


def _stat_value(stats: Dict[str, Any], field: str, type_index: int) -> Optional[float]:
    arr = stats.get(field)
    if not arr or type_index >= len(arr):
        return None
    val = arr[type_index]
    if val is None or val in NO_DATA_SENTINELS:
        return None
    return val


def _stat_pair_value(stats: Dict[str, Any], field: str, type_index: int) -> Optional[float]:
    """For fields like `min`/`max`/`minInInterval`/`maxInInterval` which are
    arrays of [time, value] pairs indexed by csv type."""
    arr = stats.get(field)
    if not arr or type_index >= len(arr):
        return None
    pair = arr[type_index]
    if not pair or len(pair) < 2:
        return None
    val = pair[1]
    if val is None or val in NO_DATA_SENTINELS:
        return None
    return val


def current_price(product: Dict[str, Any], domain: str) -> Optional[float]:
    """Best-effort current landed price (buy box w/ shipping, falling back to
    marketplace new / Amazon), converted to the marketplace's major currency unit."""
    stats = product.get("stats") or {}
    divisor = CURRENCY_DIVISOR.get(domain.upper(), 100)
    for type_index in (CsvType.BUY_BOX_SHIPPING, CsvType.NEW, CsvType.AMAZON):
        raw = _stat_value(stats, "current", type_index)
        if raw is not None and raw >= 0:
            return raw / divisor
    return None


def sales_rank(product: Dict[str, Any]) -> Optional[int]:
    stats = product.get("stats") or {}
    val = _stat_value(stats, "current", CsvType.SALES)
    return int(val) if val is not None else None


def review_count(product: Dict[str, Any]) -> Optional[int]:
    stats = product.get("stats") or {}
    val = _stat_value(stats, "current", CsvType.COUNT_REVIEWS)
    return int(val) if val is not None else None


def rating(product: Dict[str, Any]) -> Optional[float]:
    stats = product.get("stats") or {}
    val = _stat_value(stats, "current", CsvType.RATING)
    return val / 10 if val is not None else None


def monthly_sold(product: Dict[str, Any]) -> Optional[int]:
    """Keepa's own bucketed estimate of units sold in the past ~30 days
    (Amazon's "X+ bought in past month" badge data). Already present on the
    standard /product response we fetch - no extra token cost to read it."""
    val = product.get("monthlySold")
    return int(val) if isinstance(val, (int, float)) and val >= 0 else None


def sales_rank_drops_30(product: Dict[str, Any]) -> Optional[int]:
    """Count of sales-rank improvements ("drops", i.e. rank number going
    down) in the past 30 days that Keepa considers sale-indicating. CEO:
    「先月の販売個数が取得できていないアイテムがたくさんあります。最終判断前には
    知りたいです」— monthly_sold is null for most ASINs because Amazon only
    shows its "bought in past month" badge above a ~50-units/month threshold
    (confirmed via Keepa's own Product.java docs: "Most ASINs do not have
    this value set"); re-fetching cannot fill it in for lower-velocity items,
    since the source data genuinely doesn't exist. salesRankDrops30 has no
    such floor, so it's used as a free fallback demand signal when
    monthly_sold is unavailable. Already present on the standard `stats`
    object we fetch (stats_days=90 in keepa_client.get_products()) - no
    extra token cost. Keepa's sentinel for "no value" is -1, normalized to
    None like monthly_sold()."""
    stats = product.get("stats") or {}
    val = stats.get("salesRankDrops30")
    return int(val) if isinstance(val, (int, float)) and val >= 0 else None


def sales_rank_drops_90(product: Dict[str, Any]) -> Optional[int]:
    """sales_rank_drops_30と同じ考え方の90日版。追加のKeepaコールなし(同じ
    statsオブジェクトの一部)。商品全体の目安であり、セラー別の販売数ではない
    点に注意 - CEO: 「過去の販売数、30日の販売数も取得して表示してください」に
    対する、Keepa APIで実際に取得できる範囲の代替値(セラー別の販売数は
    Offer.java/Stats.javaの公式構造体を確認したが該当フィールドが存在せず、
    APIでは取得不可能と判明したため)。"""
    stats = product.get("stats") or {}
    val = stats.get("salesRankDrops90")
    return int(val) if isinstance(val, (int, float)) and val >= 0 else None


def total_offer_count(product: Dict[str, Any]) -> Optional[int]:
    """現在ライブな出品(セラー)の総数。stats.totalOfferCountをそのまま読むだけ -
    通常の商品取得(1トークン/商品、どの候補でも既に取得済み)に含まれており、
    offers=N(約7トークン/商品)の追加コールは不要。

    以前はdistinct_seller_ids()でoffers配列を数えていたが、その配列には
    過去の(もう出品されていない)古いオファーが混在しており、実データで
    大きく水増しされることが判明した(ASIN B07V31TRKB: offers配列41件から
    38セラーとカウントしていたが、実際にライブなのは2件のみ - stats.
    totalOfferCount=2、liveOffersOrder=[36,34]と一致)。CEO:「セラー数が
    38となっていますが、keepaで直接見た値と明らかに違います」。

    既知の制約(Keepa非公開のため未確定): Amazon自身が出品中の場合にこの
    カウントに含まれるかどうかは、検証した2件のASINでは判別できなかった
    (どちらもAmazon自身のアクティブな出品が無かったため)。"""
    stats = product.get("stats") or {}
    val = stats.get("totalOfferCount")
    return int(val) if isinstance(val, (int, float)) and val >= 0 else None


def demand_signal(product: Dict[str, Any], domain: str) -> Dict[str, Any]:
    """複数の需要シグナルを優先順位付きで統合し、単一指標への過信を避ける
    (参考: https://www.buppan-ai-lab.com/keepa-seller-research-1-token-optimization/
    - monthlySold優先→salesRankDrops30→ランクのみ、という組み合わせ方を踏襲)。

    CEO: 「この記事を参考にして、今の実装に対して取り入れられ所はある？」への
    対応。monthly_sold()(Keepa自身の実売推定値、一部カテゴリのみ提供)を
    最優先とし、無ければsales_rank_drops_30()(過去30日のランク下落回数=
    販売機会の近似値であって実売数そのものではない)、それも無ければ
    sales_rank()のみを「参考」として返す。

    意図的にhigh/medium/low等の閾値による自動判定はしない - このセッション中の
    実例(HARIO V60ペーパーフィルター: monthly_sold最大6000 vs KTC工具:
    monthly_soldデータなし)を見ても、カテゴリによって桁が大きく異なるため、
    恣意的な閾値を導入するとかえって誤解を招く。「どの数字を根拠に・どの程度
    信頼して良いか(confidence)」を一貫した形で返すことに留め、大小の評価は
    人間の判断に委ねる。追加のKeepa呼び出しは発生しない(渡されたproductから
    導出するだけの純粋関数)。"""
    monthly = monthly_sold(product)
    drops_30 = sales_rank_drops_30(product)
    drops_90 = sales_rank_drops_90(product)
    rank = sales_rank(product)
    sellers = total_offer_count(product)

    if monthly is not None:
        primary_value, primary_label, confidence = monthly, "月間販売数(Keepa推定)", "high"
    elif drops_30 is not None:
        primary_value, primary_label, confidence = drops_30, "ランク変動30日(実売数の近似値)", "medium"
    elif rank is not None:
        primary_value, primary_label, confidence = rank, "売上ランクのみ(実売シグナルなし)", "low"
    else:
        primary_value, primary_label, confidence = None, "データなし", "none"

    return {
        "primary_value": primary_value,
        "primary_label": primary_label,
        "confidence": confidence,  # high/medium/low/none
        "monthly_sold": monthly,
        "sales_rank_drops_30": drops_30,
        "sales_rank_drops_90": drops_90,
        "sales_rank": rank,
        "competitor_seller_count": sellers,
    }


def brand_store_info(product: Dict[str, Any]) -> Dict[str, Any]:
    """AmazonブランドストアがそのブランドにあるかどうかをbrandStoreName/
    brandStoreUrlから判定する。追加のKeepaコールなし(通常の商品取得に
    既に含まれる)。

    CEO: 「amazonに正規代理店がいるかどうかは調べられる？」への対応。
    ブランドストアの存在は、そのブランドがAmazonブランド登録(Brand
    Registry)済みであることを示す無料のシグナル - 実データで確認した通り
    (HARIO: ブランドストアあり→実際に新規セラーの出品を拒否された。
    KTC: ブランドストア無し。VESSEL: ブランドストアあり→未検証だが
    要注意)、ブランドストアを持つブランドは出品ゲーティングのリスクが
    高い傾向がある。ただし完全な保証ではない(ブランドストアがあっても
    出品可能なブランドもあれば、無くても個別ASINが閉じているケースも
    あり得る) - 発注前の無料の一次スクリーニングとして使う。"""
    name = product.get("brandStoreName")
    url = product.get("brandStoreUrl")
    return {
        "has_brand_store": bool(name or url),
        "brand_store_name": name,
        "brand_store_url": url,
    }


def _live_offers(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    """product['offers'](offers_limit指定時のみ存在)を、現在ライブな
    (=もう出品終了していない)ものだけに絞り込む。実データで確定済み:
    product['liveOffersOrder']はoffers配列への**インデックス**のリスト
    (offerIdではない - ASIN B07V31TRKBで、liveOffersOrder=[36,34]は
    インデックス34・36の要素と一致し、その2件はlastSeen==stats.
    lastOffersUpdateとも一致する。offerIdとして解釈すると別の・古い2件に
    なり不一致だった)。liveOffersOrderが無ければlastSeen==stats.
    lastOffersUpdateにフォールバックする。"""
    offers = product.get("offers")
    if not offers or not isinstance(offers, list):
        return []
    stats = product.get("stats") or {}
    live_order = product.get("liveOffersOrder")
    if isinstance(live_order, list) and live_order:
        live_indices = set(live_order)
        return [o for i, o in enumerate(offers) if isinstance(o, dict) and i in live_indices]
    last_update = stats.get("lastOffersUpdate")
    if last_update is None:
        # liveOffersOrderもlastOffersUpdateも無ければ絞り込みようがない -
        # 以前の(誤った)全件返却に留める。
        return [o for o in offers if isinstance(o, dict)]
    return [o for o in offers if isinstance(o, dict) and o.get("lastSeen") == last_update]


def total_live_stock(product: Dict[str, Any]) -> Optional[int]:
    """ライブな出品の在庫数(stockCSVの最新値)の合計。keepa_client.get_products
    (offers_limit=N, include_stock=True)で取得した商品でのみ意味を持つ
    (stockCSVはstock=1指定時のみ各offerに付与される)。

    stats側の集計フィールド(stockPerCondition3rdFBA等)は使わない -
    Keepa公式ドキュメントで最大10までしか報告しない仕様と確認済み
    (CEOのスクリーンショットは48・16・合計64という10超えの実例)。
    個々のライブなofferのstockCSV最新値を合計する方式のみが正確。

    stockCSVを持つofferが1件も無ければ(=stock=1を付けずに取得した場合、
    または本当にデータが無い場合)Noneを返す(在庫0とは区別する)。"""
    live = _live_offers(product)
    if not live:
        return None
    total = 0
    any_stock_data = False
    for offer in live:
        stock_csv = offer.get("stockCSV")
        if not stock_csv or not isinstance(stock_csv, list) or len(stock_csv) < 2:
            continue
        any_stock_data = True
        total += stock_csv[-1]
    return total if any_stock_data else None


# Keepa uses these sentinel seller ids in buyBoxSellerIdHistory to mean
# "no seller qualified for the buy box" (-1) / "a brand-new, unknown seller"
# (-2) - neither is a real, queryable seller id.
_NO_SELLER_SENTINELS = ("-1", "-2")


def current_buy_box_seller_id(product: Dict[str, Any]) -> Optional[str]:
    """Most recent real seller id from buyBoxSellerIdHistory (flat array:
    [keepaMinutes, sellerId, keepaMinutes, sellerId, ...], most recent last).
    Only present when the product was fetched with buybox=1 (see
    keepa_client.get_products(include_buybox=True)) - this is the expensive
    (5 tokens/product) call, so only do this for candidates worth the cost
    (e.g. already-qualified ones, for seller-based expansion)."""
    history = product.get("buyBoxSellerIdHistory")
    if not history or not isinstance(history, list):
        return None
    # 末尾から遡って、-1/-2でない実在のsellerIdを探す(直近の有効な入札者)。
    for i in range(len(history) - 1, 0, -2):
        seller_id = history[i]
        if seller_id and seller_id not in _NO_SELLER_SENTINELS:
            return seller_id
    return None


def distinct_seller_ids(product: Dict[str, Any], exclude_amazon: bool = True) -> List[str]:
    """All distinct sellerIds *currently* offering this ASIN (not just the buy
    box winner) - Amazon's "Other sellers on Amazon" list, in effect. Only
    present when the product was fetched with offers_limit set (see
    keepa_client.get_products(offers_limit=N)); each real dict in the
    `offers` array has its own `sellerId`. Order is preserved, first-seen
    (roughly Amazon's own offer ranking), duplicates removed.

    Filters to live offers only via _live_offers() - the raw `offers` array
    mixes current listings with stale/historical ones no longer for sale
    (verified live: ASIN B07V31TRKB had 41 raw offers but only 2 were
    actually live), so counting/listing the raw array overstates real
    competition significantly.

    exclude_amazon=True (default) drops offers where isAmazon is true -
    Amazon itself isn't a "seller to mine" for this pipeline's purposes.
    """
    seen = set()
    seller_ids: List[str] = []
    for offer in _live_offers(product):
        if exclude_amazon and offer.get("isAmazon"):
            continue
        seller_id = offer.get("sellerId")
        if seller_id and seller_id not in seen:
            seen.add(seller_id)
            seller_ids.append(seller_id)
    return seller_ids


def price_volatility_ratio(product: Dict[str, Any], domain: str) -> Optional[float]:
    """(max - min) / avg over the interval the product was fetched with
    (call get_products(..., stats_days=90) so this reflects the last 90 days).
    Returns None if there isn't enough price history to compute it."""
    stats = product.get("stats") or {}
    for type_index in (CsvType.BUY_BOX_SHIPPING, CsvType.NEW, CsvType.AMAZON):
        lo = _stat_pair_value(stats, "minInInterval", type_index)
        hi = _stat_pair_value(stats, "maxInInterval", type_index)
        avg = _stat_value(stats, "avg", type_index)
        if lo is not None and hi is not None and avg:
            return (hi - lo) / avg
    return None


# Keepa timestamps in `csv`/`history` arrays are "Keepa minutes": minutes
# since 2011-01-01 00:00 UTC. Convert to real Unix milliseconds with this
# fixed offset (see https://keepa.com/#!discuss/t/time-values/13 and
# Keepa's own client libraries - this constant is the same in all of them).
KEEPA_MINUTES_EPOCH_OFFSET = 21564000


def csv_time_series(
    product: Dict[str, Any], type_index: int, divisor: float = 1.0
) -> List[Dict[str, Any]]:
    """Parse one `product['csv'][type_index]` flat array (only present when
    get_products(..., history=True) was used) into `[{time, value}, ...]`,
    `time` as Unix milliseconds and `value` converted to the marketplace's
    major currency unit (pass CURRENCY_DIVISOR[domain] for price series,
    1.0 for the unitless sales-rank series). Drops Keepa's "no data"
    sentinel entries. Returns [] if there's no csv data for this type
    (e.g. this product/domain has no price history in the window Keepa has
    on file)."""
    csv = product.get("csv")
    if not csv or type_index >= len(csv) or not csv[type_index]:
        return []
    raw = csv[type_index]
    series = []
    for i in range(0, len(raw) - 1, 2):
        keepa_minutes, value = raw[i], raw[i + 1]
        if keepa_minutes is None or value is None or value in NO_DATA_SENTINELS:
            continue
        unix_ms = (keepa_minutes + KEEPA_MINUTES_EPOCH_OFFSET) * 60_000
        series.append({"time": unix_ms, "value": round(value / divisor, 2) if divisor != 1.0 else value})
    return series


def package_weight_kg(product: Dict[str, Any]) -> Optional[float]:
    """Package weight in kg, used for international-shipping cost estimates.
    Keepa returns `packageWeight` in grams; falls back to `itemWeight` if the
    package figure isn't present. Returns None if neither is available (the
    caller should fall back to a manual/default estimate in that case)."""
    for field in ("packageWeight", "itemWeight"):
        raw = product.get(field)
        if raw is not None and raw > 0:
            return round(raw / 1000, 3)
    return None


def product_image_url(product: Dict[str, Any]) -> Optional[str]:
    """Primary product thumbnail URL.

    `imagesCSV` (when present) is a semicolon-separated string of bare
    Amazon image ids. `images` (the fallback when imagesCSV is absent) is
    structured instead - a list of per-image dicts with size-variant keys
    like "l" (large) / "m" (medium) filenames, NOT a list of plain
    strings - so it must not be naively str()'d (that stringifies the
    whole dict into the URL)."""
    images_csv = product.get("imagesCSV")
    if images_csv:
        image_value = str(images_csv).split(";")[0].strip()
    else:
        raw_images = product.get("images")
        image_value = ""
        if isinstance(raw_images, list) and raw_images:
            first = raw_images[0]
            if isinstance(first, dict):
                image_value = str(first.get("l") or first.get("hiRes") or first.get("m") or "").strip()
            else:
                image_value = str(first).strip()
        elif isinstance(raw_images, str):
            image_value = raw_images.split(";")[0].strip()

    if not image_value:
        return None
    if image_value.startswith(("http://", "https://")):
        return image_value
    # imagesCSV entries are bare ids with no extension; the "l"/"m" filenames
    # from `images` already end in .jpg - don't double it up.
    suffix = "" if image_value.lower().endswith((".jpg", ".jpeg", ".png", ".gif")) else ".jpg"
    return f"https://images-na.ssl-images-amazon.com/images/I/{image_value}{suffix}"


def referral_fee_percent(product: Dict[str, Any]) -> Optional[float]:
    """Amazon referral (selling) fee as a percentage of price, e.g. 15.01
    means 15.01%. Category-specific (ranges roughly 8-45% on Amazon), so
    prefer this over a flat assumption whenever Keepa has it."""
    val = product.get("referralFeePercentage")
    if val is None or val < 0:
        return None
    return val


def fba_pickpack_fee(product: Dict[str, Any], domain: str) -> Optional[float]:
    """FBA pick & pack fee in the marketplace's major currency unit
    (e.g. USD for domain="US"). Size/weight-tier dependent on Amazon's
    side, so this is Keepa's own per-product estimate, not a flat rate."""
    fees = product.get("fbaFees") or {}
    raw = fees.get("pickAndPackFee")
    if raw is None or raw < 0:
        return None
    divisor = CURRENCY_DIVISOR.get(domain.upper(), 100)
    return round(raw / divisor, 2)


def primary_code(product: Dict[str, Any]) -> Optional[str]:
    """Primary UPC, falling back to EAN, used to match the same physical
    product across Amazon marketplaces (ASINs differ per-domain)."""
    upc_list = product.get("upcList") or []
    if upc_list:
        return str(upc_list[0])
    ean_list = product.get("eanList") or []
    if ean_list:
        return str(ean_list[0])
    return None


def price_diff_rate(sell_price_local: float, sell_domain: str, cost_price_local: float,
                     cost_domain: str, usd_to_jpy: float) -> Optional[float]:
    """(sell - cost) / cost, with both prices normalized to JPY.

    sell_domain/cost_domain are Keepa domain codes ("US", "JP", ...).
    Only USD<->JPY conversion is implemented (matches the Japan->US arbitrage
    use case); pass already-JPY prices for cost_domain="JP".
    """
    if cost_price_local is None or cost_price_local <= 0 or sell_price_local is None:
        return None

    def to_jpy(amount: float, domain: str) -> float:
        if domain.upper() == "JP":
            return amount
        if domain.upper() == "US":
            return amount * usd_to_jpy
        raise ValueError(f"price_diff_rate only supports US/JP domains, got '{domain}'")

    sell_jpy = to_jpy(sell_price_local, sell_domain)
    cost_jpy = to_jpy(cost_price_local, cost_domain)
    if cost_jpy <= 0:
        return None
    return (sell_jpy - cost_jpy) / cost_jpy


def product_url(asin: str, domain: str) -> Optional[str]:
    if not asin:
        return None
    host = {"US": "amazon.com", "JP": "amazon.co.jp", "GB": "amazon.co.uk",
            "DE": "amazon.de", "FR": "amazon.fr", "CA": "amazon.ca"}.get(domain.upper())
    if not host:
        return None
    return f"https://www.{host}/dp/{asin}"
