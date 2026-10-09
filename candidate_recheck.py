#!/usr/bin/env python3
"""発注前の最終確認: S/A+候補のROIとTierを、Amazonのバイボックス価格と実額の手数料で出し直す。

候補のTier(agent_candidates.priority_tier)はKeepaの価格と既定/Keepaの手数料で付いている。
ここでは Tierの計算(ops_finance.calc_unit_profit / _classify_priority_tier)は変えず、価格と手数料の
入力だけを SP-API の値に差し替える(送料ゼロ・関税12.5%など他の前提は自動判定と同じ)。
結果は candidate_rechecks に保存するだけで、agent_candidates のTierは書き換えない。
APIの上限(毎秒0.5回)のため170件で約10分かかる。--max-age-hours 内に確認済みのASINは飛ばす(途中再開用)。

使い方(AWSで実行):
    candidate_recheck.py                  # S/A+を確認(nohup推奨)
    candidate_recheck.py --report         # 保存済みの結果でTierが変わったものを表示
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import ops_finance as of
from sp_api import client

logger = logging.getLogger("candidate_recheck")

DEFAULT_TIERS = ("S", "A+")


def init_table() -> None:
    of.init_ops_tables()
    with sqlite3.connect(of.DB_PATH) as conn:
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS candidate_rechecks (
                asin TEXT PRIMARY KEY,
                checked_at TEXT,
                status TEXT,                -- ok / no_buy_box / error
                title TEXT,
                tier_before TEXT,
                tier_after TEXT,
                roi_before REAL,
                roi_after REAL,
                price_before_usd REAL,
                buy_box_usd REAL,
                offer_count INTEGER,
                referral_fee_usd REAL,
                fba_fee_usd REAL
            )'''
        )


def load_candidates(tiers=DEFAULT_TIERS) -> list:
    """ASINごとの最新の評価行のうち、Tierが指定のもの。"""
    init_table()
    marks = ",".join("?" * len(tiers))
    with sqlite3.connect(of.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f'''SELECT asin, title, us_price_usd, jp_cost_jpy, weight_kg, monthly_sold, priority_tier, data_json
                FROM agent_candidates a
                WHERE id = (SELECT MAX(id) FROM agent_candidates WHERE asin = a.asin)
                  AND priority_tier IN ({marks}) AND excluded_kind IS NULL
                ORDER BY CASE priority_tier WHEN 'S' THEN 0 ELSE 1 END, asin''', tuple(tiers)).fetchall()
    candidates = []
    for r in rows:
        candidate = dict(r)
        try:
            detail = json.loads(candidate["data_json"] or "{}")
        except ValueError:
            detail = {}
        # monthly_sold列が空でも詳細データ(data_json)に値があることがある。Tierの判定には両方を見る。
        if candidate["monthly_sold"] is None:
            candidate["monthly_sold"] = detail.get("monthly_sold")
        candidate["sales_rank_drops_30"] = detail.get("sales_rank_drops_30")
        candidates.append(candidate)
    return candidates


def recompute(candidate: dict, buy_box: float, fees: dict) -> dict:
    """自動判定と同じ計算式で、価格をバイボックス・手数料を実額にして利益・ROI・Tierを出す。"""
    result = of.calc_unit_profit(
        us_price_usd=buy_box,
        jp_cost_jpy=candidate["jp_cost_jpy"],
        weight_kg=candidate["weight_kg"] or 0.0,
        amazon_fee_rate=fees["referral"] / buy_box,
        fba_fee_usd=fees["fba"] + (fees.get("other") or 0.0),
    )
    tier = of._classify_priority_tier(result["roi_pct"], candidate["monthly_sold"], candidate.get("sales_rank_drops_30"))
    return {"roi": result["roi_pct"], "tier": tier}


def roi_before(candidate: dict) -> float | None:
    try:
        return json.loads(candidate["data_json"]).get("roi_pct")
    except (TypeError, ValueError):
        return None


def recheck(tiers=DEFAULT_TIERS, max_age_hours: float = 20.0, limit: int | None = None) -> dict:
    init_table()
    fresh_after = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    with sqlite3.connect(of.DB_PATH) as conn:
        done = {a for (a,) in conn.execute("SELECT asin FROM candidate_rechecks WHERE checked_at > ? AND status != 'error'", (fresh_after,))}
    todo = [c for c in load_candidates(tiers) if c["asin"] not in done]
    if limit:
        todo = todo[:limit]
    counts = {"ok": 0, "no_buy_box": 0, "error": 0}
    for candidate in todo:
        asin = candidate["asin"]
        row = {"asin": asin, "title": candidate["title"], "tier_before": candidate["priority_tier"],
               "roi_before": roi_before(candidate), "price_before_usd": candidate["us_price_usd"],
               "tier_after": None, "roi_after": None, "buy_box_usd": None, "offer_count": None,
               "referral_fee_usd": None, "fba_fee_usd": None}
        try:
            time.sleep(2.2)
            offers = client.get_offers_summary(asin)
            row["offer_count"] = offers["offers"]
            if offers["buy_box"] is None:
                row["status"] = "no_buy_box"
            else:
                time.sleep(1.1)
                fees = client.get_fees_estimate(asin, offers["buy_box"])
                new = recompute(candidate, offers["buy_box"], fees)
                row.update(status="ok", buy_box_usd=offers["buy_box"], tier_after=new["tier"], roi_after=new["roi"],
                           referral_fee_usd=fees["referral"], fba_fee_usd=fees["fba"] + (fees.get("other") or 0.0))
        except client.SpApiError as exc:
            logger.warning(f"{asin}: 取得失敗: {str(exc)[:100]}")
            row["status"] = "error"
        counts[row["status"]] += 1
        with sqlite3.connect(of.DB_PATH) as conn:
            conn.execute(
                '''INSERT INTO candidate_rechecks (asin, checked_at, status, title, tier_before, tier_after, roi_before, roi_after,
                       price_before_usd, buy_box_usd, offer_count, referral_fee_usd, fba_fee_usd)
                   VALUES (:asin, :checked_at, :status, :title, :tier_before, :tier_after, :roi_before, :roi_after,
                       :price_before_usd, :buy_box_usd, :offer_count, :referral_fee_usd, :fba_fee_usd)
                   ON CONFLICT(asin) DO UPDATE SET checked_at=excluded.checked_at, status=excluded.status, title=excluded.title,
                       tier_before=excluded.tier_before, tier_after=excluded.tier_after, roi_before=excluded.roi_before,
                       roi_after=excluded.roi_after, price_before_usd=excluded.price_before_usd, buy_box_usd=excluded.buy_box_usd,
                       offer_count=excluded.offer_count, referral_fee_usd=excluded.referral_fee_usd, fba_fee_usd=excluded.fba_fee_usd''',
                {**row, "checked_at": datetime.now(timezone.utc).isoformat()})
    return {**counts, "skipped_recent": len(done), "total_candidates": len(todo) + len(done)}


def reclassify_saved() -> int:
    """保存済みのバイボックス・手数料から、APIを呼ばずにROIとTierだけ計算し直す。"""
    init_table()
    by_asin = {c["asin"]: c for c in load_candidates()}
    changed = 0
    with sqlite3.connect(of.DB_PATH) as conn:
        for asin, bb, referral, fba in conn.execute(
                "SELECT asin, buy_box_usd, referral_fee_usd, fba_fee_usd FROM candidate_rechecks WHERE status = 'ok'").fetchall():
            candidate = by_asin.get(asin)
            if not candidate:
                continue
            new = recompute(candidate, bb, {"referral": referral, "fba": fba, "other": 0.0})
            conn.execute("UPDATE candidate_rechecks SET tier_after = ?, roi_after = ? WHERE asin = ?", (new["tier"], new["roi"], asin))
            changed += 1
    return changed


def report() -> str:
    init_table()
    with sqlite3.connect(of.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM candidate_rechecks ORDER BY tier_before, asin")]
    order = {t: i for i, t in enumerate(of.PRIORITY_TIER_ORDER)}
    ok = [r for r in rows if r["status"] == "ok"]
    changed = [r for r in ok if r["tier_after"] != r["tier_before"]]
    down = sorted([r for r in changed if order[r["tier_after"]] > order[r["tier_before"]]], key=lambda r: -order[r["tier_after"]])
    up = [r for r in changed if order[r["tier_after"]] < order[r["tier_before"]]]
    lines = [f"確認済み{len(ok)}件 / バイボックスなし{sum(r['status'] == 'no_buy_box' for r in rows)}件 / 取得失敗{sum(r['status'] == 'error' for r in rows)}件",
             f"Tierが変わった: {len(changed)}件（下がる{len(down)}・上がる{len(up)}）"]

    def line(r):
        title = (r["title"] or "")[:40]
        return (f"  {r['asin']} {r['tier_before']}→{r['tier_after']} 価格${r['price_before_usd']:.2f}→BB${r['buy_box_usd']:.2f} "
                f"ROI{(r['roi_before'] or 0) * 100:.0f}%→{r['roi_after'] * 100:.0f}% 出品{r['offer_count']} {title}")

    for label, group in (("■下がった", down), ("■上がった", up)):
        if group:
            lines.append(label)
            lines += [line(r) for r in group[:30]]
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="S/A+候補をバイボックスと実額の手数料で再確認する(Tierは書き換えない)")
    parser.add_argument("--report", action="store_true", help="取得せず、保存済みの結果を表示する")
    parser.add_argument("--reclassify", action="store_true", help="APIを呼ばず、保存済みのバイボックス・手数料でTierだけ計算し直す")
    parser.add_argument("--tiers", nargs="+", default=list(DEFAULT_TIERS))
    parser.add_argument("--max-age-hours", type=float, default=20.0)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.reclassify:
        logger.info(f"再計算: {reclassify_saved()}件")
    elif not args.report:
        logger.info(f"再確認: {recheck(tuple(args.tiers), args.max_age_hours, args.limit)}")
    print(report())


if __name__ == "__main__":
    main()
