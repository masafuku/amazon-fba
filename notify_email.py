import smtplib
from email.message import EmailMessage
from typing import List, Dict


def send_email(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to_addr
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(message)


def build_email_body(results: List[Dict]) -> str:
    if not results:
        return "該当する商品は見つかりませんでした。"

    lines = [
        "Amazon FBA 市場差額レポート",
        "",
    ]
    for item in results:
        lines.append(f"ASIN: {item['asin']}")
        lines.append(f"Title: {item['title']}")
        lines.append(f"US Price: ${item['us_price']:.2f}")
        lines.append(f"JP Price: ¥{item['jp_price']:.0f}")
        lines.append(f"差額率: {item['price_diff_percent'] * 100:.1f}%")
        lines.append(f"US URL: {item['us_url']}")
        lines.append("-")

    return "\n".join(lines)
