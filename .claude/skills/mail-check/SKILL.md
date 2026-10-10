---
name: mail-check
description: 「メール確認して」「メール見て」「Gmail確認」と言われたときの定型手順。SD・TNK・Amazonのメールを在庫台帳(AWSのDB)に取り込み、領収書をDriveに整理し、新しい動きと要対応を報告する。要約だけで終わらせず、必ず取り込みまで行う。
---

# メール確認

メールの要約だけで終わらせないこと。**在庫台帳への取り込みと領収書の整理まで行う**のがこの手順。
DBはAWSが唯一の正本で、ローカルのsqliteは使わない。

SSH: `ssh -i ~/.ssh/LightsailDefaultKey-ap-northeast-1.pem ubuntu@52.199.161.97`
作業ディレクトリ: `/home/ubuntu/work/amazon-fba`（Pythonは `.venv/bin/python`）

## 1. 検索開始日を取る

```bash
ssh -i ~/.ssh/LightsailDefaultKey-ap-northeast-1.pem ubuntu@52.199.161.97 'cd /home/ubuntu/work/amazon-fba && .venv/bin/python stock_ledger.py since'
```
→ `YYYY/MM/DD` が返る（取り込み済みの最新メールの前日。重複取り込みは無害）。

## 2. Gmailで探す

Gmailの `search_threads` で次を検索する（`<since>` は手順1の値）:

```
after:<since> (from:raccoon.ne.jp OR from:superdelivery.com OR from:globalbrand.co.jp OR from:amazon.com) -subject:新着
```

件名が次のどれかに当たるメッセージだけ、`get_thread`（`messageFormat: PLAIN_TEXT`）で本文を取る。
スレッドに複数メッセージがあれば（Amazonの `RE:[CASE …]` など）、**メッセージごとに**1件として扱う。

| 件名に含まれる文字列 | 意味 |
|---|---|
| `＜SD＞ご注文内容控え` | SD注文 |
| `＜SD＞出荷予定日のご連絡` | 出荷予定日 |
| `＜SD＞出荷完了いたしました` | 仕入れ先が発送（運送会社・送り状番号） |
| `TNK Logisticsからのお知らせ: 貨物が到着しました` | TNK入庫 |
| `TNK Logisticsからのお知らせ: 貨物を発送しました` | 国際便発送（FedEx番号・実重量） |
| `Brand Approval Request for` | ブランド承認／却下 |
| `Amazon Listing Created -` | 出品作成 |
| `FBA Inbound Shipment Checked-In / Receiving / Closed (FBA…)` | FBA着荷・受領開始・受領完了 |
| `＜Paid＞決済確定のお知らせ（スーパーデリバリー）` | SDの出荷ごとの決済額（支払いの月次集計。金額・日付つき） |
| `＜Paid＞ご入金ありがとうございます` | Paidへの入金確認（金額は本文にない。どの月か、CEOに確認して `paid --month` で登録） |
| `価格の誤設定に対処し、停止された出品情報を回復する` | amazon.co.jp等での価格誤設定による出品停止（SKU・価格つき。要対応） |

SDの「新着・プライスダウン情報」、ログイン通知、Amazonの「新しい返信先アドレスが追加されました」は対象外。

## 3. JSONにしてAWSで取り込む

スクラッチパッドに `mail_ingest.json` を Write ツールで作る。形式（本文は `plaintextBody` を**加工せずそのまま**入れる）:

```json
[
  {"id": "<メッセージのid>", "subject": "<件名>", "sender": "<送信者>", "date": "<メッセージのdate(ISO)>", "body": "<plaintextBody>"}
]
```

```bash
scp -i ~/.ssh/LightsailDefaultKey-ap-northeast-1.pem <scratchpad>/mail_ingest.json ubuntu@52.199.161.97:/tmp/mail_ingest.json
ssh -i ~/.ssh/LightsailDefaultKey-ap-northeast-1.pem ubuntu@52.199.161.97 'cd /home/ubuntu/work/amazon-fba && .venv/bin/python stock_ledger.py ingest /tmp/mail_ingest.json; rm /tmp/mail_ingest.json'
```

出力は「== 新しい動き」と「== 要対応」。対象メールが0件でも、要対応を確認するために `stock_ledger.py alerts` は実行する。
同じメールを2回渡しても重複しないので、迷ったら入れてよい。

## 4. 領収書・請求書をDriveに整理する

メモリ `receipt-invoice-filing-workflow` の手順に従う（既知の送信者は確認なしで保存、不明な送信者は保存せず確認する。FBAと不動産は中身で判断して混ぜない）。

## 5. 報告する

この順で短く書く:
1. **新しい動き**：取り込み結果を「注文／出荷予定／仕入先出荷／TNK入庫／国際発送／ブランド／出品」ごとにまとめる（同じ便の行は1行にまとめる）
2. **要対応**：`alerts` の内容。特に
   - `国内輸送N日経過` → 「届いていますか？」と聞く
   - `ブランド却下` → 仕入れ済み・発注予定の商品に関係するか伝える
   - `ASIN未紐付け` → 出品するか聞く
3. **領収書**：発行元・金額・日付・保存先を1行ずつ
4. それ以外の重要なメール（Amazonからのケース返信、仕入れ先からの連絡など）があれば、件名と要点を1行ずつ

## CEOの返事を台帳に反映するコマンド

| 言われたこと | コマンド（AWSの作業ディレクトリで実行） |
|---|---|
| 「◯◯届いた」 | `stock_ledger.py received --tracking <送り状番号>`（または `--reception <受付番号...>`）。日付が今日でなければ `--on YYYY-MM-DD` |
| 「まだ届いてない」と言われて登録を取り消すとき | 上と同じコマンドに `--undo` を付ける |
| 「◯◯ブランドは再申請中」 | `stock_ledger.py brand <ブランド名> pending --note "再申請中"` |
| 新しいSD品番を出品するASINに紐付ける | `stock_ledger.py map-sd <SD品番> <ASIN>` |
| 「◯月分のSDを払った」 | `stock_ledger.py paid --month YYYY-MM --on YYYY-MM-DD [--note ...]` |
| SDの支払い状況・未払い | `stock_ledger.py payments` |
| 在庫状況・第N便の中身・次の発注 | `stock_ledger.py stock` |

**データの食い違い（伝票と実物が違う等）は勝手に直さず、CEOに確認してから反映する。**
