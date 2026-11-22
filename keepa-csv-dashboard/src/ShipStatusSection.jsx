import { Fragment, useEffect, useMemo, useState } from 'react';
import { loadShipStatus } from './db';
import { formatDateTime } from './formatters';

const usd = (v) => (v == null ? '-' : `${v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v) => (v == null ? '-' : `${(v * 100).toFixed(0)}%`);
const tone = (v) => (v == null ? '' : v < 0 ? 'text-rose-300' : 'text-emerald-300');

const STAGES = ['仕入れ先', '自宅', 'TNK', '国際輸送', 'FC受領中', '完了'];

// 便ごとの状況と収支。利益は送料前(手取り − 原価。国際送料・関税は引かない)。既存の「FBA納品便ごとのP&L」とは別の見方(便ごとの集計)。
export default function ShipStatusSection() {
    const [data, setData] = useState(null);
    const [error, setError] = useState('');
    const [open, setOpen] = useState(() => new Set());

    useEffect(() => {
        let cancelled = false;
        loadShipStatus()
            .then((d) => { if (!cancelled) setData(d); })
            .catch((e) => { if (!cancelled) setError(e?.message || '便ごとの状況と収支を読み込めませんでした。'); });
        return () => { cancelled = true; };
    }, []);

    const itemsByShip = useMemo(() => {
        const m = new Map();
        for (const it of data?.items ?? []) m.set(it.ship, [...(m.get(it.ship) ?? []), it]);
        return m;
    }, [data]);

    const toggle = (ship) => setOpen((prev) => {
        const next = new Set(prev);
        if (next.has(ship)) next.delete(ship); else next.add(ship);
        return next;
    });

    if (error) return <div className="rounded-xl bg-rose-900/40 px-4 py-3 text-sm text-rose-200">{error}</div>;
    if (!data) return <div className="rounded-xl bg-slate-900 p-4 text-sm text-slate-400">便ごとの状況と収支を読み込み中…</div>;
    if (!data.shipments.length) return <div className="rounded-xl bg-slate-900 p-4 text-sm text-slate-400">便のデータがまだありません。</div>;

    return (
        <div className="rounded-xl bg-slate-900 p-4">
            <div className="mb-3">
                <div className="text-sm font-semibold text-slate-200">便ごとの状況と収支</div>
                <div className="text-xs text-slate-400">
                    利益は送料前(手取り − 原価。国際送料・関税は含まない)。見込みは、残りを今の価格で売り切った場合。行をクリックすると商品ごとの内訳が開きます。
                </div>
            </div>
            <div className="overflow-x-auto">
                <table className="w-full min-w-[900px] text-left text-xs text-slate-300">
                    <thead className="text-slate-400">
                        <tr>
                            <th className="py-1 pr-3">便</th>
                            <th className="py-1 pr-3">状況</th>
                            <th className="py-1 pr-3 text-right">送った</th>
                            <th className="py-1 pr-3 text-right">受領</th>
                            <th className="py-1 pr-3 text-right">売れた</th>
                            <th className="py-1 pr-3 text-right">原価</th>
                            <th className="py-1 pr-3 text-right">売上(手取り)</th>
                            <th className="py-1 pr-3 text-right">利益(今)</th>
                            <th className="py-1 pr-3 text-right">回収率</th>
                            <th className="py-1 pr-3 text-right">見込み利益</th>
                            <th className="py-1 text-right">目標との差</th>
                        </tr>
                    </thead>
                    <tbody>
                        {data.shipments.map((s) => {
                            const isOpen = open.has(s.ship);
                            const its = itemsByShip.get(s.ship) ?? [];
                            return (
                                <Fragment key={s.ship}>
                                    <tr className="cursor-pointer border-t border-slate-800 hover:bg-slate-800/40" onClick={() => toggle(s.ship)}>
                                        <td className="py-1.5 pr-3 font-semibold text-slate-100">{isOpen ? '▾' : '▸'} {s.ship}</td>
                                        <td className="py-1.5 pr-3">
                                            {s.status}
                                            <span className="ml-1 text-slate-500">({STAGES[(s.stage ?? 1) - 1] ?? ''})</span>
                                        </td>
                                        <td className="py-1.5 pr-3 text-right">{s.units ?? '-'}</td>
                                        <td className="py-1.5 pr-3 text-right">{s.received ?? '-'}</td>
                                        <td className="py-1.5 pr-3 text-right">{s.sold ?? '-'}</td>
                                        <td className="py-1.5 pr-3 text-right">{usd(s.cost_usd)}</td>
                                        <td className="py-1.5 pr-3 text-right">{usd(s.proceeds_usd)}</td>
                                        <td className={`py-1.5 pr-3 text-right ${tone(s.profit_pre)}`}>{usd(s.profit_pre)}</td>
                                        <td className="py-1.5 pr-3 text-right">{pct(s.recovery_pre)}</td>
                                        <td className={`py-1.5 pr-3 text-right ${tone(s.projected_pre)}`}>
                                            {usd(s.projected_pre)}
                                            <span className="text-slate-500"> {s.proj_roi_pre != null ? `(ROI ${pct(s.proj_roi_pre)})` : ''}</span>
                                        </td>
                                        <td className={`py-1.5 text-right ${tone(s.diff_target_pre)}`}>{usd(s.diff_target_pre)}</td>
                                    </tr>
                                    {isOpen && (
                                        <tr className="bg-slate-950/40">
                                            <td colSpan={11} className="px-3 py-3">
                                                <div className="mb-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-slate-400 sm:grid-cols-4">
                                                    <div>発注 {s.d_order || '-'}</div>
                                                    <div>自宅着 {s.d_home || '-'}</div>
                                                    <div>TNK着 {s.d_tnk || '-'}</div>
                                                    <div>国際発送 {s.d_intl || '-'}</div>
                                                    <div>FC着 {s.d_fc || '-'}{s.fc ? `(${s.fc})` : ''}</div>
                                                    <div>完了 {s.d_done || '-'}</div>
                                                    <div>国際送料+関税(参考) {usd(s.intl_usd)}</div>
                                                    <div>{s.track_intl || ''}</div>
                                                </div>
                                                {s.note && <div className="mb-2 text-xs text-amber-200">{s.note}</div>}
                                                {its.length > 0 && (
                                                    <table className="w-full text-left text-xs">
                                                        <thead className="text-slate-500">
                                                            <tr>
                                                                <th className="py-1 pr-3">商品</th>
                                                                <th className="py-1 pr-3 text-right">送った</th>
                                                                <th className="py-1 pr-3 text-right">売れた</th>
                                                                <th className="py-1 pr-3 text-right">残り</th>
                                                                <th className="py-1 pr-3 text-right">紛失</th>
                                                                <th className="py-1 pr-3 text-right">原価</th>
                                                                <th className="py-1 pr-3 text-right">売上</th>
                                                                <th className="py-1 pr-3 text-right">利益(今)</th>
                                                                <th className="py-1 pr-3 text-right">1個の利益</th>
                                                                <th className="py-1 text-right">見込み利益</th>
                                                            </tr>
                                                        </thead>
                                                        <tbody>
                                                            {its.map((it) => (
                                                                <tr key={it.product} className="border-t border-slate-800">
                                                                    <td className="py-1 pr-3 text-slate-200">{it.product}</td>
                                                                    <td className="py-1 pr-3 text-right">{it.sent ?? '-'}</td>
                                                                    <td className="py-1 pr-3 text-right">{it.sold ?? '-'}</td>
                                                                    <td className="py-1 pr-3 text-right">{it.left ?? '-'}</td>
                                                                    <td className="py-1 pr-3 text-right">{it.lost || '-'}</td>
                                                                    <td className="py-1 pr-3 text-right">{usd(it.cost_usd)}</td>
                                                                    <td className="py-1 pr-3 text-right">{usd(it.proceeds_usd)}</td>
                                                                    <td className={`py-1 pr-3 text-right ${tone(it.profit_now)}`}>{usd(it.profit_now)}</td>
                                                                    <td className={`py-1 pr-3 text-right ${tone(it.unit_profit)}`}>{usd(it.unit_profit)}</td>
                                                                    <td className={`py-1 text-right ${tone(it.proj_before)}`}>{usd(it.proj_before)}</td>
                                                                </tr>
                                                            ))}
                                                        </tbody>
                                                    </table>
                                                )}
                                            </td>
                                        </tr>
                                    )}
                                </Fragment>
                            );
                        })}
                    </tbody>
                </table>
            </div>
            {data.generatedAt && (
                <div className="mt-2 text-right text-xs text-slate-500">集計 {formatDateTime(data.generatedAt)}(毎朝8時に更新)</div>
            )}
        </div>
    );
}
