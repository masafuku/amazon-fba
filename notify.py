"""通知の送り先を1か所で選ぶ。.env に DISCORD_WEBHOOK_URL があればDiscord、無ければLINE。

LINEの無料枠(月200通・友だち2人へbroadcastで1回=2通)を11/16に使い切ったため、
Discordを試す(CEO 2026-11-18)。Discordは通数の制限がない。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import List

DISCORD_MAX_CHARS = 2000


class NotifyError(RuntimeError):
    pass


def channel() -> str:
    import config  # noqa: F401  .env を読み込む(load_dotenv)
    return "discord" if os.getenv("DISCORD_WEBHOOK_URL", "").strip() else "line"


def split_for_discord(text: str, limit: int = DISCORD_MAX_CHARS) -> List[str]:
    """2000文字までに、行の切れ目で分ける(1行が長すぎるときだけ途中で切る)。"""
    chunks, current = [], ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks


def send_discord(webhook_url: str, texts: List[str]) -> None:
    for text in texts:
        for chunk in split_for_discord(text):
            body = json.dumps({"content": chunk}).encode("utf-8")
            request = urllib.request.Request(
                webhook_url, data=body, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "fba-notify (urllib, 1.0)"},
            )
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=30):
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code == 429 and attempt < 2:  # 短い間隔の制限。少し待って送り直す
                        time.sleep(2)
                        continue
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                    raise NotifyError(f"Discord HTTP {exc.code}: {detail}") from exc
                except urllib.error.URLError as exc:
                    raise NotifyError(f"Discord request failed: {exc.reason}") from exc
            time.sleep(0.5)


def send(message: str | List[str], also_line: bool = False) -> str:
    """送って、送り先の名前を返す。DISCORD_WEBHOOK_URL があればDiscord、無ければLINE。
    also_line=True(朝・昼・夜のまとめ。CEO 2026-11-18「LINEは7時、14時、21時のまま」)は、Discordに加えてLINEにも送る。
    LINEが失敗しても(月の上限など)Discordに届いていれば成功として扱い、警告だけ出す。"""
    import config  # noqa: F401  .env を読み込む(load_dotenv)
    texts = [message] if isinstance(message, str) else [m for m in message if m]
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return _send_line(texts)
    send_discord(url, texts)
    if not also_line:
        return "Discord"
    try:
        return "Discord + " + _send_line(texts)
    except Exception as exc:
        print(f"[WARN] LINEには送れませんでした(Discordには送信済み): {exc}")
        return "Discord (LINEは失敗)"


def _send_line(texts: List[str]) -> str:
    from config import Settings
    from notify_line import send_line_message
    settings = Settings.load()
    if not settings.line_channel_access_token:
        raise NotifyError("DISCORD_WEBHOOK_URL も LINE_CHANNEL_ACCESS_TOKEN も未設定です。")
    send_line_message(channel_access_token=settings.line_channel_access_token, message=texts,
                      user_id=settings.line_user_id or None)
    return f"LINE ({settings.line_user_id or '友だち全員へbroadcast'})"
