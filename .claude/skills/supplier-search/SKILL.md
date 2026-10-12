---
name: supplier-search
description: 「〇〇(SD業者名)で候補を探して」「SD業者から仕入れ候補を探して」と言われたときの手順。取引済みのSD業者の商品から、US Amazonで利益が出る候補を探してAWS DBに登録する。
---

# supplier-search スキル

取引済みのSD(SuperDelivery)業者から、US Amazonで利益が出る商品候補を探す。
前半(ブラウザ操作: SD → Amazon.co.jp → Keepa製品ビューア)は下の手順、後半(計算・Tier・登録)は
`supplier_pipeline.py`(ops_finance の公式式をそのまま使う)で行う。**計算式を手書きしない。**

## 0. 最初にやること(毎回)

1. このスキルと `supplier-search-status` のメモを読む(自己流で SD を叩き始めない)。
2. AWSで既存ASINを取り出す。調査の前に除外するため(調べた後で重複と分かる無駄を防ぐ)。
   ```bash
   KEY=~/.ssh/LightsailDefaultKey-ap-northeast-1.pem
   scp -i $KEY supplier_pipeline.py ubuntu@52.199.161.97:/tmp/
   ssh -i $KEY ubuntu@52.199.161.97 "cd /home/ubuntu/work/amazon-fba && PYTHONPATH=. python3 /tmp/supplier_pipeline.py existing --out /tmp/existing.json"
   scp -i $KEY ubuntu@52.199.161.97:/tmp/existing.json .
   ```
3. 業者ID: `https://www.superdelivery.com/p/do/dpsl/<業者ID>/`(`/p/r/` は旧形式)。
   既知: ハリマ共和物産 `204961`、Zoomy BUNGU `1003908`、丸進 `102452`。
   不明なら `https://www.superdelivery.com/p/do/psl/?word=<業者名>&so=pk` で探す。

除外カテゴリ(一覧段階でスキップ): 食品・飲料・菓子・サプリ、化粧品・医薬品・スキンケア・入浴剤、電子機器・家電・
電池内蔵品(ファン・時計・電卓など)、包丁・刃物(ハサミ・爪切り含む)、フィギュア・コレクタブル、
アパレル(下着・靴下)、カレンダー・手帳・年賀・クリスマスなどの季節の日本向け商品。

## 1. SD: 人気順の上位N件の品番・卸価格を取得(ログイン済みChrome)

**SDはTurnstileがあるため、ログイン済みのChrome(`mcp__claude-in-chrome__*`)だけを使う。**
一覧は人気順120件/ページ: `/p/do/dpsl/<ID>/`(1ページ目)、`/p/do/dpsl/<ID>/all/<N>/`。

**アクセスの作法(2026-10-12に守らず「メンテナンス中」表示を出した):**
- 商品ページの取得は**直列・1件ごとに1.2秒空ける**。並列や数百件の一括は禁止。
- 一度に走らせるのは100〜200件まで。`window.__prog` で進捗を確認しながら待つ。
- メンテナンス/制限の画面が出たら、すぐ止めて報告する(同じ取得を繰り返さない)。

一覧と詳細を1本のJSで取る(Chromeで業者の一覧ページを開いた状態で実行。結果は `window.__res`):

