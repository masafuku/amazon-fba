import { useEffect, useMemo, useState } from 'react';
import {
    Bar,
    CartesianGrid,
    ComposedChart,
    Legend,
    Line,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { loadSalesTrend } from './db';
import { formatDateTime } from './formatters';

const RANGES = [
    { key: 'all', label: '全期間', days: null },
    { key: '30', label: '直近30日', days: 30 },
    { key: '14', label: '直近14日', days: 14 },
];

const usd = (v) => (v == null ? '-' : `${v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);

// 売上と利益の推移。利益の定義は「送料前」(手取り − 原価。国際送料・関税は引かない)。
export default function SalesTrendSection() {
    const [data, setData] = useState(null);
    const [error, setError] = useState('');
    const [range, setRange] = useState('all');

    useEffect(() => {
        let cancelled = false;
        loadSalesTrend()
            .then((d) => { if (!cancelled) setData(d); })
            .catch((e) => { if (!cancelled) setError(e?.message || '売上と利益の推移を読み込めませんでした。'); });
        return () => { cancelled = true; };
    }, []);

    const rows = useMemo(() => {
        const all = data?.rows ?? [];
        const days = RANGES.find((r) => r.key === range)?.days;
        return days ? all.slice(-days) : all;
    }, [data, range]);

    const totals = useMemo(() => {
        const all = data?.rows ?? [];
        const last = all[all.length - 1];
        return last ? { sales: last.cum_sales, profit: last.cum_profit_pre } : null;
    }, [data]);

    if (error) return <div className="rounded-xl bg-rose-900/40 px-4 py-3 text-sm text-rose-200">{error}</div>;
    if (!data) return <div className="rounded-xl bg-slate-900 p-4 text-sm text-slate-400">売上と利益の推移を読み込み中…</div>;
    if (!rows.length) return <div className="rounded-xl bg-slate-900 p-4 text-sm text-slate-400">売上と利益の推移のデータがまだありません。</div>;

    return (
        <div className="rounded-xl bg-slate-900 p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                    <div className="text-sm font-semibold text-slate-200">売上と利益の推移</div>
                    <div className="text-xs text-slate-400">
                        利益は送料前(手取り − 原価。国際送料・関税は含まない)
                        {totals && ` ／ 累計 売上 ${usd(totals.sales)}・利益 ${usd(totals.profit)}`}
                    </div>
                </div>
                <div className="flex gap-2">
                    {RANGES.map((r) => (
                        <button
                            key={r.key}
                            onClick={() => setRange(r.key)}
                            className={`rounded-lg px-3 py-1.5 text-xs font-semibold ${range === r.key ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-300'}`}
                        >
                            {r.label}
                        </button>
                    ))}
                </div>
            </div>
            <div className="h-72 w-full">
                <ResponsiveContainer>
                    <ComposedChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                        <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
                        <XAxis dataKey="date" tick={{ fill: '#94a3b8', fontSize: 11 }} tickFormatter={(v) => v.slice(5)} />
                        <YAxis yAxisId="day" tick={{ fill: '#94a3b8', fontSize: 11 }} tickFormatter={(v) => `$${v}`} />
                        <YAxis yAxisId="cum" orientation="right" tick={{ fill: '#94a3b8', fontSize: 11 }} tickFormatter={(v) => `$${v}`} />
                        <Tooltip
                            contentStyle={{ background: '#0f172a', border: '1px solid #334155' }}
                            formatter={(v) => usd(v)}
                        />
                        <Legend />
                        <Bar yAxisId="day" dataKey="sales" name="日ごとの売上" fill="#38bdf8" />
                        <Bar yAxisId="day" dataKey="profit_pre" name="日ごとの利益" fill="#34d399" />
                        <Line yAxisId="cum" type="monotone" dataKey="cum_sales" name="累計売上" stroke="#7dd3fc" dot={false} strokeWidth={2} />
                        <Line yAxisId="cum" type="monotone" dataKey="cum_profit_pre" name="累計利益" stroke="#10b981" dot={false} strokeWidth={2} />
                    </ComposedChart>
                </ResponsiveContainer>
            </div>
            {data.generatedAt && (
                <div className="mt-2 text-right text-xs text-slate-500">集計 {formatDateTime(data.generatedAt)}(毎朝8時に更新)</div>
            )}
        </div>
    );
}
