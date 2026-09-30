#!/usr/bin/env python3
"""
send_daily_digest.py — 1日2回(朝8時・夜8時)、前回の通知以降にたまった
合格候補をまとめて1通のLINEメッセージにして送る。

daily_scan.py 自体は実行のたびに通知しない(即時通知は不要というCEOの
指示)。代わりにこのスクリプトを1日2回スケジュール実行し、
ops_finance.load_digest_window() で「前回このスクリプトを実行した時刻」
以降に agent_candidates に保存された合格候補(ASIN重複は最新のものだけ)
と、その間に検索したキーワード一覧をまとめて通知する。

前回の送信時刻は ops_finance.get_last_digest_sent_at() / DBの
digest_state テーブルで管理しており、送信のたびに更新される。初回実行時
(まだ一度も送っていない)は直近24時間分を対象にする。

使い方:
    python3 send_daily_digest.py                # 朝/夜どちらでも同じ処理
    python3 send_daily_digest.py --period "朝の"  # 通知文の見出しに使う接頭辞(任意)
    python3 send_daily_digest.py --dry-run       # 送信せず内容を表示するだけ

スケジュール: Claude Codeのスケジュールタスク機能(またはcron)で
毎日8:00と20:00に実行するよう設定する。1回の実行がそのまま「前回送信〜今」
の範囲になるので、時刻がずれても取りこぼしは起きない(次回にまとめて拾われる)。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from config import Settings
from notify_line import send_line_message
from ops_finance import (
    build_daily_digest_message,
    check_budget_alert,
    get_last_digest_sent_at,
    init_ops_tables,
    load_digest_window,
    set_last_digest_sent_at,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="前回送信以降の合格候補をまとめてLINEに通知する")
    parser.add_argument("--period", type=str, default="", help="通知文の見出しに使うラベル(例: '朝の' / '夜の')")
    parser.add_argument("--dry-run", action="store_true", help="送信せず、組み立てたメッセージを表示するだけ")
    args = parser.parse_args()

    init_ops_tables()

    since_iso = get_last_digest_sent_at()
    candidates, keywords, effective_since = load_digest_window(since_iso)

    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    budget_alerts = check_budget_alert(this_month)

    period_label = f"{args.period}候補" if args.period else "候補"
    message = build_daily_digest_message(candidates, keywords, period_label, budget_alerts=budget_alerts)

    print(f"[INFO] 対象期間: {effective_since} 〜 現在 (前回送信: {since_iso or 'なし(初回)'})")
    print(f"[INFO] 検索キーワード: {len(keywords)}件 / 合格候補: {len(candidates)}件(重複除く)")
    print("--- 通知内容 ---")
    print(message)

    if args.dry_run:
        print("[INFO] --dry-run のため送信しません。")
        return

    settings = Settings.load()
    if not settings.line_channel_access_token:
        print("[WARN] LINE_CHANNEL_ACCESS_TOKEN が未設定のため、送信をスキップしました。")
        return

    try:
        send_line_message(
            channel_access_token=settings.line_channel_access_token,
            message=message,
            user_id=settings.line_user_id or None,
        )
        target = settings.line_user_id or "友だち全員へbroadcast"
        print(f"[INFO] LINEダイジェストを送信しました ({target})。")
        set_last_digest_sent_at(datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        print(f"[ERROR] LINE送信失敗: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