```javascript
window.__prog={done:0,state:'listing'};window.__res={};
(async()=>{
 const items=[],seen={};
 const parse=doc=>doc.querySelectorAll('a[href*="/p/r/pd_p/"]').forEach(a=>{const id=a.href.match(/pd_p\/(\d+)/)[1];if(seen[id])return;
  const t=((a.closest('li')||a.parentElement.parentElement)?.innerText||'').replace(/\s+/g,' ').replace('検討中に追加','').trim();
  if(t.length<3)return;seen[id]=1;const m=t.match(/(.*?)¥([\d,]+)\/点/);
  items.push({id,name:(m?m[1]:t).replace(/SOLD OUT|NEW|リピート多数/g,'').trim(),price:m?+m[2].replace(/,/g,''):null})});
 parse(document);   // 1ページ目。2ページ目以降は /all/2/ をfetchして parse(new DOMParser().parseFromString(html,'text/html'))
 window.__top=items.slice(0,100);   // 例: 人気順100位まで
 window.__prog={done:0,total:window.__top.length,state:'running'};
 for(const it of window.__top){
  try{
   const r=await fetch(`/p/r/pd_p/${it.id}/`,{credentials:'include'});
   const d=new DOMParser().parseFromString(await r.text(),'text/html');
   const t=[...d.querySelectorAll('table')].map(x=>x.innerText.replace(/\s+/g,' ')).find(s=>/卸単価/.test(s))||'';
   window.__res[it.id]={status:r.status,t:t.slice(0,500),noexp:/国内販売のみ|ご遠慮/.test(d.body.innerText)};
  }catch(e){window.__res[it.id]={err:String(e)}}
  window.__prog.done++; await new Promise(r=>setTimeout(r,1200));
 }
 window.__prog.state='finished';
})();
```

商品ページの表(`t`)の読み方:
- **Zoomy BUNGU / ハリマ**: バリエーションごとに `JAN：<13桁>` が載る。卸単価=`卸単価（NN%） ： ¥xxx`、ロット=`1セット（N点）`。
- **丸進(サンリオ)**: JANは載らない(ページのHTMLにある `4580235733684` は全商品共通の別物で使えない)。
  `S1 キティ （348881）` の括弧内が**メーカー品番**。これでAmazon.co.jpを検索する。
- 「国内販売のみ」「Amazon.co.jpでの販売はご遠慮ください」(`noexp`)・SOLD OUT は候補にしない/注記する。
- 卸価格は**SDの税抜価格をそのまま** map.csv に入れる(税込への換算はパイプラインがやる)。

## 2. Amazon.co.jp: ASINを特定(内蔵ブラウザ、Keepaトークン不消費)

内蔵ブラウザ(`mcp__Claude_Browser__*`)で `https://www.amazon.co.jp/s?k=...` を一度開き、
そのページ(同一オリジン)から検索結果を1.5秒間隔でfetchして `window.__az` に集める:

```javascript
// L = [[sd_id, 品番またはJAN, 卸価格], ...]
(async()=>{ window.__az={}; window.__azp={done:0,state:'running'};
 for(const [id,key,w] of L){
  const h=await (await fetch('/s?k='+key+'+%E3%82%B5%E3%83%B3%E3%83%AA%E3%82%AA',{credentials:'include'})).text();   // 丸進は「サンリオ」を足す。JANならkeyのみ
  const d=new DOMParser().parseFromString(h,'text/html'); const hits=[];
  d.querySelectorAll('[data-asin]').forEach(e=>{const a=e.dataset.asin; if(!a)return;
   const t=(e.querySelector('h2')?.getAttribute('aria-label')||e.querySelector('h2')?.innerText||'');
   if(t.includes(key)&&!hits.find(x=>x[0]==a)) hits.push([a,t.slice(0,60),e.querySelector('.a-price .a-offscreen')?.textContent||'']);});
  window.__az[id]={key,w,hits:hits.slice(0,1),blocked:/captcha|ロボット/i.test(h)};
  window.__azp.done++; await new Promise(r=>setTimeout(r,1500));
 } window.__azp.state='finished'; })();
```

- 品番で検索するとタイトルに品番が入る商品が多く、`t.includes(品番)` の一致で同一商品と確実に判断できる。
- JAN検索は外れることがあるので、タイトルを目視確認する。0件なら商品名・型番で再検索、それでも無ければスキップ。

## 3. Keepa製品ビューア: US市場の確認とCSV出力(APIトークン不消費、ログイン済みChrome)

`https://keepa.com/#!viewer` を開き、ASIN(改行区切り)を投入する:

