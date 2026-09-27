#!/usr/bin/env python3
"""
zero_shipping_cost.py — agent_candidatesの既存行から国際送料(shipping_cost_usd)を
0にして、利益・利益率・ROI・tier・qualifiedを再計算する一度きりのスクリプト
(手動実行専用)。

CEO: 「輸送費の想定があまり妥当でないので輸送費は0に変更してください」
calc_unit_profit()側は0に変更済み。既存行は保存済みの値から算術だけで直せる
(Keepa再取得は不要)。ローカルDBとAWS本番DBは別ファイルなので、それぞれで実行する。

使い方:
    python3 scripts/zero_shipping_cost.py --dry-run   # 変更内容だけ確認
    python3 scripts/zero_shipping_cost.py             # 実際に上書きする
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops_finance import (
    DB_PATH, MIN_MARGIN_PCT, MIN_ROI_PCT, TIER_PASS, _classify_tier,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id, data_json, tier, qualified, reason FROM agent_candidates").fetchall()

        updates, tier_moves = [], Counter()
        for row_id, data_json, old_tier, old_qualified, old_reason in rows:
            data = json.loads(data_json)
            shipping = data.get("shipping_cost_usd")
            if not shipping:
                continue
            profit = data.get("unit_profit_usd")
            us_price = data.get("us_price_usd")
            jp_cost_usd = data.get("jp_cost_usd")
            if profit is None or not us_price:
                continue

            new_profit = round(profit + shipping, 2)
            new_margin = round(new_profit / us_price, 4)
            new_roi = round(new_profit / jp_cost_usd, 4) if jp_cost_usd else 0.0
            new_tier = _classify_tier(
                new_margin, new_roi, us_price, jp_cost_usd,
                demand_signal=data.get("demand_signal"),
            )
            new_qualified = 1 if new_tier == TIER_PASS else 0
            new_reason = None if new_qualified else (
                f"実質利益率 {new_margin:.1%}(閾値{MIN_MARGIN_PCT:.0%}) / "
                f"ROI {new_roi:.0%}(閾値{MIN_ROI_PCT:.0%}) が基準未満"
            )

            data.update({
                "shipping_cost_usd": 0.0, "unit_profit_usd": new_profit,
                "margin_pct": new_margin, "roi_pct": new_roi, "tier": new_tier,
            })
            if new_reason:
                data["reason"] = new_reason
            else:
                data.pop("reason", None)
            updates.append((new_profit, new_margin, new_qualified, new_tier, new_reason,
                            json.dumps(data, ensure_ascii=False), row_id))
            tier_moves[(old_tier, new_tier)] += 1

        print(f"対象行: {len(updates)}件 / 全{len(rows)}件 {'(dry-run: 書き換えません)' if args.dry_run else ''}")
        for (old, new), n in sorted(tier_moves.items(), key=lambda kv: str(kv[0])):
            print(f"  tier {old} -> {new}: {n}件")

        if args.dry_run or not updates:
            return

        backup = DB_PATH.with_name(f"{DB_PATH.name}.bak-{datetime.now():%Y%m%d_%H%M%S}")
        shutil.copy(DB_PATH, backup)
        print(f"バックアップ: {backup}")
        conn.executemany(
            "UPDATE agent_candidates SET unit_profit_usd=?, margin_pct=?, qualified=?, tier=?, reason=?, data_json=? WHERE id=?",
            updates,
        )
    print("更新しました。")


if __name__ == "__main__":
    main()
