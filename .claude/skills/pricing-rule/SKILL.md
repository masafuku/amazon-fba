---
name: pricing-rule
description: 出品中の全SKUをAmazonの自動価格設定(ダイナミックプライシング)ルールに紐付け、最低価格・最高価格を設定し直す手順。「価格設定を更新」「最低価格を決め直して」「新しい出品を自動価格にして」「TNKの請求が届いたので反映して」と言われたときに使う。
---

# 自動価格設定ルールの設定・更新

SP-APIの認証情報はローカルの`.env`にしかないため、**ローカルで実行**する。仕入れ原価の正本はAWSのDB。
ルールそのものはAPIで作れない（Seller Centralで作る）。使えるルールは`get_pricing_rule_ids`で取れる
（現在は `1293652560402-COMPETITIVE_BUYBOX`＝Amazonの競争力のある価格ルール1つ）。

## 手順

1. **原価をAWSから取る**（1個あたり＝仕入れ金額（税抜）、円。`shipping_cost_jpy`は物流費の概算なので**含めない**。物流費は`INTL_SHIPPING_USD`で別に引くため、含めると二重計上になる）:
   ```bash
   ssh -i ~/.ssh/LightsailDefaultKey-ap-northeast-1.pem ubuntu@52.199.161.97 'cd /home/ubuntu/work/amazon-fba && python3 - <<EOF
   import sqlite3, json
   c = sqlite3.connect("keepa-csv-dashboard/keepa_imports.sqlite3")
   rows = c.execute("SELECT asin, SUM(IFNULL(amount_jpy,0))/SUM(quantity) FROM jp_purchase_records WHERE asin IS NOT NULL AND quantity > 0 GROUP BY asin").fetchall()
   print(json.dumps({a: round(v, 1) for a, v in rows}))
   EOF' > <scratchpad>/costs.json
   ```
   新しい商品は、台帳でASINが紐付いていないと原価が取れずSKIPされる（`stock_ledger.py map-sd`）。
2. **検証だけ実行**（何も反映しない。VALIDATION_PREVIEW）:
   `.venv/bin/python pricing_rule.py --costs <scratchpad>/costs.json`
   全行が `VALID` か、最低・最高価格がCEOの方針に合うかを確認する。約2分かかる（APIの間隔制限）。
3. **反映**: 同じコマンドに `--apply`。全行が `ACCEPTED` になる。
4. **確認**: 反映は非同期。`GET /listings/2021-08-01/items/{seller}/{sku}?includedData=attributes` で
   `purchasable_offer` に最低・最高価格とルールIDが入っていることを1〜2件確認する。
5. コミットして`feat/ops-finance-bridge`にpush。

## 価格の決め方（CEO合意 2026-10-10）
- **最低価格** ＝ バイボックス価格(Product Pricing API getItemOffers)と**損益分岐点**の真ん中（セント切り上げ）。
  真ん中が「損益分岐点＋原価の20%（`MIN_ROI`）」を下回るときは後者。バイボックスが取れないときも後者。
  →価格競争に巻き込まれすぎないための余裕。基準は**出品中の価格ではなく相場**（出品中の価格は仮置きが多い）。
- **損益分岐点** ＝ (FBA手数料 ＋ 物流費 ＋ 関税 ＋ 原価) ÷ (1 − 販売手数料率)。
  販売手数料・FBA手数料は**Product Fees API の実額**（価格帯で変わるので価格を動かして収束）。
  関税は原価の12.5%、為替¥150/$。
- **最高価格** ＝ 現在価格の2倍（最低価格の1.5倍を下回らない）。
- 現在価格が最低価格を下回るSKUは、現在価格も最低価格まで引き上げる（`our_price`は一緒に渡す。渡さないと価格が消える）。
- 1つのSKUに付けられるルールは1つ。切り替えはルールIDの置き換え、解除は `automated_pricing_merchandising_rule_plan` を `null` にするPATCH。

## 見直すタイミング
- **TNKの請求書が届いたら**（発送の翌月10日ごろ。第一便は2026-11-10ごろ）:
  `pricing_rule.py`の`INTL_SHIPPING_USD`（1個あたりの物流費。現在は第一便の概算 (運賃¥4,373＋燃油サーチャージ¥2,044[9/11の見積と同じ比率47%を**仮定**]＋発送代行¥1,500＋国内送料(自宅→TNK)約¥750)÷90個÷¥150 ≒ $0.642）を実額に差し替える。
  あわせてAWSの`jp_purchase_records.shipping_cost_jpy`（第一便の4行に概算が入っている）と台帳の`shipping_estimate`イベントも実額に直し、手順2〜3をやり直す。
- 為替や関税率、FBA手数料の改定時、商品を追加したとき、相場が大きく動いたとき。
- 第二便以降は国際送料の運賃が変わる（FedEx Economyは2026-10-10から改定、2kg区分 ¥4,373→¥4,543）。
  運賃表は `https://docs.google.com/spreadsheets/d/1tFWiBODxykXF83-yTJQECioZk4gPvtklSAce8nbAHPU/gviz/tq?tqx=out:csv&sheet=<シート名>` でCSVとして読める
  （「U.S.A(Rest of country)」列が東側＝納品先HIA1。請求重量は実重量と容積重量の大きい方を0.5kg刻み）。

## 注意
- 最低価格は固定値。相場が動いても自動では変わらない。決め直すときは手順2から。
- 同じASINに複数のSKUがあると価格が別々になる（パタップ ブルー `7J-ESCZ-TEEU` と `NAKA-PATAP-1531672`）。整理はCEOに確認。
- 認証情報（`.env`）をチャットに貼らない・コードに書かない。