```javascript
await new Promise(r=>setTimeout(r,2500));
Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='ASIN')?.click();
await new Promise(r=>setTimeout(r,400));
const ta=document.querySelectorAll('textarea')[0];
Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(ta, ASINS.join('\n'));
ta.dispatchEvent(new Event('input',{bubbles:true}));
await new Promise(r=>setTimeout(r,300));
Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='リストを読み込む')?.click();
await new Promise(r=>setTimeout(r,12000));
```

**エクスポートの注意(2026-10-12に何度か空振り):**
1. 読み込み後に「次のN件のASINが読み込めませんでした」(USに無いASIN)のダイアログが出る。**先に `Escape` を押して閉じる**(閉じないとエクスポートのクリックが横取りされる)。
2. エクスポートボタンは座標 (466,117) をクリック → ダイアログに「CSV」が選択済みなのをスクリーンショットで確認 → (468,471) をクリック。
3. `ls -t ~/Downloads/KeepaExport*` で最新ファイルの行数が「読み込めた件数+1」か確認する。古いファイルのままなら失敗している。

## 4. 計算・Tier・登録(`supplier_pipeline.py`)

map.csv(列: `sd_id,part_no,wholesale_jpy,asin,name`)を作り、評価する:

```bash
python3 supplier_pipeline.py evaluate --map map.csv --keepa ~/Downloads/KeepaExport-....csv \
    --supplier 丸進 --existing existing.json --out evaluation.json
```

- 利益・ROIは `ops_finance.calc_unit_profit`(送料0円・関税12.5%・ROI=利益÷卸原価)、卸価格は
  `normalize_jp_cost_for_tax`(免税事業者の間は税込=SDの税抜×1.1)、TierはAWSと同じ `_apply_priority_fields`。
- **足切り(ROIを計算する前に除外、登録しない)**: US順位なし/50万位より下、USの現在価格なし、
  US価格(円換算)が卸の10倍超かつ月間販売の実数値なし。実需のない出品の異常価格がROI数百〜1,000%超に
  見えてTier Dで残る問題への対処。
- Tierの根拠が実数値(月間販売)か推定(ランク変動のみ)かを `evidence` に残す(レポートで「(推定)」と表示)。
- 重量がKeepaに無い商品は既定値で計算し `weight_estimated` を立てる(黙って仮定しない)。
- 既存ASINは登録せずスキップし、Tierが食い違うものを `diffs` として表示する(上書きしない)。

登録(AWS)。まず予行演習、内容を確認してから `--apply`:

```bash
scp -i $KEY evaluation.json ubuntu@52.199.161.97:/tmp/
ssh -i $KEY ubuntu@52.199.161.97 "cd /home/ubuntu/work/amazon-fba && PYTHONPATH=. python3 /tmp/supplier_pipeline.py register --evaluation /tmp/evaluation.json --supplier 丸進"
# 問題なければ --apply を付けて再実行
```

過去に旧方式(税抜・独自ROI式)で登録した supplier 行の監査(DBは変えない):
`... supplier_pipeline.py audit`

## 優先度ティア(`_classify_priority_tier`)

実売の確度(強い実売>=100 / 実売あり30〜99 / ランク変動のみ(月間なし・30日の変動>=10) / 実売なし=D)× ROI
(>=100% / 20〜100% / 0〜20% / 赤字)。合格Tier: S / A+ / A- / B+ / B- / C+。
`rank_drops_30` = Keepa CSV「過去90日間の減少」÷ 3。

## 注意事項

- SDはTurnstileがあるためログイン済みのChromeのみ。直列・1.2秒間隔(上記)。
- AWS DBが唯一の正本。登録は必ずAWSで実行する(ローカルのsqliteは古いコピー)。
- NETSEAは2026-10-07よりランサムウェア攻撃で停止中(使用不可)。
- US EAN ≠ JP JAN でも問題なし(FBAではFNSKUラベルが上書き)。
