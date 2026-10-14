// 仕入れ先(卸売りプラットフォーム)の表示名。CEO: 「仕入れ先の情報は、ダッシュボードページは
// 簡潔に、SD, NSなどの短縮などにして、個別ページに詳しく表記してください」(2026-09-28)。
// 一覧ページ(AgentPage)は短縮(abbr)、候補詳細ページ(CandidateDetailPage)はフル名(full)を使う。
// 出展企業名(shop_name)自体は略さない - どちらのページでも別途表示する。
export const SUPPLIER_SOURCES = {
    netsea: { abbr: 'NS', full: 'NETSEA' },
    sd: { abbr: 'SD', full: 'SuperDelivery' },
};

export function supplierSourceLabel(source, style = 'abbr') {
    return SUPPLIER_SOURCES[source]?.[style] ?? source ?? '?';
}

// NETSEA自動連携(data.netsea_shop_name等、単一)と、手動追加(data.manual_suppliers、
// 複数、scripts/add_manual_supplier.py経由でClaudeが記録)を、1つのリストにまとめて
// 安い順に並べる。AgentPage(一覧)・CandidateDetailPage(詳細)の両方で同じ並びを使う。
export function mergeSuppliers(data) {
    const netseaSupplier = data?.netsea_shop_name
        ? {
              source: 'netsea', shopName: data.netsea_shop_name, url: data?.netsea_product_url ?? null,
              priceJpy: data?.wholesale_cost_jpy ?? null, minQty: data?.netsea_set_num ?? null,
              checkedAt: data?.wholesale_checked_at ?? null,
          }
        : null;
    const manualSuppliers = (data?.manual_suppliers ?? []).map((s) => ({
        source: s.source, shopName: s.shop_name, url: s.url ?? null, priceJpy: s.price_jpy ?? null,
        minQty: s.min_qty ?? null, note: s.note ?? null, checkedAt: s.checked_at ?? null,
    }));
    return [netseaSupplier, ...manualSuppliers]
        .filter((s) => s && s.priceJpy != null)
        .sort((a, b) => a.priceJpy - b.priceJpy);
}
