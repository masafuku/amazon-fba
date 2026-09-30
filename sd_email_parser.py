#!/usr/bin/env python3
"""sd_email_parser.py — 1サイクル実行して終了するスクリプト(sp_api_sync.pyと
同じ設計)。Super Deliveryの注文確定メール(件名「＜SD＞ご注文内容控え」)を
Gmail APIで検索・取得し、正規表現で仕入原価・数量・送料を抽出して
ops_finance.jp_purchase_records に書き込む。

メール本文フォーマット例(本日実際に受信したもの):
    [ 発注日 ] 2026/09/08(火)18:37
    ...
    ■出展企業(問い合わせ先)：Zoomy BUNGU
    ----------------------------------------------------------------------
    [　受付番号　]　93977902
    [　 SD品番 　]　15782388S2
    [　 商品名 　]　【ナカバヤシ】シリコンブックマーカー パタップ
    [ JANコード　]　4902205744443
    [　　内訳　　]　DSB-PTP-CY クリアイエロー
    [　注文点数　]　10点
    [　注文単価　]　\\196
    [　注文金額　]　\\1,960
    ----------------------------------------------------------------------
    [お支払い方法]Paid（掛け）
    [　商品小計　] \\1,960
    [送料(見込み)] \\1,100
    [小計(税抜き)] \\3,060
    [消費税(10%)] \\306
    [ クーポン利用 ] -1,210

1通のメールに複数の出展企業ブロック・複数の商品ブロックが含まれることがある
(受付番号ごとに商品ブロックが繰り返される)。受付番号は一意なので、
ops_finance.upsert_jp_purchase_record() 側で重複投入を防ぐ。

送料(見込み)・クーポン利用は出展企業(仕入先)ブロックごとに1つずつ現れる
(1回の発注内で仕入先が複数あれば、それぞれ別々に送料・クーポンが付く)。
クーポン控除後の実質送料が0円より大きい場合のみ、ops_finance.allocate_order_shipping()
で同ブロック内の商品行に数量按分で書き込む(2026-09-30、CEOへの提案通りの運用:
「クーポンで無料なら反映不要」)。

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
SUPPLIER_BLOCK_SPLIT = re.compile(r"(?=■出展企業\(問い合わせ先\))")
RECEPTION_BLOCK_SPLIT = re.compile(r"(?=\[\s*受付番号\s*\])")
ORDER_DATE_PATTERN = r"発注日\s*\]\s*([0-9]{4})/([0-9]{2})/([0-9]{2})"
SHIPPING_PATTERN = r"送料\(見込み\)\s*\]\s*[\\¥]?([0-9,]+)"
COUPON_PATTERN = r"クーポン利用\s*\]\s*(-?[0-9,]+)"


def _clean_label_text(text: str) -> str:
    # ラベル自体の全角スペース(例: "受付番号　　")を取り除いた残りの行だけを返す。
    return re.sub(r"^[　\s]*", "", text).split("\n")[0].strip()


def _to_number(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def extract_order_date(body: str) -> str | None:
    """メール本文の[ 発注日 ] YYYY/MM/DD(曜)HH:MM からISO日付(YYYY-MM-DD)を取り出す。
    見つからなければNone(呼び出し元がGmailメッセージのDateヘッダ等にフォールバックする)。"""
    m = re.search(ORDER_DATE_PATTERN, body)
    if not m:
        return None
    year, month, day = m.groups()
    return f"{year}-{month}-{day}"


def parse_email_body(body: str, order_date: str | None = None) -> list:
    """1通のメール本文を出展企業(仕入先)ブロックごとに分割し、各ブロック内の
    複数の受付番号ブロックをパースしてrecord dictのリストを返す。
    各recordには、そのブロックの送料・クーポン控除後の実質送料(netShippingJpy、
    ブロック内の全商品で共有、run_once側でallocate_order_shippingに渡す)も含む。"""
    records = []
    supplier_blocks = [b for b in SUPPLIER_BLOCK_SPLIT.split(body) if "受付番号" in b]
    if not supplier_blocks:
        return records

    for supplier_block in supplier_blocks:
        supplier_match = re.search(SUPPLIER_PATTERN, supplier_block)
        supplier_name = supplier_match.group(1).strip() if supplier_match else None

        shipping_match = re.search(SHIPPING_PATTERN, supplier_block)
        shipping_jpy = _to_number(shipping_match.group(1)) if shipping_match else 0.0
        coupon_match = re.search(COUPON_PATTERN, supplier_block)
        coupon_jpy = abs(_to_number(coupon_match.group(1))) if coupon_match else 0.0
        net_shipping_jpy = max((shipping_jpy or 0.0) - (coupon_jpy or 0.0), 0.0)

        item_blocks = [b for b in RECEPTION_BLOCK_SPLIT.split(supplier_block) if "受付番号" in b]
        for block in item_blocks:
            fields = {}
            for key, pattern in FIELD_PATTERNS.items():
                m = re.search(pattern, block)
                fields[key] = _clean_label_text(m.group(1)) if m else None

            if not fields.get("sdReceptionNo"):
                continue

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
                "netShippingJpy": net_shipping_jpy,
            })
    return records


def run_once(max_messages: int = 50) -> None:
    if not gmail.configured():
        logger.info("Gmail API認証情報が未設定のため、パースをスキップします(.envにGMAIL_CLIENT_ID等を設定してください)。")
        return

    message_ids = gmail.search_messages(SEARCH_QUERY, max_results=max_messages)
    logger.info(f"対象メール: {len(message_ids)}件")

    total_new = 0
    shipping_allocated = set()  # (supplierName, orderDate)の組で1メール内の重複呼び出しを防ぐ
    for message_id in message_ids:
        body = gmail.get_message_plaintext(message_id)
        if not body:
            continue
        order_date = extract_order_date(body) or datetime.now(timezone.utc).date().isoformat()
        records = parse_email_body(body, order_date=order_date)
        for record in records:
            net_shipping = record.pop("netShippingJpy", 0.0)
            if of.upsert_jp_purchase_record(record):
                total_new += 1
            if net_shipping and net_shipping > 0:
                key = (record["supplierName"], order_date)
                if key not in shipping_allocated:
                    of.allocate_order_shipping(record["supplierName"], order_date, net_shipping)
                    shipping_allocated.add(key)

    of.set_sd_parse_state(datetime.now(timezone.utc).isoformat())
    logger.info(f"新規登録: {total_new}件")


def main() -> None:
    parser = argparse.ArgumentParser(description="Super Delivery注文確定メールをパースしてjp_purchase_recordsに登録する")
    parser.add_argument("--max-messages", type=int, default=50)
    args = parser.parse_args()
    run_once(max_messages=args.max_messages)


if __name__ == "__main__":
    main()
