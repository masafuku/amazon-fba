---
name: review-request
description: 出荷済みの注文にAmazon定型の「レビューとセラー評価のお願い」を送る手順。「レビュー依頼送れる？」「レビュー依頼を送って」「レビュー返ってきてる？」と言われたときに使う。予行演習で一覧を出し、CEOの承認を得てから送る。自動では送らない。
---

# レビュー依頼(Solicitations API)

**自動では送らない(CEO 2026-11-11)。毎回、予行演習の一覧を見せて「はい」をもらってから送る。**
背景と送った履歴はメモ [[seller-feedback-buybox]]。セラー評価が少ないとBuy Boxが取れず売れないので、評価を増やすために送る。

SSH: `ssh -i ~/.ssh/LightsailDefaultKey-ap-northeast-1.pem ubuntu@52.199.161.97`
作業ディレクトリ: `/home/ubuntu/work/amazon-fba`

## 1. 予行演習(何も送らない・記録しない)

`review_request.py` はAWSに常駐させていない。ローカルの最新を `/tmp` に送って実行する(約1〜2分。APIは1秒1回):

```bash
K=~/.ssh/LightsailDefaultKey-ap-northeast-1.pem
scp -i $K review_request.py ubuntu@52.199.161.97:/tmp/
ssh -i $K ubuntu@52.199.161.97 'cd /home/ubuntu/work/amazon-fba && PYTHONPATH=. .venv/bin/python /tmp/review_request.py --dry-run'
```

出力:
- **今送れる**: 送る候補(返金なし)。日付・商品名つき。
- **返金ありのため送らない**: 送信時に `refunded` と記録され、以後は見ない。
- **Amazonが送れないと言う注文**: 配達5日前か、理由不明。注文から12日以上は「←理由不明」。
  原因の候補: すでに送った(Seller Centralから手で送った等)、配達日が遅い、Amazon側の対象外。
  次の予行演習で送れるようになることもあるので、毎回見直す。注文から40日を過ぎると一覧から消える。
- **これから**: 注文から5日未満(未出荷を含む)。配達の5日後から送れるので、だいたい注文の1〜2週間後に「今送れる」に移る。

## 2. CEOに確認する

商品名・注文日・件数で「今送れる」を見せ、「送りますか？」と聞く。理由不明の注文と、これからの見込み(何日ごろ何件)も添える。
「レビュー返ってきてる？」と聞かれても、評価件数は amazon.com の出品者ページがAIのアクセスを拒否するので見られない。
Seller Central(パフォーマンス → フィードバック)でCEOに見てもらう。拒否の画面で「続ける」を押して回避しない。

## 3. 送る(承認後)

```bash
ssh -i $K ubuntu@52.199.161.97 'cd /home/ubuntu/work/amazon-fba && PYTHONPATH=. .venv/bin/python /tmp/review_request.py; rm /tmp/review_request.py'
```

- 結果 `{"sent": [...], "skipped_refunded": [...]}`。送った注文は AWS の `ledger_state`(キー `review_request_done`)に記録され、二度と送らない。
- 予行演習と送信の間に新しく送れるようになった注文も送られる。件数が予行演習と違ったら報告する。
- 送ったら、メモ `seller-feedback-buybox` に1行足す(日付・件数・商品の内訳・累計)。

## 注意

- 送れるのは配達の5〜30日後、1注文1回だけ。文面はAmazonの定型で変えられない。
- 返金のあった注文には送らない(スクリプトがFinances APIで除外)。
- 定期実行・同期への組み込みはしない(CEO決定)。
