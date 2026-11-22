import { useEffect, useMemo, useState } from 'react';
import {
    Area,
    CartesianGrid,
    ComposedChart,
    Legend,
    Line,
    ReferenceLine,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { loadPlanComparison } from './db';
import { formatDateTime } from './formatters';

const PLAN_COLORS = { now: '#38bdf8', match: '#a78bfa', floor: '#fb923c', up10: '#34d399' };

const usd = (v) => (v == null ? '-' : `${v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v) => (v == null ? '-' : `${(v * 100).toFixed(0)}%`);

// 便ごとの価格プラン比較。利益は送料前(手取り − 原価 − 保管料。国際送料・関税は引かない)。
export default function PlanComparisonSection() {
    const [data, setData] = useState(null);
    const [error, setError] = useState('');
    const [ship, setShip] = useState('');
    const [bandPlan, setBandPlan] = useState('now');

    useEffect(() => {
        let cancelled = false;
        loadPlanComparison()
            .then((d) => {
                if (cancelled) return;
                setData(d);
                const first = d.plans?.[0]?.ship;
                if (first) setShip(first);
            })
            .catch((e) => { if (!cancelled) setError(e?.message || 'プラン比較を読み込めませんでした。'); });
        return () => { cancelled = true; };
    }, []);

    const ships = useMemo(() => [...new Set((data?.plans ?? []).map((p) => p.ship))], [data]);
    const plans = useMemo(() => (data?.plans ?? []).filter((p) => p.ship === ship), [data, ship]);

    // 実績(kind=actual)と各プランの予測を日付でそろえる
    const chartRows = useMemo(() => {
        const byDate = new Map();
        const put = (date, patch) => byDate.set(date, { date, ...byDate.get(date), ...patch });
        for (const r of data?.shipTrend ?? []) {
            if (r.ship !== ship) continue;
            if (r.kind === 'actual') {
                put(r.date, { actual: r.profit });
            } else {
                put(r.date, { [r.plan]: r.profit });
                if (r.plan === bandPlan && r.profit_lo != null && r.profit_hi != null) put(r.date, { band: [r.profit_lo, r.profit_hi] });
            }
        }
        const rows = [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
        // 実績の最後の点を各プランの出発点にして、線をつなぐ
        const lastActual = [...rows].reverse().find((r) => r.actual != null);
        if (lastActual) for (const p of plans) lastActual[p.plan] = lastActual[p.plan] ?? lastActual.actual;
        return rows;
    }, [data, ship, plans, bandPlan]);

    if (error) return <div className="rounded-xl bg-rose-900/40 px-4 py-3 text-sm text-rose-200">{error}</div>;
    if (!data) return <div className="rounded-xl bg-slate-900 p-4 text-sm text-slate-400">プラン比較を読み込み中…</div>;
    if (!ships.length) return <div className="rounded-xl bg-slate-900 p-4 text-sm text-slate-400">プラン比較のデータがまだありません。</div>;

    return (
        <div className="rounded-xl bg-slate-900 p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                    <div className="text-sm font-semibold text-slate-200">プラン比較(価格の付け方ごとの利益の見通し)</div>
                    <div className="text-xs text-slate-400">利益は送料前(手取り − 原価 − 保管料。国際送料・関税は含まない)。便ごとに、今日から先を予測します。</div>
                </div>
                <div className="flex gap-2">
                    {ships.map((s) => (
                        <button
                            key={s}
                            onClick={() => setShip(s)}
                            className={`rounded-lg px-3 py-1.5 text-xs font-semibold ${ship === s ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-300'}`}
                        >
                            {s}
                        </button>
                    ))}
                </div>
            </div>

            <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-left text-xs text-slate-300">
                    <thead className="text-slate-400">
                        <tr>
                            <th className="py-1 pr-3">プラン</th>
                            <th className="py-1 pr-3">損益分岐</th>
                            <th className="py-1 pr-3">売り切り</th>
                            <th className="py-1 pr-3 text-right">最終利益(幅)</th>
                            <th className="py-1 pr-3 text-right">ROI</th>
                            <th className="py-1 pr-3 text-right">目標との差</th>
                            <th className="py-1">目標に届く日</th>
                        </tr>
                    </thead>
                    <tbody>
                        {plans.map((p) => (
                            <tr key={p.plan} className="border-t border-slate-800">
                                <td className="py-1.5 pr-3 font-semibold" style={{ color: PLAN_COLORS[p.plan] }}>{p.plan_name}</td>
                                <td className="py-1.5 pr-3">{p.breakeven_date}</td>
                                <td className="py-1.5 pr-3">{p.sellout_date}</td>
                                <td className="py-1.5 pr-3 text-right">
                                    {usd(p.final_profit)}
                                    <span className="text-slate-500"> ({usd(p.final_lo)}〜{usd(p.final_hi)})</span>
                                </td>
                                <td className="py-1.5 pr-3 text-right">{pct(p.roi)}</td>
                                <td className={`py-1.5 pr-3 text-right ${p.diff_target != null && p.diff_target < 0 ? 'text-rose-300' : 'text-emerald-300'}`}>{usd(p.diff_target)}</td>
                                <td className="py-1.5">{p.target_date || '-'}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                累計利益の推移。帯は
                {plans.map((p) => (
                    <button
                        key={p.plan}
                        onClick={() => setBandPlan(p.plan)}
                        className={`rounded px-2 py-1 ${bandPlan === p.plan ? 'bg-slate-700 text-slate-100' : 'bg-slate-800 text-slate-400'}`}
                    >
                        {p.plan_name}
                    </button>
                ))}
                の幅
            </div>
            <div className="h-72 w-full">
                <ResponsiveContainer>
                    <ComposedChart data={chartRows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                        <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
                        <XAxis dataKey="date" tick={{ fill: '#94a3b8', fontSize: 11 }} tickFormatter={(v) => v.slice(5)} minTickGap={24} />
                        <YAxis tick={{ fill: '#94a3b8', fontSize: 11 }} tickFormatter={(v) => `$${v}`} />
                        <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #334155' }} formatter={(v) => (Array.isArray(v) ? `${usd(v[0])}〜${usd(v[1])}` : usd(v))} />
                        <Legend />
                        <ReferenceLine y={0} stroke="#64748b" />
                        <Area dataKey="band" name="予測の幅" stroke="none" fill={PLAN_COLORS[bandPlan]} fillOpacity={0.15} connectNulls />
                        <Line dataKey="actual" name="実績" stroke="#e2e8f0" dot={false} strokeWidth={2} connectNulls />
                        {plans.map((p) => (
                            <Line key={p.plan} dataKey={p.plan} name={p.plan_name} stroke={PLAN_COLORS[p.plan]} dot={false} strokeWidth={2} strokeDasharray="5 3" connectNulls />
                        ))}
                    </ComposedChart>
                </ResponsiveContainer>
            </div>
            {data.generatedAt && (
                <div className="mt-2 text-right text-xs text-slate-500">集計 {formatDateTime(data.generatedAt)}(毎朝8時に更新)</div>
            )}
        </div>
    );
}
