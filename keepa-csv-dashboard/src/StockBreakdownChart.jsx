import { useEffect, useMemo, useState } from 'react';
import { loadStockBreakdown } from './db';

// 色はartifact「FBA在庫と販売」の「商品ごとの内訳」の見た目(ライトテーマ)と同じにする。
// 売れた・販売可能 = デザインシステムのグラフ色1・2(blue-450 / orange-350)、
// 倉庫間移動中・FC受領中・調査中 = artifactのページが直接指定している色、
// その他 = chart-muted(gray-200)、紛失 = danger(text-danger)。
const SEGMENTS = [
    { key: 'sold', label: '売れた', color: '#2a78d6' },
    { key: 'sellable', label: '販売可能', color: '#eb6834' },
    { key: 'transship', label: '倉庫間移動中', color: '#ef9f27' },
    { key: 'receiving', label: 'FC受領中', color: '#fac775' },
    { key: 'research', label: '調査中', color: '#ba7517' },
    { key: 'other', label: 'その他(内訳のずれ)', color: '#c3c2b7' },
    { key: 'lost', label: '紛失', color: '#a42528' },
];
const HOME_COLOR = '#e49c06'; // artifactの「自宅」の枠(warning)。暗い背景で見えるよう明るいほうの値
const INCOMING_COLOR = 'rgba(255, 255, 255, 0.57)'; // 「入荷中」の点線(text-secondary)

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
            <div className="mb-2 text-xs text-slate-400">
                棒の長さが送った数(売れた数と販売可能は、古い便から順に割り振った概算)。右に、送った数／仕入れた数と、自宅・入荷中の数を出しています。
            </div>
            <div className="mb-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-400">
                {SEGMENTS.map((s) => (
                    <span key={s.key} className="inline-flex items-center gap-1">
                        <i className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} />{s.label}
                    </span>
                ))}
                <span className="inline-flex items-center gap-1">
                    <i className="inline-block h-2.5 w-2.5 rounded-sm" style={{ border: `1px solid ${HOME_COLOR}` }} />自宅(まだ送っていない)
                </span>
                <span className="inline-flex items-center gap-1">
                    <i className="inline-block h-2.5 w-2.5 rounded-sm" style={{ border: `1px dashed ${INCOMING_COLOR}` }} />入荷中
                </span>
            </div>
            <div className="space-y-1">
                {rows.map((r) => {
                    const pct = (v) => `${(v / max) * 100}%`;
                    const parts = [
                        r.sent ? (r.bought > r.sent ? `${r.sent}／${r.bought}` : String(r.sent)) : null,
                        r.home ? `自宅${r.home}` : null,
                        r.incoming ? `入荷中${r.incoming}` : null,
                    ].filter(Boolean);
                    const [primary, ...rest] = parts;
                    const tip = [
                        r.product,
                        `仕入れた ${r.bought}`, `送った ${r.sent}`, `売れた ${r.sold}`, `販売可能 ${r.sellable}`,
                        `その他(作業中など) ${r.other}`, r.lost ? `紛失 ${r.lost}` : null, `入荷中 ${r.incoming}`,
                    ].filter(Boolean).join('\n');
                    return (
                        <div key={r.product} className="flex items-center gap-2 text-xs" title={tip}>
                            <div className="w-28 shrink-0 truncate text-right text-slate-200 sm:w-48">{r.product}</div>
                            <div className="relative h-5 flex-1">
                                <div className="absolute inset-y-0 w-px bg-slate-600" style={{ left: 0 }} />
                                <div className="absolute inset-y-0.5 flex overflow-hidden rounded-sm" style={{ left: 0, width: pct(r.sent) }}>
                                    {SEGMENTS.map((s) => (r[s.key] ? (
                                        <div key={s.key} style={{ width: `${(r[s.key] / (r.sent || 1)) * 100}%`, background: s.color, minWidth: 1 }} />
                                    ) : null))}
                                </div>
                                {r.home > 0 && (
                                    <div className="absolute inset-y-0.5 rounded-sm" style={{ left: pct(r.sent), width: pct(r.home), border: `1px solid ${HOME_COLOR}` }} />
                                )}
                                {r.incoming > 0 && (
                                    <div className="absolute inset-y-0.5 rounded-sm" style={{ left: pct(r.sent + r.home), width: pct(r.incoming), border: `1px dashed ${INCOMING_COLOR}` }} />
                                )}
                            </div>
                            <div className="w-36 shrink-0 text-slate-300 sm:w-44">
                                {primary} <span className="text-slate-500">{rest.join(' ・ ')}</span>
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
