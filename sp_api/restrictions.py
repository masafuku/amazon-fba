"""出品制限の照会結果(Listings Restrictions API)の解釈。

client.get_listings_restrictions() の返り値から「出品可 / 承認が必要 / 出品不可」を判定する。
scripts/check_listing_restrictions.py(1件ずつの確認)と、
scripts/check_candidate_restrictions.py(候補の一括確認)で共有する。
"""
from __future__ import annotations

STATUS_OK = "ok"
STATUS_APPROVAL_REQUIRED = "approval_required"
# 「この商品(ASIN)を出品するための承認」が必要なもの(ブランドの承認の理由は無い)。
# 例: セザンヌ(B07H97J6TP)は、新品の理由がこの1つだけ。
STATUS_PRODUCT_APPROVAL_REQUIRED = "product_approval_required"
# ブランドの承認に加えて、「この商品を出品するための承認」も必要なもの(理由が2つ)。ブランド承認を
# 取っても出品できない。例: Holbein のガッシュ(B075YJLKRZ)。CEOがSeller Centralの画面で確認済み。
STATUS_BRAND_AND_PRODUCT_APPROVAL_REQUIRED = "brand_and_product_approval_required"
STATUS_NOT_ELIGIBLE = "not_eligible"
STATUS_RESTRICTED = "restricted"

STATUS_LABELS = {
    STATUS_OK: "出品可",
    STATUS_APPROVAL_REQUIRED: "承認が必要",
    STATUS_PRODUCT_APPROVAL_REQUIRED: "商品の承認が必要",
    STATUS_BRAND_AND_PRODUCT_APPROVAL_REQUIRED: "ブランド＋商品の承認が必要",
    STATUS_NOT_ELIGIBLE: "出品不可",
    STATUS_RESTRICTED: "制限あり",
}


def summarize_restrictions(payload: dict) -> tuple[str, list[str]]:
    """(状態, 詳細行) を返す。restrictions が空なら出品可。"""
    restrictions = (payload or {}).get("restrictions") or []
    if not restrictions:
        return STATUS_OK, []
    details, codes = [], set()
    product_level = False
    brand_level = False
    for restriction in restrictions:
        for reason in restriction.get("reasons") or []:
            code = reason.get("reasonCode") or ""
            codes.add(code)
            message = (reason.get("message") or "").lower()
            if code == "APPROVAL_REQUIRED" and (
                "この商品を出品するための承認" in message or "approval to list this product" in message
            ):
                product_level = True
            if code == "APPROVAL_REQUIRED" and (
                "このブランドには出品許可" in message or "approval to sell this brand" in message
                or "approval to list this brand" in message
            ):
                brand_level = True
            details.append(f"{code}: {reason.get('message', '')}".strip())
            for link in reason.get("links") or []:
                details.append(f"  申請/詳細: {link.get('resource', '')}")
    if product_level and brand_level:
        return STATUS_BRAND_AND_PRODUCT_APPROVAL_REQUIRED, details
    if product_level:
        return STATUS_PRODUCT_APPROVAL_REQUIRED, details
    if "APPROVAL_REQUIRED" in codes:
        return STATUS_APPROVAL_REQUIRED, details
    if "NOT_ELIGIBLE" in codes:
        return STATUS_NOT_ELIGIBLE, details
    return STATUS_RESTRICTED, details
