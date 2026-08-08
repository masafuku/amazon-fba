# Amazon FBA Arbitrage Agent

このリポジトリは、US Amazon と日本 Amazon の価格差を調べるための Python プロトタイプです。

## ディレクトリ構成

- ルート: Pythonの実行モジュールとテスト
- `keepa-csv-dashboard/`: Keepa CSV分析と商品FinderのReactダッシュボード
- `research/amazon_export_research/`: Keepa CSVエクスポート調査用の実験コード
- `data/samples/`: ローカルで取得したHTMLなどの検証用サンプル（Git管理対象外）

認証情報、取得データ、SQLiteデータベース、フロントエンドの依存関係とビルド成果物は
`.gitignore`で除外しています。

## 特長
- Keepa を使って US Amazon の商品検索とセールス情報を取得
- 日本 Amazon で同一商品を検索して価格を取得
- 価格差 30% 以上の商品を抽出
- 手動実行で結果を表示し、メール通知も可能

## 使い方
1. 環境をセットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. `.env` を作成して設定をコピー

```bash
cp .env.example .env
```

3. `main.py` を実行

```bash
python main.py
```

4. メール通知を実行する場合

```bash
python main.py --notify-email
```

## CSVダッシュボード

```bash
cd keepa-csv-dashboard
npm install
npm run dev
```

SQLite APIを使う場合は、別ターミナルで `python3 sqlite_api_server.py` を実行します。

## Keepa MCPサーバー（せどり候補検索）

Claude Code / Claude Desktop から Keepa API を使って「日本仕入れ→北米販売」の候補商品を
検索できる MCP サーバーです。詳細は [`keepa_mcp/README.md`](keepa_mcp/README.md) を参照してください。
このリポジトリを Claude Code で開くと `.mcp.json` 経由で自動的に利用可能になります。

## 注意
- US Amazon のスクレイピングは難しいため、Keepa API を使って US 側のデータを取得します。
- 日本 Amazon の価格取得はスクレイピングで実装しています。IP ブロックや HTML 変更に注意してください。
