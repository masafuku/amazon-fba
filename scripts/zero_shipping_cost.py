#!/usr/bin/env python3
"""
zero_shipping_cost.py — agent_candidatesの既存行から国際送料(shipping_cost_usd)を
0にして、利益・利益率・ROI・tier・qualifiedを再計算する一度きりのスクリプト
(手動実行専用)。

HISTORICAL — ローカル・AWS本番の両方に2026年に適用済み。2026-09-28、tier
(pass/consider/reference/reject)・_classify_tier()・MIN_MARGIN_PCT/MIN_ROI_PCTが
priority_tierへの統合で削除されたため、このスクリプトはもう実行できない
(依存していたops_finance側のシンボルが存在しない)。再実行が必要になった場合は、
_classify_tierの代わりにis_qualified_priority_tier()/_priority_rejection_reason()
とops_finance._classify_priority_tier()を使って書き直すこと。
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "[INFO] このスクリプトは既に2026年に適用済みの一度きりの移行スクリプトです。\n"
        "[INFO] 2026-09-28のtier廃止(priority_tierへの統合)により、依存していた\n"
        "[INFO] _classify_tier/MIN_MARGIN_PCT/MIN_ROI_PCTがops_finance.pyから\n"
        "[INFO] 削除されたため、このスクリプトはもう実行できません。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
