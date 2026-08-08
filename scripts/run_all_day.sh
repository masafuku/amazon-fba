#!/usr/bin/env bash
# run_all_day.sh — daily_scan.py を止めずに1日中回し続けるループ。
#
# daily_scan.py は1回の実行で「キーワードプールから1個選ぶ→検索→評価→
# LINE通知」までを行い、それで終了する。Keepaのトークンは1分に1個しか
# 回復しない(かつ60個までしか貯まらない)プランなので、1日分のトークン
# (最大1,440個/日 = 1分×1,440分)を使い切るには、このスクリプトのように
# 外側からdaily_scan.pyを繰り返し呼び出し続ける必要がある。
#
# daily_scan.py自身はwait_for_tokens=True(デフォルト)で、トークンが
# 足りない間は内部でポーリング待機するので、このループは単純に
# 「1回実行→少し待つ→次のキーワードで1回実行→...」を繰り返すだけでよい。
# キーワードはキーワードプールから「一番使われていないもの」が毎回自動で
# 選ばれる(pick_next_keyword())ので、指定は不要。
#
# 使い方:
#   ./scripts/run_all_day.sh                  # デフォルト設定で開始
#   MAX_CANDIDATES=20 ./scripts/run_all_day.sh # 1回あたりの候補数を変更
#   ./scripts/run_all_day.sh --max-candidates 20 --category "Kitchen Utensils & Gadgets"
#     (daily_scan.py に渡す追加の引数。--keyword は指定しないこと -
#      指定するとプールを使わず常に同じキーワードになってしまう)
#
# 停止: Ctrl+C、または `kill <pid>`。SIGINT/SIGTERM で安全に終了する。
#
# ダッシュボードの停止ボタン(または `touch .scan_loop_stop_requested`)は
# これとは別の、よりソフトな停止方法: 実行中のサイクルを中断せず、次の
# サイクルを開始しないだけ。フラグファイル(リポジトリ直下の
# .scan_loop_stop_requested)の有無をサイクルの節目ごとにチェックし、
# あれば消費中のトークンを無駄にせず綺麗に終了する(SIGTERM経由の停止は
# 実行中のdaily_scan.pyごと即座に中断してしまうため、意図的に別の仕組みに
# している)。再開はダッシュボードの再開ボタン(フラグファイルを消して
# `systemctl start`)から。
#
# 常駐させたい場合(macOSでログイン時に自動起動するなど)は、この
# スクリプトを launchd の plist や `nohup ./scripts/run_all_day.sh &`
# から起動する。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
STOP_FLAG="$REPO_ROOT/.scan_loop_stop_requested"

VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  echo "[ERROR] $VENV_PYTHON が見つかりません。先に 'python3 -m venv .venv' & 'pip install -r requirements.txt' を実行してください。" >&2
  exit 1
fi

# 1回あたりの評価件数上限(小さいほどトークン消費が少なく、1日に回せる
# キーワード数が増える。目安: 10〜15件なら1回あたり最大31〜41トークン)。
MAX_CANDIDATES="${MAX_CANDIDATES:-12}"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_all_day.log"

# 連続エラー時に無限ループでAPIを叩き続けないためのバックオフ設定。
NORMAL_SLEEP_SECONDS=30
ERROR_SLEEP_SECONDS=300
consecutive_errors=0

running=1
trap 'running=0; log "[INFO] 停止シグナルを受信しました。今のサイクルが終わり次第終了します。"' INT TERM

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE"
}

log "[INFO] run_all_day.sh 開始 (MAX_CANDIDATES=$MAX_CANDIDATES, PID=$$)"
log "[INFO] 追加引数: $*"

cycle=0
while [ "$running" -eq 1 ]; do
  if [ -e "$STOP_FLAG" ]; then
    log "[INFO] 停止フラグ($STOP_FLAG)を検出。次のサイクルは開始せず終了します。"
    break
  fi

  cycle=$((cycle + 1))
  log "[INFO] --- サイクル $cycle 開始 ---"

  if "$VENV_PYTHON" daily_scan.py --max-candidates "$MAX_CANDIDATES" "$@" >> "$LOG_FILE" 2>&1; then
    log "[INFO] サイクル $cycle 完了。"
    consecutive_errors=0
    sleep_seconds="$NORMAL_SLEEP_SECONDS"
  else
    consecutive_errors=$((consecutive_errors + 1))
    log "[WARN] サイクル $cycle が異常終了しました(連続 $consecutive_errors 回目)。詳細は $LOG_FILE を確認してください。"
    sleep_seconds="$ERROR_SLEEP_SECONDS"
  fi

  if [ "$running" -eq 0 ]; then
    break
  fi

  log "[INFO] ${sleep_seconds}秒待機します..."
  # sleepを中断可能にする(シグナル、または停止フラグで即座にループを抜ける)。
  for _ in $(seq 1 "$sleep_seconds"); do
    [ "$running" -eq 1 ] || break
    [ -e "$STOP_FLAG" ] && break
    sleep 1
  done
done

log "[INFO] run_all_day.sh 終了。"
