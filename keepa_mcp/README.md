# Keepa Arbitrage Finder (MCP server)

Keepa API を使って「日本で仕入れて北米(Amazon.com)で販売する」せどり/OA候補商品を
探すための MCP サーバーです。Claude Code / Claude Desktop からツールとして呼び出せます。

## 探せる条件（デフォルト値の例）

- カテゴリ: Keepa のカテゴリID指定（`search_category` で名前から検索）
- 北米ランキング: 1,000〜20,000位
- レビュー数: 200件以下
- 価格変動: 過去90日で ±20% 以内
- 価格差率: 40%以上（日本Amazonの価格 → 北米Amazonの価格、為替は `.env` の `USD_TO_JPY`）

## セットアップ

1. ルートの `.env` に Keepa API キーを設定（`.env.example` を参照）

   ```bash
   cp ../.env.example ../.env
   # .env を編集して KEEPA_API_KEY=... を設定
   ```

2. 依存関係をインストール（リポジトリ共通の venv を使用）

   ```bash
   cd ..
   python3 -m venv .venv
   ./.venv/bin/pip install -r requirements.txt
   ```

3. ルートの `.mcp.json` に `keepa` サーバーが登録済みです。Claude Code でこのプロジェクトを
   開けば自動的に MCP サーバーとして認識されます（`.venv` の Python を直接起動します）。

   単体で動作確認したい場合:

   ```bash
   .venv/bin/python -m keepa_mcp.server
   ```
   (stdio で待機するプロセスです。Ctrl+C で終了)

## 提供ツール

| ツール | 用途 | Keepaトークン消費 |
|---|---|---|
| `search_category(term, domain)` | カテゴリ名からカテゴリIDを検索 | 小 |
| `find_candidates(category_id, sales_rank_min, sales_rank_max, review_count_max, max_results, domain)` | Product Finder で粗く候補ASINを絞り込み（価格情報なし） | 小〜中 |
| `get_product_detail(asin, domain)` | 1商品の価格・90日変動・ランキング・レビュー数・UPC/EANを取得 | 中 |
| `find_jp_price(code)` | UPC/EANでAmazon.co.jp側の同一商品と現在価格を検索 | 中 |
| `find_arbitrage_candidates(category_id, ...)` | 上記を一括実行し、価格差率・価格変動の条件を満たす候補だけ返す一括検索 | 大（max_candidates で調整） |

典型的な使い方: まず `search_category("Kitchen Utensils & Gadgets")` などでカテゴリIDを
特定し、そのIDを `find_arbitrage_candidates` に渡して一括検索します。

## 注意点

- Amazon の ASIN はマーケットプレイスごとに異なるため、日本⇔北米の商品照合は
  UPC/EAN バーコード（`upcList`/`eanList`）で行っています。UPC/EANが登録されていない
  商品はマッチできず候補から除外されます（理由は `find_arbitrage_candidates` の
  `skipped` に記録されます）。
- 価格差率は `(北米価格(円換算) - 日本価格) / 日本価格` で計算しています。FBA手数料や
  関税・送料は含まれていないため、実際の利益率とは異なります。
- Keepa の API 利用はトークン制です。`find_arbitrage_candidates` はカテゴリ検索1回 +
  ASINごとの詳細取得 + ASINごとのJP照合、と `max_candidates` に比例してトークンを
  消費します。まずは `max_candidates` を小さく（10〜20件程度）して試すことを推奨します。
- 価格・ランキング等の Keepa フィールド仕様は変更される可能性があります。
  `find_candidates` や `find_arbitrage_candidates` が Keepa のエラーメッセージ
  （フィールド名不正など）を返す場合は `keepa_client.py` のフィルタ名を見直してください。
