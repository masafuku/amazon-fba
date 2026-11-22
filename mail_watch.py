#!/usr/bin/env python3
"""AWSのcronで動かすメール確認: Gmail(読み取り専用API)を検索 → 在庫台帳に取り込み → 新しい動きと要対応をLINEで通知する。

手動の「メール確認して」(.claude/skills/mail-check)の、取り込みと通知の部分を自動化したもの。台帳の取り込みは
stock_ledger.ingest_batch(同じメールを2回渡しても重複しない)。通知は、今回新しく分かったことがあるときだけ送る
(新しい動き / 新しく出た要対応 / 新しいAmazonケースの返信)。

使い方(AWSで実行):
    mail_watch.py --dry-run      # 検索・取り込み・通知文の表示だけ(LINEは送らない。台帳には取り込む)
    mail_watch.py                # 取り込み + 新着があればLINEに通知
前提: .env に GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN(scripts/gmail_authorize.py で設定)。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import gmail_client
import ops_finance as of
import stock_ledger

JST = timezone(timedelta(hours=9))
SENDERS = 'from:raccoon.ne.jp OR from:superdelivery.com OR from:globalbrand.co.jp OR from:amazon.com OR from:amazon.co.jp'
CASE_SUBJECT = re.compile(r'\[CASE\s*\d+\]|ケース\s*ID\s*\d+', re.IGNORECASE)
NOTIFIED_KEY = 'mail_watch_notified_case_ids'
MAX_LINE_CHARS = 4500


def build_query(since: str) -> str:
    return f'after:{since} ({SENDERS}) -subject:新着'


def fetch_emails(since: str, max_results: int = 100) -> list[dict]:
    """台帳が扱う種類のメールと、Amazonのケース返信だけを返す(広告などは取らない)。"""
    emails = []
    for message_id in gmail_client.search_messages(build_query(since), max_results):
        email = gmail_client.get_message(message_id)
        if stock_ledger.classify(email.get('subject')) or CASE_SUBJECT.search(email.get('subject') or ''):
            emails.append(email)
    return emails


def _state(key: str):
    with sqlite3.connect(of.DB_PATH) as conn:
        return stock_ledger._get_state(conn, key)


def _set_state(key: str, value) -> None:
    with sqlite3.connect(of.DB_PATH) as conn:
        stock_ledger._set_state(conn, key, value)


def new_case_replies(emails: list[dict]) -> list[dict]:
    """まだ通知していないAmazonのケース返信(台帳には取り込まない種類)。"""
    seen = set(json.loads(_state(NOTIFIED_KEY) or '[]'))
    return [e for e in emails if CASE_SUBJECT.search(e.get('subject') or '') and not stock_ledger.classify(e.get('subject'))
            and e['id'] not in seen]


def remember_cases(cases: list[dict]) -> None:
    seen = json.loads(_state(NOTIFIED_KEY) or '[]') + [c['id'] for c in cases]
    _set_state(NOTIFIED_KEY, json.dumps(seen[-500:]))


def format_message(new_events: list[str], new_alerts: list[str], cases: list[dict], now: datetime | None = None) -> str:
    now = now or datetime.now(JST)
    lines = [f'【メール確認 {now:%m/%d %H:%M}】']
    if new_events:
        lines += ['■新しい動き'] + [f'・{e}' for e in new_events]
    if new_alerts:
        lines += ['■要対応(新規)'] + [f'・{a}' for a in new_alerts]
    if cases:
        lines += ['■Amazonケースの返信'] + [f"・{c['subject']}" for c in cases]
    text = '\n'.join(lines)
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS - 20] + '\n…(以下省略)'


def run(dry_run: bool = False, send=None) -> dict:
    """取り込みと通知。send(message)は通知の送信関数(省略時はLINE)。戻り値は結果の要約。"""
    of.init_ops_tables()
    since = stock_ledger.gmail_since()
    emails = fetch_emails(since)
    alerts_before = set(stock_ledger.alerts())
    new_events = stock_ledger.ingest_batch([e for e in emails if stock_ledger.classify(e.get('subject'))])
    new_alerts = [a for a in stock_ledger.alerts() if a not in alerts_before]
    cases = new_case_replies(emails)
    result = {'since': since, 'fetched': len(emails), 'new_events': new_events, 'new_alerts': new_alerts,
              'cases': [c['subject'] for c in cases], 'sent': False, 'message': None}
    if not (new_events or new_alerts or cases):
        return result
    message = format_message(new_events, new_alerts, cases)
    result['message'] = message
    if dry_run:
        return result
    if send is None:
        from config import Settings
        from notify_line import send_line_message
        settings = Settings.load()
        if not settings.line_channel_access_token:
            raise RuntimeError('LINE_CHANNEL_ACCESS_TOKEN が未設定のため通知できません')
        send = lambda m: send_line_message(channel_access_token=settings.line_channel_access_token, message=m,
                                           user_id=settings.line_user_id or None)
    send(message)
    remember_cases(cases)
    result['sent'] = True
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if not gmail_client.configured():
        print('[ERROR] Gmail APIの認証情報が .env にありません。scripts/gmail_authorize.py で設定してください。')
        return 2
    result = run(dry_run=args.dry_run)
    print(f"[INFO] 検索開始日 {result['since']} / 対象メール {result['fetched']}件 / 新しい動き {len(result['new_events'])}件 / "
          f"新規の要対応 {len(result['new_alerts'])}件 / 新しいケース返信 {len(result['cases'])}件 / LINE送信 {'済' if result['sent'] else 'なし'}")
    if result['message']:
        print(result['message'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
