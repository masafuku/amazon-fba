# Keepa CSV Research Dashboard

React + Vite + Tailwind CSS のシンプルな CSV 分析ダッシュボードです。

## セットアップ

```bash
cd keepa-csv-dashboard
npm install
npm run dev
```

## SQLファイル保存（SQLite）

CSV読込時に、ブラウザ内DBではなくSQLiteファイルへ保存するには、別ターミナルでAPIサーバーを起動してください。

```bash
cd keepa-csv-dashboard
python3 sqlite_api_server.py
```

- API: `http://127.0.0.1:8001`
- DBファイル: `keepa-csv-dashboard/keepa_imports.sqlite3`
- 保存テーブル: `keepa_items`

フロントエンド（`npm run dev`）と API サーバーの両方が起動している状態で、CSV を読み込むと自動で SQL 保存されます。

## 使い方

1. Keepa でエクスポートした CSV をドラッグ＆ドロップ。
2. 画面右側で為替レートや送料を調整。
3. 右上の「CSVダウンロード」で計算結果付きCSVを出力。
