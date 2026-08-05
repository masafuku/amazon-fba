import argparse
from typing import List, Dict

from config import Settings
from fetch_keepa import fetch_keepa_products, map_keepa_product
from jp_amazon import search_jp_by_asin
from notify_email import send_email, build_email_body
from notify_line import send_line_notify, build_line_message
from us_amazon import search_us_asins


def find_price_gap_products(settings: Settings) -> List[Dict]:
    us_items = search_us_asins(
        keyword=settings.search_keyword,
        category=settings.search_category,
        max_results=settings.us_max_results,
    )

    if not us_items:
        return []

    asins = [item["asin"] for item in us_items if item.get("asin")]
    keepa_products = fetch_keepa_products(settings.keepa_api_key, asins)

    matches: List[Dict] = []
    keepa_lookup = {product.get("asin"): map_keepa_product(product) for product in keepa_products}

    for item in us_items:
        asin = item.get("asin")
        if not asin or asin not in keepa_lookup:
            continue

        mapped = keepa_lookup[asin]
        us_price = mapped.get("us_price")
        if us_price is None or us_price <= 0:
            continue

        sales_rank = mapped.get("sales_rank")
        if sales_rank is not None and sales_rank > settings.sales_rank_threshold:
            continue

        jp_price = search_jp_by_asin(asin)
        if jp_price is None or jp_price <= 0:
            continue

        jp_price_yen = jp_price
        us_price_jpy = us_price * settings.usd_to_jpy
        price_diff = jp_price_yen - us_price_jpy
        price_diff_percent = price_diff / us_price_jpy

        if price_diff_percent >= settings.price_diff_threshold:
            matches.append(
                {
                    "asin": asin,
                    "title": mapped.get("title") or item.get("title"),
                    "us_price": us_price,
                    "us_price_jpy": us_price_jpy,
                    "jp_price": jp_price_yen,
                    "price_diff_percent": price_diff_percent,
                    "sales_rank": sales_rank,
                    "us_url": mapped.get("product_url"),
                    "jp_url": f"https://www.amazon.co.jp/dp/{asin}",
                }
            )

    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description="Amazon FBA price gap scanner")
    parser.add_argument("--notify-email", action="store_true", help="Send email notification")
    parser.add_argument("--notify-line", action="store_true", help="Send LINE notification")
    args = parser.parse_args()

    settings = Settings.load()
    results = find_price_gap_products(settings)

    if results:
        for item in results:
            print(
                f"{item['asin']} {item['title']} US=${item['us_price']:.2f} (¥{item['us_price_jpy']:.0f}) "
                f"JP=¥{item['jp_price']:.0f} △{item['price_diff_percent']*100:.1f}%"
            )
    else:
        print("差額30%以上の商品は見つかりませんでした。")

    if args.notify_email:
        subject = "Amazon FBA 価格差レポート"
        body = build_email_body(results)
        send_email(
            smtp_host=settings.email_smtp_host,
            smtp_port=settings.email_smtp_port,
            username=settings.email_username,
            password=settings.email_password,
            from_addr=settings.email_from,
            to_addr=settings.email_to,
            subject=subject,
            body=body,
        )
        print("メール通知を送信しました。")

    if args.notify_line:
        if not settings.line_notify_token:
            raise ValueError("LINE_NOTIFY_TOKEN is required for LINE notification.")

        line_message = build_line_message(results)
        send_line_notify(settings.line_notify_token, line_message)
        print("LINE通知を送信しました。")


if __name__ == "__main__":
    main()
