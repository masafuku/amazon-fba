"""NETSEA(卸売り仕入れサイト)のJANコード付き商品を、Amazon US側と完全一致で
突き合わせて実質利益を計算するパイプライン。daily_scan.py のKeepaキーワード検索
経路とは別の、独立した仕入れ発見経路。

CEO: 「Amazon以外にネット系の卸売り業者からの仕入れも考えたほうがよいと考えます。
自動で行いたい」「GTINやJANなどでSKUの完全一致確認をする方法を提案してください」

設計判断(前回のキーワード検索デモで実際に誤マッチ - Parker Jotterの替え芯が
本体ペンの検索結果に紛れ込んだ - を踏まえたもの):
  - JANコードが無い商品(NETSEA全体の約8割)は対象外とする。あいまいなキーワード
    一致によるフォールバックは追加しない - 「完全一致」というCEOの要求を優先する。
  - jp_cost_jpy には、NETSEAの実際の卸価格(wholesale_cost_jpy)と、同じJANで
    Amazon JPにも出品があればその価格(jp_amazon_cost_jpy)のうち安い方を使う
    (daily_scan.py 経由の候補より原価の精度が高い上、卸より小売の方が安いケースも
    取りこぼさない。CEO: 「利益等は安い方で計算してください」)。
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from keepa_mcp import analysis
from keepa_mcp.cached_ops import cached_get_products, cached_lookup_by_code
from keepa_mcp.keepa_client import KeepaError, get_token_status
from keepa_mcp.server import enrich_qualified_candidates_with_seller_count
from netsea_client import BATCH_SIZE, NetseaError, get_items, get_suppliers
from ops_finance import (
    DEFAULT_WEIGHT_KG_FALLBACK,
    MIN_MARGIN_PCT,
    _classify_tier,
    calc_unit_profit,
    init_ops_tables,
    log_agent_run,
    new_agent_run_id,
    persist_agent_run,
)


def fetch_supplier_pool(netsea_token: str) -> List[Dict[str, Any]]:
    """取引可能なサプライヤー全件を取得する(実測156社、next_supplier_idで
    ページング)。呼び出しごとに毎回取得する - サプライヤー数が少なく変動も
    緩やかなため、今回のスコープでは専用キャッシュは設けない。"""
    suppliers: List[Dict[str, Any]] = []
    next_id: Optional[str] = None
    while True:
        page = get_suppliers(netsea_token, next_supplier_id=next_id)
        suppliers.extend(page.get("data", []))
        next_id = page.get("next_supplier_id")
        if not next_id:
            break
    return suppliers


def _extract_jan_codes(item: Dict[str, Any]) -> List[str]:
    """商品直下のjan_code、またはset(バリエーション)ごとのjan_codeから、
    空でないものだけを重複排除して集める。"""
    codes = set()
    top_jan = item.get("jan_code")
    if top_jan:
        codes.add(str(top_jan))
    for variant in item.get("set") or []:
        jan = variant.get("jan_code")
        if jan:
            codes.add(str(jan))
    return list(codes)


def _wait_for_one_token(api_key: str) -> None:
    """keepa_mcp/server.py の _wait_for_budget() と同じ考え方(残高0コストの
    /tokenをポーリングして待つ)を、JANコード1件=1トークンのこのパイプライン用に
    単純化したもの。wait_for_tokens=Trueのときだけ呼ばれる - トークン不足時に
    KeepaError(429)でスキップされてしまうと、実際にはマッチしていたはずの候補が
    「見つからなかった」ことになってしまうため、それを避ける。"""
    while True:
        status = get_token_status(api_key)
        if (status.get("tokens_left") or 0) >= 1:
            return
        refill_rate = status.get("refill_rate_per_minute") or 1
        wait_seconds = min(60, max(5, math.ceil(60 / refill_rate)))
        time.sleep(wait_seconds)


def _min_active_price_jpy(item: Dict[str, Any]) -> Optional[float]:
    """在庫あり(sold_out_flag='N')のバリエーションの中で最安値(税抜)を返す。"""
    prices = [
        v.get("price")
        for v in (item.get("set") or [])
        if v.get("sold_out_flag") == "N" and v.get("price") is not None
    ]
    return float(min(prices)) if prices else None


def find_netsea_items_for_categories(
    netsea_token: str,
    suppliers: List[Dict[str, Any]],
    category_ids: List[str],
    price_range_from: Optional[int] = None,
    price_range_to: Optional[int] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """カテゴリーIDごとに、全サプライヤーをBATCH_SIZE件ずつのバッチで
    /items に問い合わせ、結果をまとめて返す。"""
    supplier_ids = [str(s["id"]) for s in suppliers]
    results: Dict[str, List[Dict[str, Any]]] = {}

    for category_id in category_ids:
        items: List[Dict[str, Any]] = []
        for i in range(0, len(supplier_ids), BATCH_SIZE):
            batch = supplier_ids[i : i + BATCH_SIZE]
            try:
                page = get_items(
                    netsea_token, batch, category_id=category_id,
                    price_range_from=price_range_from, price_range_to=price_range_to,
                    sold_out_flag="N",
                )
            except NetseaError:
                continue  # 1バッチの失敗で全体を止めない(次のバッチへ)
            items.extend(page.get("data", []))
            time.sleep(0.15)  # NETSEA側への配慮(明示的なレート制限は不明のため保守的に)
        results[category_id] = items

    return results


def find_jan_matched_candidates(
    api_key: str,
    netsea_token: str,
    category_ids: List[str],
    category_labels: Optional[Dict[str, str]] = None,
    price_range_from: Optional[int] = None,
    price_range_to: Optional[int] = None,
    exchange_rate: float = 150.0,
    min_margin_pct: float = MIN_MARGIN_PCT,
    wait_for_tokens: bool = False,
) -> Dict[str, Any]:
    """メインのエントリポイント。指定カテゴリーのNETSEA商品のうちJANコード付きの
    ものだけを対象に、Amazon US側の完全一致ASINを探し、実質利益を計算する。

    Returns: {'qualified': [...], 'rejected': [...], 'category_items': int,
              'jan_candidates': int, 'matched': int}
    (qualified/rejectedの各要素はpersist_agent_run()にそのまま渡せる形)
    """
    category_labels = category_labels or {}
    suppliers = fetch_supplier_pool(netsea_token)
    items_by_category = find_netsea_items_for_categories(
        netsea_token, suppliers, category_ids, price_range_from, price_range_to,
    )

    # JANコード -> (NETSEA商品, 卸価格, カテゴリーラベル) のマップを作り、
    # 同じJANが複数サプライヤーから出ていた場合は最安値を採用する。
    jan_map: Dict[str, Dict[str, Any]] = {}
    total_items = 0
    for category_id, items in items_by_category.items():
        label = category_labels.get(category_id, category_id)
        for item in items:
            total_items += 1
            price_jpy = _min_active_price_jpy(item)
            if price_jpy is None:
                continue
            for jan in _extract_jan_codes(item):
                existing = jan_map.get(jan)
                if existing is None or price_jpy < existing["jp_cost_jpy"]:
                    jan_map[jan] = {
                        "jan": jan,
                        "jp_cost_jpy": price_jpy,
                        "netsea_product_name": item.get("product_name"),
                        "netsea_shop_name": item.get("shop_name"),
                        "netsea_supplier_id": item.get("supplier_id"),
                        "netsea_product_url": item.get("product_url"),
                        "category_label": label,
                    }

    qualified: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    matched = 0

    for jan, meta in jan_map.items():
        if wait_for_tokens:
            _wait_for_one_token(api_key)
        try:
            products, _cache_info = cached_lookup_by_code(
                api_key, jan, domain="US",
            )
        except KeepaError:
            if wait_for_tokens:
                _wait_for_one_token(api_key)
                try:
                    products, _cache_info = cached_lookup_by_code(api_key, jan, domain="US")
                except KeepaError:
                    continue
            else:
                continue
        if not products:
            continue
        product = products[0]  # 完全一致(バーコード)のため通常1件のみ
        asin = product.get("asin")
        if not asin:
            continue
        matched += 1

        us_price = analysis.current_price(product, "US")
        if us_price is None:
            continue

        weight_kg = analysis.package_weight_kg(product)
        weight_estimated = weight_kg is None
        if weight_estimated:
            weight_kg = DEFAULT_WEIGHT_KG_FALLBACK

        fee_kwargs: Dict[str, float] = {}
        fee_estimated = False
        referral_pct = analysis.referral_fee_percent(product)
        if referral_pct is not None:
            fee_kwargs["amazon_fee_rate"] = referral_pct / 100
        else:
            fee_estimated = True
        fba_fee = analysis.fba_pickpack_fee(product, "US")
        if fba_fee is not None:
            fee_kwargs["fba_fee_usd"] = fba_fee
        else:
            fee_estimated = True

        # CEO: 「卸売りの仕入れ価格がわかったら、Amazonとは別の列に価格を追加して
        # ください。利益等は安い方で計算してください。」— 同じJANでAmazon JP側にも
        # 出品があれば(卸で仕入れず小売で買った方が安いケースもあるため)、
        # NETSEAの卸価格と比べて安い方を実際の原価として使う。JANは既に厳密な
        # バーコード一致キーなので、追加のトークンを払ってでも一致確認する価値がある。
        wholesale_cost_jpy = meta["jp_cost_jpy"]
        jp_amazon_cost_jpy: Optional[float] = None
        if wait_for_tokens:
            _wait_for_one_token(api_key)
        try:
            jp_products, _cache_info = cached_lookup_by_code(api_key, jan, domain="JP")
        except KeepaError:
            jp_products = []
        if jp_products:
            jp_amazon_cost_jpy = analysis.current_price(jp_products[0], "JP")

        if jp_amazon_cost_jpy is not None:
            effective_cost_jpy = min(wholesale_cost_jpy, jp_amazon_cost_jpy)
        else:
            effective_cost_jpy = wholesale_cost_jpy

        profit = calc_unit_profit(
            us_price_usd=us_price,
            jp_cost_jpy=effective_cost_jpy,
            weight_kg=weight_kg,
            exchange_rate=exchange_rate,
            **fee_kwargs,
        )

        entry = {
            "asin": asin,
            "title": product.get("title") or meta["netsea_product_name"],
            "url": analysis.product_url(asin, "US"),
            "image_url": analysis.product_image_url(product),
            "jp_asin": None,  # NETSEA由来はJP側Amazon ASINと無関係(卸サイトのため)
            "jp_url": meta["netsea_product_url"],  # 詳細ページの「JP」リンクはNETSEA商品ページに流用
            "sales_rank": analysis.sales_rank(product),
            "review_count": analysis.review_count(product),
            "monthly_sold": analysis.monthly_sold(product),
            "sales_rank_drops_30": analysis.sales_rank_drops_30(product),
            "price_diff_rate_gross": None,
            "price_volatility_90d": analysis.price_volatility_ratio(product, "US"),
            "weight_kg": weight_kg,
            "weight_estimated": weight_estimated,
            "fee_estimated": fee_estimated,
            "brand": product.get("brand"),
            "jp_brand": None,
            "rating": analysis.rating(product),
            "jp_rating": None,
            "upc": (product.get("upcList") or [None])[0],
            "ean": jan,
            "netsea_jan": jan,
            "netsea_shop_name": meta["netsea_shop_name"],
            "netsea_product_url": meta["netsea_product_url"],
            **profit,
            "jp_cost_jpy": effective_cost_jpy,  # 実際の利益計算に使った原価(安い方)
            "wholesale_cost_jpy": wholesale_cost_jpy,
            "jp_amazon_cost_jpy": jp_amazon_cost_jpy,
        }
        entry["tier"] = _classify_tier(
            profit["margin_pct"], profit["us_price_usd"], profit["jp_cost_usd"], min_margin_pct,
        )
        entry["category"] = f"NETSEA卸仕入れ({meta['category_label']})"

        if entry["tier"] == "pass":
            qualified.append(entry)
        else:
            entry["reason"] = f"実質利益率 {profit['margin_pct']:.1%} が閾値 {min_margin_pct:.0%} 未満"
            rejected.append(entry)

    qualified.sort(key=lambda e: e["margin_pct"], reverse=True)

    return {
        "qualified": qualified,
        "rejected": rejected,
        "category_items": total_items,
        "jan_candidates": len(jan_map),
        "matched": matched,
    }


def run_netsea_sourcing_cycle(
    api_key: str,
    netsea_token: str,
    category_ids: List[str],
    category_labels: Optional[Dict[str, str]] = None,
    price_range_from: Optional[int] = None,
    price_range_to: Optional[int] = None,
    wait_for_tokens: bool = True,
) -> Dict[str, Any]:
    """1サイクル実行 -> 保存 -> 結果サマリーを返す(scripts/netsea_sourcing_cli.py用)。
    wait_for_tokens既定True: daily_scan.pyと同じく、CLIからのバッチ実行は
    ブロックしてでも正確な結果を得る方を優先する(ダッシュボードからの
    即時応答が要るseller_mine_cli.pyとは事情が異なる)。"""
    init_ops_tables()
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = new_agent_run_id()
    label = "NETSEA卸仕入れ(JAN完全一致)"

    result = find_jan_matched_candidates(
        api_key, netsea_token, category_ids, category_labels,
        price_range_from=price_range_from, price_range_to=price_range_to,
        wait_for_tokens=wait_for_tokens,
    )

    # CEO: 「仕入れ先とセラーは別にして欲しい」— seller_name/seller_idは
    # Amazon側の競合出品者(セラーマイニング由来)を指す既存の意味のまま残し、
    # NETSEAの卸売り業者名は混同させない(誤ってseller_nameに仕入れ先名を
    # 入れていた版から差し戻し)。仕入れ先の表示はentry内の
    # netsea_shop_name/netsea_product_url(data_json経由)をフロントエンド側で
    # 専用の「仕入れ先」欄として別に表示する形にする。
    #
    # persist_agent_run()はcategoryを候補ごとではなく呼び出し単位で一律適用
    # するため、カテゴリーごとに分けて保存する。
    by_category: Dict[str, Dict[str, list]] = {}
    for qualified_flag, key in ((1, "qualified"), (0, "rejected")):
        for item in result[key]:
            cat = item.get("category", label)
            by_category.setdefault(cat, {"qualified": [], "rejected": []})
            by_category[cat]["qualified" if qualified_flag else "rejected"].append(item)

    for cat, evaluation in by_category.items():
        if evaluation["qualified"]:
            enrich_qualified_candidates_with_seller_count(
                evaluation["qualified"], wait_for_tokens=wait_for_tokens,
            )
        if evaluation["qualified"] or evaluation["rejected"]:
            persist_agent_run(cat, evaluation, run_id=run_id, source_type="netsea")

    log_agent_run(
        run_id, started_at, 0,
        keyword="[netsea] " + ",".join(category_ids), category=label,
        max_candidates=result["jan_candidates"],
        mcp_result={"category_items": result["category_items"], "jan_candidates": result["jan_candidates"], "matched": result["matched"]},
        evaluation={"qualified": result["qualified"], "rejected": result["rejected"]},
        notify_status="deferred_to_digest",
        source_type="netsea",
    )

    return {
        "runId": run_id,
        "categoryItems": result["category_items"],
        "janCandidates": result["jan_candidates"],
        "matched": result["matched"],
        "qualifiedCount": len(result["qualified"]),
        "rejectedCount": len(result["rejected"]),
    }
