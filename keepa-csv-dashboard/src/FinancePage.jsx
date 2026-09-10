import { useEffect, useMemo, useState } from 'react';
import { BarChart3, Package, RefreshCw } from 'lucide-react';
import {
    Bar,
    BarChart,
    CartesianGrid,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { loadFinanceInventory, loadFinanceOrders, loadFinanceSummary } from './db';
import { formatDateTime } from './formatters';

const PERIOD_OPTIONS = [
    { days: 7, label: '直近7日' },
    { days: 30, label: '直近30日' },
    { days: 90, label: '直近90日' },
];

function usd(value) {
    if (value === null || value === undefined) return '-';
    return `$${Number(value).toFixed(2)}`;
}

export default function FinancePage() {
    const [days, setDays] = useState(30);
    const [summary, setSummary] = useState(null);
    const [orders, setOrders] = useState([]);
    const [inventory, setInventory] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    const refresh = async () => {
        setLoading(true);
        setError('');
        try {
            const [summaryData, ordersData, inventoryData] = await Promise.all([
                loadFinanceSummary(days),
                loadFinanceOrders(days),
                loadFinanceInventory(),
            ]);
            setSummary(summaryData);
            setOrders(ordersData);
            setInventory(inventoryData);
        } catch (loadError) {
            setError(loadError?.message || '収支データの読み込みに失敗しました。');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        refresh();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [days]);

    const chartData = useMemo(() => {
        if (!summary) return [];
        return [
            { name: '売上', value: summary.revenueUsd },
            { name: '原価', value: -summary.cogsUsd },
            { name: '手数料', value: -summary.feesUsd },
            { name: '固定費', value: -summary.fixedCostUsd },
            { name: '純利益', value: summary.netProfitUsd },
        ];
    }, [summary]);

    // ASIN別の簡易内訳(この段階ではCOGSはjp_purchase_recordsのASIN紐付け待ちのため
    // 表示しない - 売上・数量のみ。詳細な損益内訳は今後の拡張余地)。
    const bySku = useMemo(() => {
        const map = new Map();
        for (const order of orders) {
            const key = order.asin || order.sku || '(不明)';
            const current = map.get(key) || { asin: key, quantity: 0, revenueUsd: 0 };
            current.quantity += order.quantity || 0;
            current.revenueUsd += (order.itemPriceUsd || 0) * (order.quantity || 0);
            map.set(key, current);
        }
        return [...map.values()].sort((a, b) => b.revenueUsd - a.revenueUsd);
    }, [orders]);

    const coveragePct = summary?.fixedCostCoveragePct;
    const isNoDataYet = summary && summary.orderCount === 0 && inventory.length === 0;

    return (
        <div className="space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <h1 className="flex items-center gap-2 text-lg font-bold text-slate-100 sm:text-xl">
                    <BarChart3 size={20} /> 収支
                </h1>
                <div className="flex items-center gap-2">
                    {PERIOD_OPTIONS.map((opt) => (
                        <button
                            key={opt.days}
                            onClick={() => setDays(opt.days)}
                            className={`rounded-lg px-3 py-1.5 text-xs font-semibold ${days === opt.days ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-300'}`}
                        >
                            {opt.label}
                        </button>
                    ))}
                    <button
                        onClick={refresh}
                        disabled={loading}
                        className="flex items-center gap-1 rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-semibold text-slate-300 disabled:opacity-50"
                    >
                        <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> 更新
                    </button>
                </div>
            </div>

            {error && (
                <div className="rounded-xl bg-rose-900/40 px-4 py-3 text-sm text-rose-200">{error}</div>
            )}

            {isNoDataYet && !loading && !error && (
                <div className="rounded-xl bg-slate-900 px-4 py-6 text-center text-sm text-slate-400">
                    まだデータがありません。SP-API(LWA_CLIENT_ID等)とGmail API(GMAIL_CLIENT_ID等)の
                    認証情報を.envに設定し、sp_api_sync.py / sd_email_parser.pyを実行すると
                    ここに実績が表示されます。
                </div>
            )}

            {summary && (
                <>
                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                        <SummaryCard label="売上" value={usd(summary.revenueUsd)} />
                        <SummaryCard label="原価(COGS)" value={usd(summary.cogsUsd)} />
                        <SummaryCard label="実手数料" value={usd(summary.feesUsd)} />
                        <SummaryCard label="固定費按分" value={usd(summary.fixedCostUsd)} />
                        <SummaryCard
                            label="純利益"
                            value={usd(summary.netProfitUsd)}
                            emphasis={summary.netProfitUsd >= 0 ? 'positive' : 'negative'}
                        />
                    </div>

                    <div className="rounded-xl bg-slate-900 p-4">
                        <div className="mb-2 flex items-center justify-between text-sm text-slate-300">
                            <span>固定費カバー率</span>
                            <span className="font-semibold">
                                {coveragePct === null || coveragePct === undefined ? '-' : `${coveragePct}%`}
                            </span>
                        </div>
                        <div className="h-3 w-full overflow-hidden rounded-full bg-slate-800">
                            <div
                                className={`h-full ${coveragePct >= 100 ? 'bg-emerald-400' : 'bg-cyan-500'}`}
                                style={{ width: `${Math.max(0, Math.min(100, coveragePct || 0))}%` }}
                            />
                        </div>
                    </div>

                    <div className="rounded-xl bg-slate-900 p-4">
                        <h2 className="mb-3 text-sm font-semibold text-slate-300">内訳(期間合計)</h2>
                        <ResponsiveContainer width="100%" height={220}>
                            <BarChart data={chartData}>
                                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                                <XAxis dataKey="name" stroke="#94a3b8" fontSize={12} />
                                <YAxis stroke="#94a3b8" fontSize={12} />
                                <Tooltip
                                    formatter={(value) => usd(value)}
                                    contentStyle={{ backgroundColor: '#0f172a', border: '1px solid #334155' }}
                                />
                                <Bar dataKey="value" fill="#22d3ee" radius={[4, 4, 0, 0]} />
                            </BarChart>
                        </ResponsiveContainer>
                    </div>
                </>
            )}

            <div className="rounded-xl bg-slate-900 p-4">
                <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-300">
                    <Package size={16} /> FBA在庫・在庫日数
                </h2>
                {inventory.length === 0 ? (
                    <p className="text-sm text-slate-500">在庫データがありません。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="text-slate-400">
                                    <th className="px-2 py-2">ASIN</th>
                                    <th className="px-2 py-2">SKU</th>
                                    <th className="px-2 py-2">在庫数</th>
                                    <th className="px-2 py-2">直近30日販売数</th>
                                    <th className="px-2 py-2">在庫日数</th>
                                </tr>
                            </thead>
                            <tbody>
                                {inventory.map((item) => (
                                    <tr key={item.asin} className="border-t border-slate-800">
                                        <td className="px-2 py-2 font-mono text-xs">{item.asin}</td>
                                        <td className="px-2 py-2 text-xs">{item.sku}</td>
                                        <td className="px-2 py-2">{item.fulfillableQuantity}</td>
                                        <td className="px-2 py-2">{item.soldLast30d}</td>
                                        <td className="px-2 py-2">
                                            {item.daysOfStock === null ? '-' : `${item.daysOfStock}日`}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            <div className="rounded-xl bg-slate-900 p-4">
                <h2 className="mb-3 text-sm font-semibold text-slate-300">ASIN別売上(期間内)</h2>
                {bySku.length === 0 ? (
                    <p className="text-sm text-slate-500">注文データがありません。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="text-slate-400">
                                    <th className="px-2 py-2">ASIN</th>
                                    <th className="px-2 py-2">数量</th>
                                    <th className="px-2 py-2">売上</th>
                                </tr>
                            </thead>
                            <tbody>
                                {bySku.map((row) => (
                                    <tr key={row.asin} className="border-t border-slate-800">
                                        <td className="px-2 py-2 font-mono text-xs">{row.asin}</td>
                                        <td className="px-2 py-2">{row.quantity}</td>
                                        <td className="px-2 py-2">{usd(row.revenueUsd)}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            <div className="rounded-xl bg-slate-900 p-4">
                <h2 className="mb-3 text-sm font-semibold text-slate-300">直近の注文</h2>
                {orders.length === 0 ? (
                    <p className="text-sm text-slate-500">注文データがありません。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="text-slate-400">
                                    <th className="px-2 py-2">注文日</th>
                                    <th className="px-2 py-2">ASIN</th>
                                    <th className="px-2 py-2">数量</th>
                                    <th className="px-2 py-2">金額</th>
                                    <th className="px-2 py-2">状態</th>
                                </tr>
                            </thead>
                            <tbody>
                                {orders.map((order) => (
                                    <tr key={order.orderId} className="border-t border-slate-800">
                                        <td className="px-2 py-2 text-xs">{formatDateTime(order.purchaseDate)}</td>
                                        <td className="px-2 py-2 font-mono text-xs">{order.asin || '-'}</td>
                                        <td className="px-2 py-2">{order.quantity}</td>
                                        <td className="px-2 py-2">{usd(order.itemPriceUsd)}</td>
                                        <td className="px-2 py-2 text-xs">{order.orderStatus}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>
        </div>
    );
}

function SummaryCard({ label, value, emphasis }) {
    const emphasisClass =
        emphasis === 'positive' ? 'text-emerald-400' : emphasis === 'negative' ? 'text-rose-400' : 'text-slate-100';
    return (
        <div className="rounded-xl bg-slate-900 p-3">
            <div className="text-xs text-slate-400">{label}</div>
            <div className={`mt-1 text-lg font-bold ${emphasisClass}`}>{value}</div>
        </div>
    );
}
