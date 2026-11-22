import { useEffect, useMemo, useState } from 'react';
import { loadStockBreakdown } from './db';

// 色はartifactの「商品ごとの内訳」に合わせる(売れた・販売可能・倉庫間移動中・FC受領中・調査中・その他・紛失)
const SEGMENTS = [
    { key: 'sold', label: '売れた', color: '#38bdf8' },
    { key: 'sellable', label: '販売可能', color: '#34d399' },
    { key: 'transship', label: '倉庫間移動中', color: '#f59e0b' },
    { key: 'receiving', label: 'FC受領中', color: '#fcd34d' },
    { key: 'research', label: '調査中', color: '#b45309' },
    { key: 'other', label: 'その他(内訳のずれ)', color: '#475569' },
    { key: 'lost', label: '紛失', color: '#f43f5e' },
];

// 商品ごとの内訳。棒の長さ=送った数。点線の枠=自宅(まだ送っていない)と入荷中。
export default function StockBreakdownChart() {
    const [data, setData] = useState(null);
    const [error, setError] = useState('');

    useEffect(() => {
        let cancelled = false;
        loadStockBreakdown()
            .then((d) => { if (!cancelled) setData(d); })
            .catch((e) => { if (!cancelled) setError(e?.message || '在庫の内訳を読み込めませんでした。'); });
        return () => { cancelled = true; };
    }, []);

    const rows = useMemo(() => {
        const list = (data?.rows ?? [])
            .filter((r) => r.sent > 0 || r.home > 0)
            .map((r) => ({
                ...r,
                other: Math.max(0, r.sent - r.sold - r.sellable - r.lost - r.transship - r.receiving - r.research),
            }))
            .sort((a, b) => (b.sent + b.home) - (a.sent + a.home) || b.incoming - a.incoming);
        return list;
    }, [data]);

    const max = useMemo(() => Math.max(1, ...rows.map((r) => r.sent + r.home + r.incoming)), [rows]);

    if (error) return <div className="mb-3 rounded-lg bg-rose-900/40 px-3 py-2 text-xs text-rose-200">{error}</div>;
    if (!data) return <div className="mb-3 text-xs text-slate-500">内訳を読み込み中…</div>;
    if (!rows.length) return null;

    return (
        <div className="mb-4">
            <div className="mb-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-400">
                {SEGMENTS.map((s) => (
                    <span key={s.key} className="inline-flex items-center gap-1">
                        <i className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} />{s.label}
                    </span>
                ))}
                <span className="inline-flex items-center gap-1">
                    <i className="inline-block h-2.5 w-2.5 rounded-sm border border-amber-400" />自宅(まだ送っていない)
                </span>
                <span className="inline-flex items-center gap-1">
                    <i className="inline-block h-2.5 w-2.5 rounded-sm border border-dashed border-slate-400" />入荷中
                </span>
            </div>
            <div className="space-y-1">
                {rows.map((r) => {
                    const pct = (v) => `${(v / max) * 100}%`;
                    const note = [
                        r.sent ? (r.bought > r.sent ? `${r.sent}／${r.bought}` : String(r.sent)) : null,
                        r.home ? `自宅${r.home}` : null,
                        r.incoming ? `入荷中${r.incoming}` : null,
                    ].filter(Boolean).join(' ・ ');
                    const tip = [
                        r.product,
                        `仕入れた ${r.bought}`, `送った ${r.sent}`, `売れた ${r.sold}`, `販売可能 ${r.sellable}`,
                        `その他(作業中など) ${r.other}`, r.lost ? `紛失 ${r.lost}` : null, `入荷中 ${r.incoming}`,
                    ].filter(Boolean).join('\n');
                    return (
                        <div key={r.product} className="flex items-center gap-2 text-xs" title={tip}>
                            <div className="w-28 shrink-0 truncate text-right text-slate-200 sm:w-48">{r.product}</div>
                            <div className="relative h-5 flex-1">
                                <div className="absolute inset-y-0 flex" style={{ left: 0, width: pct(r.sent) }}>
                                    {SEGMENTS.map((s) => (r[s.key] ? (
                                        <div key={s.key} style={{ width: `${(r[s.key] / (r.sent || 1)) * 100}%`, background: s.color, minWidth: 1 }} />
                                    ) : null))}
                                </div>
                                {r.home > 0 && (
                                    <div className="absolute inset-y-0 rounded-sm border border-amber-400" style={{ left: pct(r.sent), width: pct(r.home) }} />
                                )}
                                {r.incoming > 0 && (
                                    <div className="absolute inset-y-0 rounded-sm border border-dashed border-slate-400" style={{ left: pct(r.sent + r.home), width: pct(r.incoming) }} />
                                )}
                            </div>
                            <div className="w-24 shrink-0 text-slate-400 sm:w-36">{note}</div>
                        </div>
                    );
                })}
            </div>
            <div className="mt-1 text-xs text-slate-500">棒の長さが送った数。売れた数と販売可能は、古い便から順に割り振った概算です。</div>
        </div>
    );
}
