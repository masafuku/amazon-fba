#!/usr/bin/env python3
"""
check_listing_restrictions.py — ASINごとに、このAmazonアカウント(SP-API)が新品で出品できるかを
照会するだけのスクリプト(出品は一切行わない)。

CEO: 「Holbein や muji は商品登録ができない可能性があります。登録まで自動で試す方法を
提案できますか？」→ 第1段階として、Listings Restrictions API の照会を試す。
結果の見方: 出品可 / 承認が必要(申請リンクつき) / 出品不可(理由つき) / 照会失敗。

使い方:
    .venv/bin/python scripts/check_listing_restrictions.py B004O7GLL2 B014QK9ENS
    .venv/bin/python scripts/check_listing_restrictions.py --condition used_good B004O7GLL2

事前設定(.env): LWA_CLIENT_ID / LWA_CLIENT_SECRET / SP_API_REFRESH_TOKEN / SP_API_SELLER_ID
(SP-APIアプリに「Product Listing」ロールが必要)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sp_api import client  # noqa: E402
from sp_api.config import settings  # noqa: E402
from sp_api.restrictions import STATUS_LABELS, summarize_restrictions  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("asins", nargs="+", help="照会するASIN")
    parser.add_argument("--condition", default="new_new", help="状態(既定: new_new = 新品)")
    args = parser.parse_args()

    if not settings.configured:
        print("[ERROR] SP-APIの認証情報(LWA_CLIENT_ID / LWA_CLIENT_SECRET / SP_API_REFRESH_TOKEN)が未設定です。")
        return 2
    if not settings.seller_id:
        print("[ERROR] 販売者ID(SP_API_SELLER_ID)が未設定です。Seller Central の「設定 > アカウント情報」で確認して .env に設定してください。")
        return 2

    exit_code = 0
    for asin in args.asins:
        try:
            payload = client.get_listings_restrictions(asin, condition_type=args.condition)
        except client.SpApiError as exc:
            print(f"{asin}: 照会失敗 - {exc}")
            exit_code = 1
            continue
        status, details = summarize_restrictions(payload)
        print(f"{asin}: {STATUS_LABELS[status]}")
        for line in details:
            print(f"  {line}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
