import { useMemo } from 'react';

const usd = (v) => `${v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// 商品別P&Lの横棒グラフ。利益は送料前(手取り − 原価。国際送料・関税は引かない)。多い順。
export default function ProductPnlBars({ products }) {
    const rows = useMemo(
        () => [...(products ?? [])].filter((p) => p.units > 0).sort((a, b) => b.netProfitUsd - a.netProfitUsd),
        [products],
    );
    const { lo, hi } = useMemo(() => ({
        lo: Math.min(0, ...rows.map((r) => r.netProfitUsd)),
        hi: Math.max(0.01, ...rows.map((r) => r.netProfitUsd)),
    }), [rows]);
    if (!rows.length) return null;
    const span = hi - lo;
    const zero = (-lo / span) * 100;
    const total = rows.reduce((s, r) => s + r.netProfitUsd, 0);

    return (
        <div className="mb-4">
            <div className="mb-2 text-xs text-slate-400">
                棒の長さが利益(送料前: 手取り − 原価)。右に、利益と、個数×1個あたりの平均を出しています。合計 {usd(total)}
            </div>
            <div className="space-y-1">
                {rows.map((r) => {
                    const v = r.netProfitUsd;
                    const left = v >= 0 ? zero : zero - (Math.abs(v) / span) * 100;
                    const width = Math.max(0.5, (Math.abs(v) / span) * 100);
                    const tip = [
                        r.productName,
                        `利益 ${usd(v)}`, `売れた数 ${r.units}個`, `1個あたり ${usd(v / r.units)}`,
                        `売上 ${usd(r.revenueUsd)}`, `手数料 ${usd(r.feesUsd)}`, `原価 ${usd(r.cogsUsd)}`,
                        r.marginPct != null ? `利益率 ${r.marginPct}%` : null,
                    ].filter(Boolean).join('\n');
                    return (
                        <div key={r.asin} className="flex items-center gap-2 text-xs" title={tip}>
                            <div className="w-28 shrink-0 truncate text-right text-slate-200 sm:w-48">{r.productName}</div>
                            <div className="relative h-5 flex-1">
                                <div className="absolute inset-y-0 w-px bg-slate-600" style={{ left: `${zero}%` }} />
                                <div
                                    className="absolute inset-y-0.5 rounded-sm"
                                    style={{ left: `${left}%`, width: `${width}%`, background: v < 0 ? '#f43f5e' : '#38bdf8' }}
                                />
                            </div>
                            <div className="w-36 shrink-0 text-slate-300 sm:w-44">
                                {usd(v)} <span className="text-slate-500">{r.units}個×{usd(v / r.units)}</span>
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
