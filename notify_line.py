import json
import urllib.parse
import urllib.request
from typing import List, Dict

LINE_NOTIFY_URL = "https://notify-api.line.me/api/notify"


def build_line_message(results: List[Dict]) -> str:
    if not results:
        return "Amazon FBA レポート: 差額30%以上の商品は見つかりませんでした。"

    lines = ["Amazon FBA 価格差レポート"]
    for item in results[:5]:
        lines.append(f"ASIN: {item['asin']}")
        lines.append(f"{item['title']}")
        lines.append(f"US: ${item['us_price']:.2f} (¥{item['us_price_jpy']:.0f})")
        lines.append(f"JP: ¥{item['jp_price']:.0f}")
        lines.append(f"差額: {item['price_diff_percent']*100:.1f}%")
        lines.append("---")

    if len(results) > 5:
        lines.append(f"他 {len(results) - 5} 件の結果があります。")

    return "\n".join(lines)


def send_line_notify(token: str, message: str) -> None:
    data = urllib.parse.urlencode({"message": message}).encode("utf-8")
    request = urllib.request.Request(
        LINE_NOTIFY_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {token}",
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="ignore")
        result = json.loads(body)
        if result.get("status") != 200:
            raise RuntimeError(f"LINE notify failed: {body}")
