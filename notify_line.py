"""LINE通知(LINE Messaging API経由)。

旧 LINE Notify (send_line_notify) は2025年3月末でサービス終了済み
(notify-api.line.me は名前解決すらできない)ため、後継の LINE Messaging API
を使う。LINE公式アカウントの「チャネルアクセストークン(長期)」が必要
(LINE Developers Console > 対象チャンネル(Messaging API) >
「Messaging API設定」タブから発行)。

送信先の指定方法は2通り:
  - user_id を指定 -> 特定の1人にpush送信(/v2/bot/message/push)
    ※ user_idはLINEの「表示名」や「LINE ID(検索用ID)」ではなく、
      Messaging APIが内部的に使う「U」で始まる32桁の16進数文字列。
      LINE Developers Console単体では自分のuser_idを直接確認できず、
      Webhookで受信したイベントのsource.userIdから拾う必要がある。
  - user_id を指定しない -> 友だち全員にbroadcast送信(/v2/bot/message/broadcast)
    個人利用のBot(友だちが自分だけ)であれば、pushと実質的に同じ効果になり、
    user_idを調べる手間が要らないためこちらがデフォルト。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Dict, List, Optional

API_BASE = "https://api.line.me/v2/bot/message"

# LINEのテキストメッセージは1通あたり最大5000文字。
_MAX_TEXT_LENGTH = 5000


class LineNotifyError(RuntimeError):
    """LINE Messaging APIがエラーを返した場合に送出する。"""


def _post(path: str, channel_access_token: str, payload: Dict) -> None:
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {channel_access_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()  # 成功時のボディは空
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise LineNotifyError(f"LINE Messaging API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LineNotifyError(f"LINE Messaging API request failed: {exc.reason}") from exc


def send_line_message(
    channel_access_token: str,
    message: str,
    user_id: Optional[str] = None,
) -> None:
    """LINEにテキストメッセージを送る。user_idがあればpush、無ければbroadcast。"""
    if not channel_access_token:
        raise LineNotifyError("LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

    text = message[:_MAX_TEXT_LENGTH]
    messages: List[Dict] = [{"type": "text", "text": text}]

    if user_id:
        _post("/push", channel_access_token, {"to": user_id, "messages": messages})
    else:
        _post("/broadcast", channel_access_token, {"messages": messages})


def build_line_message(results: List[Dict]) -> str:
    """notify_email.pyのbuild_email_body()相当。CSVスキャン結果向け。"""
    if not results:
        return "該当する商品は見つかりませんでした。"

    lines = ["Amazon FBA 市場差額レポート", ""]
    for item in results:
        lines.append(f"ASIN: {item['asin']}")
        lines.append(f"Title: {item['title']}")
        lines.append(f"US Price: ${item['us_price']:.2f}")
        lines.append(f"JP Price: ¥{item['jp_price']:.0f}")
        lines.append(f"差額率: {item['price_diff_percent'] * 100:.1f}%")
        lines.append(f"US URL: {item['us_url']}")
        lines.append("-")

    return "\n".join(lines)
