---
name: fba-status
description: 「在庫状況」「入庫は進んでる？」「どれが買える状態？」「第N便はどうなってる？」と聞かれたときの手順。Amazonの在庫API(今の値)と在庫台帳(AWSのDB)を合わせ、今売れる/移動中/受領中/調査中/自宅/発注済みを商品名でまとめ、前回からの変化を報告する。読み取りのみ。
---

# 在庫・入庫の状況確認

読み取りだけ。DB・出品・価格は変えない。スクリプトを一時フォルダで書き直さず、必ず `fba_status.py` を使う。

SSH: `ssh -i ~/.ssh/LightsailDefaultKey-ap-northeast-1.pem ubuntu@52.199.161.97`
作業ディレクトリ: `/home/ubuntu/work/amazon-fba`

## 1. 実行(AWS)

`fba_status.py` はAWSに常駐させていない(デプロイはCEO承認制)。ローカルの最新を `/tmp` に送って実行する:

```bash
K=~/.ssh/LightsailDefaultKey-ap-northeast-1.pem
scp -i $K fba_status.py ubuntu@52.199.161.97:/tmp/
ssh -i $K ubuntu@52.199.161.97 'cd /home/ubuntu/work/amazon-fba && PYTHONPATH=. .venv/bin/python /tmp/fba_status.py'
```

- 「買える状態？」「出品は有効？」と聞かれたら `--listings` を付ける(1SKU約1秒、全体で1分弱)。
- 細かく加工したいときは `--json`。
- 実行のたびに AWS `~/fba_status/<JSTの日付>.json` に在庫の数を保存し(14日分)、前の日の結果と比べて `(前回+N)` を出す。

出力の段階:
| 見出し | 意味 |
|---|---|
| 今売れる | fulfillable。「出荷待ちの注文」は売れたが出荷前の数(在庫から引き当て済み) |
| 倉庫の間を移動中 | pendingTransshipment。受領後にFC間を移動中。数日で「今売れる」に移る |
| FCで受領待ち・受領中 | inboundReceiving。受領が止まった便(第2便のような受領取り消し)もここに残る |
| 注文済みで出荷を待っている | 販売可能0だが注文がある(移動中・受領中の在庫から出荷される) |
| 調査中 | researching。短期1〜10日/中期11〜20日/長期21日〜。見つからなければ補填対象 |
| 台帳 | 自宅・納品プラン済み・TNK・国際輸送・仕入先未出荷・国内輸送(stock_ledger.stock_pipeline) |

## 2. 報告する

- 商品名で書く(ASIN・SKUは書かない)。表を使い、便ごとに分ける。
- 順番: 今売れる在庫(前回からの増減と理由: 注文/移動) → 移動中(第何便か、売れるようになる見込み) →
  受領が止まっている・調査中 → 自宅 → 発注済み・未出荷 → 気になる点を1〜2行。
- 便の見分けは、どの便に入れた商品か(台帳の便・メモ [[first-shipment-leadtime]] [[asin-first-sellable-leadtime]] の日数)で判断する。
- 「気になる点」の例: 売れ筋の在庫が30日販売ペースで3週間を切った、移動中が前回から1つも動いていない(3日以上)、
  調査中が長期(21日〜)に入った、受領中が便のFC到着から2週間以上残っている。
- 販売可能0で「購入できる(BUYABLE)」は、移動中・受領中の在庫から注文を受けられる状態。カート(Buy Box)を取れているかは別(セラー評価が少ないため)。

## 注意

- DBの `sp_fba_inventory` は3時間ごとの同期の値なので、今の数には使わない(このスクリプトはAPIから直接取る)。
- 台帳とAmazonの数が合わないとき(受領数の不足など)は勝手に直さず、CEOに報告する。紛失にするかの判断は [[fba-lost-units]]。
- 第2便の受領停止は11/25以降に受領差異の照会をする予定([[second-shipment-receiving-reversal]])。
