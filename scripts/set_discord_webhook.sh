#!/bin/bash
# AWSの .env に Discord の通知用URL(Webhook)を書き込む。URLは画面に出さず、チャットにも残らない。
# 使い方: bash scripts/set_discord_webhook.sh
set -e
read -r -s -p "DiscordのウェブフックURL(貼り付けてEnter。表示されません): " URL; echo
case "$URL" in https://discord.com/api/webhooks/*|https://discordapp.com/api/webhooks/*) ;; *) echo "URLの形が違います(https://discord.com/api/webhooks/... のはず)"; exit 1;; esac
printf '%s\n' "$URL" | ssh -i ~/.ssh/LightsailDefaultKey-ap-northeast-1.pem ubuntu@52.199.161.97 'cd /home/ubuntu/work/amazon-fba && python3 -c "
import sys
url = sys.stdin.readline().strip()
lines = [l for l in open(\".env\").read().splitlines() if not l.startswith(\"DISCORD_WEBHOOK_URL=\")]
open(\".env\", \"w\").write(\"\n\".join(lines + [\"DISCORD_WEBHOOK_URL=\" + url]) + \"\n\")
print(\"AWSに設定しました\")
"'
