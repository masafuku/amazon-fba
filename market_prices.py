#!/usr/bin/env python3
"""出品中の全ASINのバイボックス価格・FBA最安値・出品数と、その価格での手数料(実額)を
Amazon(SP-API)から取って market_prices に保存し、ROIを計算する。

読み取りのみ。価格や最低価格は一切変更しない(CEO: 「価格は自動では動かしません」)。
sp_api_sync.py(AWSのcron、3時間ごと)から呼ばれる。手動実行: `market_prices.py [--show]`。
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timezone

import ops_finance as of
import pricing_rule as pr
from sp_api import client

logger = logging.getLogger("market_prices")

# 相場でのROIがこれを下回ったら要対応に出す(Tier合格ラインと同じ)
ROI_ALERT_THRESHOLD = pr.MIN_ROI


def refresh(asins: list | None = None) -> dict:
    """相場を取り直す。asins省略時は出品中の全ASIN。取得に失敗したASINは前回の値を残す。"""
    of.init_ops_tables()
    asins = asins if asins is not None else sorted({l["asin"] for l in pr.list_listings() if l["asin"]})
    ok, failed = 0, []
    for asin in asins:
        try:
            time.sleep(2.2)
            offers = client.get_offers_summary(asin)
            fees = None
            if offers["buy_box"] is not None:
                time.sleep(1.1)
                fees = client.get_fees_estimate(asin, offers["buy_box"])
        except client.SpApiError as exc:
            logger.warning(f"{asin}: 相場の取得に失敗(前回の値を残します): {str(exc)[:120]}")
            failed.append(asin)
            continue
        with sqlite3.connect(of.DB_PATH) as conn:
            conn.execute(
                '''INSERT INTO market_prices (asin, buy_box_usd, lowest_fba_usd, offer_count, referral_fee_usd, fba_fee_usd, other_fee_usd, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(asin) DO UPDATE SET buy_box_usd = excluded.buy_box_usd, lowest_fba_usd = excluded.lowest_fba_usd,
                       offer_count = excluded.offer_count, referral_fee_usd = excluded.referral_fee_usd,
                       fba_fee_usd = excluded.fba_fee_usd, other_fee_usd = excluded.other_fee_usd, fetched_at = excluded.fetched_at''',
                (asin, offers["buy_box"], offers["lowest_fba"], offers["offers"],
                 fees["referral"] if fees else None, fees["fba"] if fees else None, fees["other"] if fees else None,
                 datetime.now(timezone.utc).isoformat()),
            )
        ok += 1
    return {"updated": ok, "failed": failed}


def roi_rows() -> list:
    """保存済みの相場から、バイボックスで売れた場合の1個あたり利益とROIを出す。
    原価は仕入れ金額(税抜)の平均。物流費・関税は pricing_rule の定数。"""
    of.init_ops_tables()
    with sqlite3.connect(of.DB_PATH) as conn:
        costs = {a: v for a, v in conn.execute(
            'SELECT asin, SUM(IFNULL(amount_jpy, 0)) / SUM(quantity) FROM jp_purchase_records '
            'WHERE asin IS NOT NULL AND quantity > 0 GROUP BY asin')}
        names = {a: n for a, n in conn.execute('SELECT asin, sku FROM sp_fba_inventory')}
        market = conn.execute(
            'SELECT asin, buy_box_usd, lowest_fba_usd, offer_count, referral_fee_usd, fba_fee_usd, other_fee_usd, fetched_at FROM market_prices'
        ).fetchall()
    rows = []
    for asin, bb, low, offers, referral, fba, other, fetched_at in market:
        cost_jpy = costs.get(asin)
        row = {"asin": asin, "name": names.get(asin), "buy_box": bb, "lowest_fba": low, "offers": offers, "fetched_at": fetched_at,
               "cost_usd": None, "profit": None, "roi": None}
        if bb is not None and cost_jpy and referral is not None:
            cost_usd = cost_jpy / pr.EXCHANGE_RATE
            profit = bb - referral - fba - (other or 0.0) - pr.INTL_SHIPPING_USD - cost_usd * pr.IMPORT_DUTY_RATE - cost_usd
            row.update(cost_usd=round(cost_usd, 2), profit=round(profit, 2), roi=round(profit / cost_usd, 3))
        rows.append(row)
    return sorted(rows, key=lambda r: (r["roi"] is None, r["roi"] if r["roi"] is not None else 0))


def roi_alerts() -> list:
    return [
        f'ROI低下: {r["name"] or r["asin"]} 相場${r["buy_box"]:.2f}で利益${r["profit"]:.2f} ROI{r["roi"] * 100:.0f}%（{ROI_ALERT_THRESHOLD * 100:.0f}%未満）'
        for r in roi_rows() if r["roi"] is not None and r["roi"] < ROI_ALERT_THRESHOLD
    ]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="相場(バイボックス・手数料)を取得して保存する(価格は動かさない)")
    parser.add_argument("--show", action="store_true", help="取得せず、保存済みの相場とROIを表示する")
    args = parser.parse_args()
    if not args.show:
        logger.info(f"相場取得: {refresh()}")
    for r in roi_rows():
        roi = f'{r["roi"] * 100:.0f}%' if r["roi"] is not None else "-"
        bb = f'${r["buy_box"]:.2f}' if r["buy_box"] is not None else "-"
        print(f'{r["asin"]} {r["name"] or "":<14} BB{bb:>8} 出品{r["offers"]:>3} ROI{roi:>6} ({r["fetched_at"][:16]})')


if __name__ == "__main__":
    main()
