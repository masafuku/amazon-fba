#!/usr/bin/env python3
"""sd_email_parser.py — 1サイクル実行して終了するスクリプト(sp_api_sync.pyと
同じ設計)。Super Deliveryの注文確定メール(件名「＜SD＞ご注文内容控え」)を
Gmail APIで検索・取得し、正規表現で仕入原価・数量を抽出して
ops_finance.jp_purchase_records に書き込む。

メール本文フォーマット例(本日実際に受信したもの):
    [　受付番号　]　93977902
    [　 SD品番 　]　15782388S2
    [　 商品名 　]　【ナカバヤシ】シリコンブックマーカー パタップ
    [ JANコード　]　4902205744443
    [　　内訳　　]　DSB-PTP-CY クリアイエロー
    [　注文点数　]　10点
    [　注文単価　]　\\196
    [　注文金額　]　\\1,960
    ■出展企業(問い合わせ先)：Zoomy BUNGU

1通のメールに複数の出展企業ブロック・複数の商品ブロックが含まれることがある
(受付番号ごとに商品ブロックが繰り返される)。受付番号は一意なので、
ops_finance.upsert_jp_purchase_record() 側で重複投入を防ぐ。

Gmail認証情報(GMAIL_CLIENT_ID等)が未設定の場合は何もせずログだけ出して
正常終了する。Keepaには一切依存しない。
"""
from __future__ import annotations

import argparse
import logging
import re
from datetime import datetime, timezone

import gmail_client as gmail
import ops_finance as of

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("sd_email_parser")

SEARCH_QUERY = 'subject:"ご注文内容控え"'

# 各商品ブロックを受付番号区切りで抜き出し、続くフィールドをラベルで拾う。
# 全角スペース([　])を含むラベル表記のゆらぎを許容するため、各ラベル内の
# 空白文字はすべて `\s*` として扱う。
FIELD_PATTERNS = {
    "sdReceptionNo": r"受付番号\s*\]\s*([0-9]+)",
    "sdProductNo": r"SD品番\s*\]\s*(\S+)",
    "productName": r"商品名\s*\]\s*(.+)",
    "janCode": r"JANコード\s*\]\s*([0-9]+)",
    "variant": r"内訳\s*\]\s*(.+)",
    "quantity": r"注文点数\s*\]\s*([0-9,]+)点",
    "unitPriceJpy": r"注文単価\s*\]\s*[\\¥]?([0-9,]+)",
    "amountJpy": r"注文金額\s*\]\s*[\\¥]?([0-9,]+)",
}
SUPPLIER_PATTERN = r"出展企業\(問い合わせ先\)\s*[：:]\s*(.+)"
RECEPTION_BLOCK_SPLIT = re.compile(r"(?=\[\s*受付番号\s*\])")


def _clean_label_text(text: str) -> str:
    # ラベル自体の全角スペース(例: "受付番号　　")を取り除いた残りの行だけを返す。
    return re.sub(r"^[　\s]*", "", text).split("\n")[0].strip()


def parse_email_body(body: str, order_date: str | None = None) -> list:
    """1通のメール本文から複数の受付番号ブロックをパースし、record dictのリストを返す。"""
    records = []
    supplier_match = re.search(SUPPLIER_PATTERN, body)
    # 出展企業ブロックが複数ある場合、直近に出てきたものを採用する簡易実装
    # (1メール1出展企業なら問題ない。複数出展企業のメールは将来的に
    # ブロック分割の精度を上げる余地あり)。
    default_supplier = supplier_match.group(1).strip() if supplier_match else None

    blocks = [b for b in RECEPTION_BLOCK_SPLIT.split(body) if "受付番号" in b]
    for block in blocks:
        fields = {}
        for key, pattern in FIELD_PATTERNS.items():
            m = re.search(pattern, block)
            if not m:
                fields[key] = None
                continue
            value = _clean_label_text(m.group(1))
            fields[key] = value

        if not fields.get("sdReceptionNo"):
            continue

        supplier_in_block = re.search(SUPPLIER_PATTERN, block)
        supplier_name = supplier_in_block.group(1).strip() if supplier_in_block else default_supplier

        def _to_number(s):
            if s is None:
                return None
            try:
                return float(str(s).replace(",", ""))
            except ValueError:
                return None

        records.append({
            "orderDate": order_date,
            "sdReceptionNo": fields["sdReceptionNo"],
            "supplierName": supplier_name,
            "sdProductNo": fields.get("sdProductNo"),
            "productName": fields.get("productName"),
            "janCode": fields.get("janCode"),
            "variant": fields.get("variant"),
            "unitPriceJpy": _to_number(fields.get("unitPriceJpy")),
            "quantity": int(_to_number(fields.get("quantity"))) if fields.get("quantity") else None,
            "amountJpy": _to_number(fields.get("amountJpy")),
        })
    return records


def run_once(max_messages: int = 50) -> None:
    if not gmail.configured():
        logger.info("Gmail API認証情報が未設定のため、パースをスキップします(.envにGMAIL_CLIENT_ID等を設定してください)。")
        return

    message_ids = gmail.search_messages(SEARCH_QUERY, max_results=max_messages)
    logger.info(f"対象メール: {len(message_ids)}件")

    total_new = 0
    for message_id in message_ids:
        body = gmail.get_message_plaintext(message_id)
        if not body:
            continue
        order_date = datetime.now(timezone.utc).date().isoformat()  # メールヘッダのDateを使う改善余地あり
        records = parse_email_body(body, order_date=order_date)
        for record in records:
            if of.upsert_jp_purchase_record(record):
                total_new += 1

    of.set_sd_parse_state(datetime.now(timezone.utc).isoformat())
    logger.info(f"新規登録: {total_new}件")


def main() -> None:
    parser = argparse.ArgumentParser(description="Super Delivery注文確定メールをパースしてjp_purchase_recordsに登録する")
    parser.add_argument("--max-messages", type=int, default=50)
    args = parser.parse_args()
    run_once(max_messages=args.max_messages)


if __name__ == "__main__":
    main()
