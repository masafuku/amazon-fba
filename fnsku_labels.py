#!/usr/bin/env python3
"""FBAの商品ラベル(FNSKUラベル)のPDFを作る。A4・40面(4列×10行)のラベルシート用。

各ラベル: Code 128のバーコード(FNSKU)・FNSKUの文字・商品名(英語)・状態(New)。Amazonの規格
(黒インク・白い非反射のラベル・Code 128)に沿う。ラベルは出品者側で貼る設定(labelOwner: SELLER)の商品に使う。

シートの配置は、よくあるA4 40面(1枚 52.5×29.7mm・余白なし)を既定にしている。シートによって違う場合は
--cell-width/--cell-height/--margin-left/--margin-top で合わせる。**まず --grid で枠だけのPDFを
普通紙に印刷し、ラベルシートに重ねて光に透かして、ずれが無いかを確認してから本番を印刷する。**

使い方(SP-APIの認証情報があるマシンで):
    fnsku_labels.py --stock stock.json --out labels.pdf          # 第二便の対象(在庫台帳の手元+輸送中)
    fnsku_labels.py --grid --out grid.pdf                        # 位置合わせ用の枠だけのPDF
    fnsku_labels.py --stock stock.json --out labels.pdf --start-cell 14   # 使いかけのシートの15枚目から
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

from reportlab.graphics.barcode import code128
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = 210 * mm, 297 * mm
COLS, ROWS = 4, 10


def short_title(title: str, limit: int = 62) -> str:
    title = re.sub(r'[^\x20-\x7e]', '', title or '').strip()   # 標準フォントで出せる文字だけ
    title = re.sub(r'\s+', ' ', title)
    return title if len(title) <= limit else title[:limit - 2].rstrip() + '..'


def expand(items: list, spare: int = 0) -> list:
    """[{fnsku, title, quantity}] -> 1枚ずつのラベルのリスト(予備を各商品にspare枚足す)。"""
    labels = []
    for item in items:
        labels += [item] * (int(item['quantity']) + spare)
    return labels


def build_pdf(labels: list, path: str, cell: tuple = (52.5, 29.7), margin: tuple = (0.0, 0.0),
              start_cell: int = 0, grid: bool = False, condition: str = 'New') -> int:
    """ラベルを1枚ずつ並べたPDFを書き、ページ数を返す。start_cellは先頭の空きセル数(使いかけのシート用)。"""
    cell_w, cell_h = cell[0] * mm, cell[1] * mm
    left, top = margin[0] * mm, margin[1] * mm
    c = canvas.Canvas(path, pagesize=(PAGE_W, PAGE_H))
    per_page = COLS * ROWS
    total = ([None] * start_cell) + (labels if not grid else [{'fnsku': '', 'title': '', 'quantity': 1}] * per_page)
    pages = 0
    for index, label in enumerate(total):
        position = index % per_page
        if position == 0 and index > 0:
            c.showPage()
        if position == 0:
            pages += 1
        col, row = position % COLS, position // COLS
        x = left + col * cell_w
        y = PAGE_H - top - (row + 1) * cell_h
        if grid:
            c.setStrokeColorRGB(0.2, 0.2, 0.2)
            c.setLineWidth(0.3)
            c.rect(x, y, cell_w, cell_h)
            c.setFont('Helvetica', 6)
            c.drawCentredString(x + cell_w / 2, y + cell_h / 2 - 2, f'{index + 1}')
            continue
        if label is None:
            continue
        pad = 2.0 * mm
        inner_w = cell_w - 2 * pad
        # バーコード: Code 128(FNSKU)。細い棒の幅0.3mmで、約10桁なら約44mmに収まる。
        barcode = code128.Code128(label['fnsku'], barHeight=11 * mm, barWidth=0.3 * mm, quiet=False)
        bx = x + (cell_w - barcode.width) / 2
        barcode.drawOn(c, bx, y + cell_h - pad - 11 * mm - 1 * mm)
        c.setFillColorRGB(0, 0, 0)
        c.setFont('Helvetica-Bold', 8)
        c.drawCentredString(x + cell_w / 2, y + cell_h - pad - 11 * mm - 1 * mm - 3.2 * mm, label['fnsku'])
        c.setFont('Helvetica', 5.2)
        title = short_title(label.get('title'))
        words, lines, current = title.split(' '), [], ''
        for word in words:
            trial = (current + ' ' + word).strip()
            if c.stringWidth(trial, 'Helvetica', 5.2) <= inner_w:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        for i, text in enumerate(lines[:2]):
            c.drawCentredString(x + cell_w / 2, y + pad + 3.2 * mm - i * 2.1 * mm + 1.0 * mm, text)
        c.setFont('Helvetica-Bold', 6)
        c.drawString(x + pad, y + pad - 0.2 * mm + 0.0, condition)
    c.save()
    return pages


def fetch_titles(asins: list) -> dict:
    from sp_api import client
    titles = {}
    for asin in sorted(set(asins)):
        time.sleep(0.3)
        r = client._request(f"/catalog/2022-04-01/items/{asin}",
                            {"marketplaceIds": client.settings.marketplace_id, "includedData": "summaries", "locale": "en_US"})
        titles[asin] = (r.get('summaries') or [{}])[0].get('itemName', '')
    return titles


def main() -> None:
    parser = argparse.ArgumentParser(description='FNSKUラベル(A4 40面)のPDFを作る')
    parser.add_argument('--out', required=True)
    parser.add_argument('--stock', help='stock_ledger.py stock --json の出力ファイル(第二便の対象を決める)')
    parser.add_argument('--grid', action='store_true', help='位置合わせ用の枠だけのPDFを作る')
    parser.add_argument('--spare', type=int, default=0, help='各商品に足す予備のラベル枚数')
    parser.add_argument('--start-cell', type=int, default=0, help='使いかけのシートで、先頭の使用済みセル数')
    parser.add_argument('--cell-width', type=float, default=52.5)
    parser.add_argument('--cell-height', type=float, default=29.7)
    parser.add_argument('--margin-left', type=float, default=0.0)
    parser.add_argument('--margin-top', type=float, default=0.0)
    args = parser.parse_args()
    cell, margin = (args.cell_width, args.cell_height), (args.margin_left, args.margin_top)

    if args.grid:
        pages = build_pdf([], args.out, cell, margin, grid=True)
        print(f'位置合わせ用の枠 {pages}ページ → {args.out}')
        return
    if not args.stock:
        sys.exit('--stock か --grid を指定してください。')
    import inbound_draft
    with open(args.stock, encoding='utf-8') as f:
        stock = {r['asin']: r for r in json.load(f)}
    items, _ = inbound_draft.eligible_items(stock, only_with_fnsku=True, include_errors=True, include_planned=True)
    titles = fetch_titles([i['asin'] for i in items])
    for item in items:
        item['title'] = titles.get(item['asin'], '')
    labels = expand(items, args.spare)
    pages = build_pdf(labels, args.out, cell, margin, start_cell=args.start_cell)
    print(f'{len(items)}商品・ラベル{len(labels)}枚・{pages}ページ → {args.out}')
    for item in items:
        print(f"  {item['fnsku']}  ×{item['quantity'] + args.spare:<3} {item['msku']:<22} {short_title(item['title'], 40)}")


if __name__ == '__main__':
    main()
