import { Fragment, useEffect, useMemo, useState } from 'react';
import { BarChart3, Package, RefreshCw, ShoppingCart, Truck } from 'lucide-react';
import {
    Bar,
    BarChart,
    CartesianGrid,
    Cell,
    LabelList,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import {
    loadFinanceInventory,
    loadFinanceOrders,
    loadFinancePurchaseOrders,
    loadFinanceProductPnl,
    loadFinanceShipments,
    loadFinanceSummary,
} from './db';
import { formatDateTime } from './formatters';

const API_BASE = import.meta.env.VITE_API_BASE || '';

const PERIOD_OPTIONS = [
    { days: 7, label: '直近7日' },
    { days: 30, label: '直近30日' },
    { days: 90, label: '直近90日' },
];

function usd(value) {
    if (value === null || value === undefined) return '-';
    return `$${Number(value).toFixed(2)}`;
}

// ASIN/仕入先名を、既存の商品詳細ページ(#candidate/{ASIN})・仕入先詳細ページ
// (#supplier/{名前})へのリンクにする小さなヘルパー。SellerDetailPage.jsx等と同じく、
// 共通モジュール化はせずページごとにローカルコピーを持つ既存の規約に合わせる。
function AsinLink({ asin }) {
    if (!asin) return <span>-</span>;
    return (
        <a href={`#candidate/${encodeURIComponent(asin)}`} className="font-mono text-xs text-cyan-400 hover:underline">
            {asin}
        </a>
    );
}

function NameLink({ asin, name }) {
    if (!asin) return <span>-</span>;
    return (
        <a
            href={`#candidate/${encodeURIComponent(asin)}`}
            title={name || asin}
            className="block max-w-[16rem] truncate text-cyan-400 hover:underline"
        >
            {name || asin}
        </a>
    );
}

function SupplierLink({ name }) {
    if (!name) return <span>-</span>;
    return (
        <a href={`#supplier/${encodeURIComponent(name)}`} className="text-cyan-400 hover:underline">
            {name}
        </a>
    );
}


// 見出しクリックで昇順/降順に並べ替える。値が無い行(null)は常に末尾。
function useSort(rows, defaultKey = null, defaultDir = 'desc') {
    const [sort, setSort] = useState({ key: defaultKey, dir: defaultDir });
    const sorted = useMemo(() => {
        if (!sort.key) return rows;
        const sign = sort.dir === 'asc' ? 1 : -1;
        return [...rows].sort((a, b) => {
            const x = a[sort.key];
            const y = b[sort.key];
            const xn = x === null || x === undefined || x === '';
            const yn = y === null || y === undefined || y === '';
            if (xn && yn) return 0;
            if (xn) return 1;
            if (yn) return -1;
            if (typeof x === 'number' && typeof y === 'number') return (x - y) * sign;
            return String(x).localeCompare(String(y), 'ja') * sign;
        });
    }, [rows, sort]);
    const th = (label, key) => (
        <th
            className="cursor-pointer select-none whitespace-nowrap px-2 py-2 hover:text-slate-200"
            onClick={() => setSort((p) => (p.key === key ? { key, dir: p.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' }))}
        >
            {label}{sort.key === key ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : ''}
        </th>
    );
    return { rows: sorted, th };
}

export default function FinancePage() {
    const [days, setDays] = useState(30);
    const [summary, setSummary] = useState(null);
    const [orders, setOrders] = useState([]);
    const [inventory, setInventory] = useState([]);
    const [products, setProducts] = useState([]);
    const [shipments, setShipments] = useState([]);
    const [purchaseOrders, setPurchaseOrders] = useState([]);
    const [expandedOrders, setExpandedOrders] = useState(() => new Set());
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [sync, setSync] = useState(null);

    const refresh = async () => {
        setLoading(true);
        setError('');
        try {
            const [summaryData, ordersData, inventoryData, productsData, shipmentsData, purchaseOrdersData] = await Promise.all([
                loadFinanceSummary(days),
                loadFinanceOrders(days),
                loadFinanceInventory(),
                loadFinanceProductPnl(days),
                loadFinanceShipments(),
                loadFinancePurchaseOrders(),
            ]);
            setSummary(summaryData);
            setOrders(ordersData);
            setInventory(inventoryData);
            setProducts(productsData);
            setShipments(shipmentsData);
            setPurchaseOrders(purchaseOrdersData);
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

    useEffect(() => {
        let timer;
        let cancelled = false;
        let wasRunning = false;
        const poll = async () => {
            try {
                const res = await fetch(`${API_BASE}/api/finance/sync`);
                const data = await res.json();
                if (cancelled) return;
                setSync(data);
                if (wasRunning && !data.running) refresh();
                wasRunning = !!data.running;
                timer = setTimeout(poll, data.running ? 4000 : 30000);
            } catch {
                if (!cancelled) timer = setTimeout(poll, 30000);
            }
        };
        poll();
        return () => { cancelled = true; clearTimeout(timer); };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const startSync = async () => {
        try {
            const res = await fetch(`${API_BASE}/api/finance/sync`, { method: 'POST' });
            const data = await res.json();
            setSync(data);
            if (!data.ok) setError(data.error || '更新を開始できませんでした。');
        } catch (e) {
            setError(e?.message || '更新を開始できませんでした。');
        }
    };

    // ウォーターフォール: 売上から項目を引いていき、小計(固定費前利益)・合計(純利益)は地面から立てる。
    const chartData = useMemo(() => {
        if (!summary) return [];
        const gross = summary.revenueUsd - summary.feesUsd - summary.cogsUsd;
        const steps = [
            { name: '売上', kind: 'up', delta: summary.revenueUsd },
            { name: '原価', kind: 'down', delta: -summary.cogsUsd },
            { name: '手数料', kind: 'down', delta: -summary.feesUsd },
            { name: '固定費前利益', kind: 'total', total: gross },
            { name: '固定費', kind: 'down', delta: -summary.fixedCostUsd },
            { name: '純利益', kind: 'total', total: summary.netProfitUsd },
        ];
        let running = 0;
        return steps.map((step) => {
            if (step.kind === 'total') {
                running = step.total;
                return { name: step.name, kind: 'total', amount: step.total, range: [Math.min(0, step.total), Math.max(0, step.total)] };
            }
            const from = running;
            running += step.delta;
            return { name: step.name, kind: step.kind, amount: step.delta, range: [Math.min(from, running), Math.max(from, running)] };
        });
    }, [summary]);

    const coveragePct = summary?.fixedCostCoveragePct;
    const invS = useSort(inventory);
    const prodS = useSort(products);
    const shipS = useSort(shipments);
    const poS = useSort(purchaseOrders);
    const ordS = useSort(orders);

    const isNoDataYet = summary && summary.orderCount === 0 && inventory.length === 0;

    const toggleOrder = (key) => {
        setExpandedOrders((prev) => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key); else next.add(key);
            return next;
        });
    };

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
                        onClick={startSync}
                        disabled={sync?.running}
                        className="flex items-center gap-1 rounded-lg bg-cyan-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-60"
                    >
                        <RefreshCw size={14} className={sync?.running ? 'animate-spin' : ''} /> {sync?.running ? 'API更新中…' : 'APIで更新'}
                    </button>
                    {sync && !sync.running && (sync.finishedAt || sync.lastLogAt) && (
                        <span className={`text-xs ${sync.ok === false ? 'text-rose-300' : 'text-slate-400'}`}>
                            {sync.ok === false ? '前回失敗' : '最終更新'} {new Date(sync.finishedAt || sync.lastLogAt).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                        </span>
                    )}
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
                        <SummaryCard
                            label={summary.estimatedRevenueUsd > 0 ? `売上(うち見込み ${usd(summary.estimatedRevenueUsd)})` : '売上'}
                            value={usd(summary.revenueUsd)}
                        />
                        <SummaryCard label="原価(COGS)" value={usd(summary.cogsUsd)} />
                        <SummaryCard
                            label={summary.estimatedFeesUsd > 0 ? `手数料(うち見込み ${usd(summary.estimatedFeesUsd)})` : '実手数料'}
                            value={usd(summary.feesUsd)}
                        />
                        <SummaryCard
                            label="固定費前利益"
                            value={usd(summary.revenueUsd - summary.feesUsd - summary.cogsUsd)}
                            emphasis={summary.revenueUsd - summary.feesUsd - summary.cogsUsd >= 0 ? 'positive' : 'negative'}
                        />
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
                        <h2 className="mb-3 text-sm font-semibold text-slate-300">固定費の内訳</h2>
                        {(summary.fixedCosts || []).length === 0 ? (
                            <p className="text-sm text-slate-500">固定費が登録されていません。</p>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead>
                                        <tr className="text-slate-400">
                                            <th className="px-2 py-2">項目</th>
                                            <th className="px-2 py-2">月額</th>
                                            <th className="px-2 py-2">期間按分</th>
                                            <th className="px-2 py-2">備考</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {summary.fixedCosts.map((item) => (
                                            <tr key={item.id} className="border-t border-slate-800">
                                                <td className="px-2 py-2">{item.name}</td>
                                                <td className="px-2 py-2">
                                                    ¥{Number(item.monthlyAmountJpy).toLocaleString('ja-JP')}
                                                </td>
                                                <td className="px-2 py-2">{usd(item.periodUsd)}</td>
                                                <td className="px-2 py-2 text-xs text-slate-500">{item.note || '-'}</td>
                                            </tr>
                                        ))}
                                        <tr className="border-t border-slate-700 font-semibold">
                                            <td className="px-2 py-2">合計</td>
                                            <td className="px-2 py-2">
                                                ¥{summary.fixedCosts
                                                    .reduce((sum, item) => sum + Number(item.monthlyAmountJpy), 0)
                                                    .toLocaleString('ja-JP')}
                                            </td>
                                            <td className="px-2 py-2">{usd(summary.fixedCostUsd)}</td>
                                            <td className="px-2 py-2" />
                                        </tr>
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </div>

                    <div className="rounded-xl bg-slate-900 p-4">
                        <h2 className="mb-1 text-sm font-semibold text-slate-300">内訳(期間合計)</h2>
                        <p className="mb-3 text-xs text-slate-500"><span className="text-blue-400">■</span> 増加 <span className="text-orange-400">■</span> 減少 <span className="text-slate-400">■</span> 小計・合計</p>
                        <ResponsiveContainer width="100%" height={220}>
                            <BarChart data={chartData}>
                                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                                <XAxis dataKey="name" stroke="#94a3b8" fontSize={12} />
                                <YAxis stroke="#94a3b8" fontSize={12} />
                                <Tooltip
                                    formatter={(value, name, item) => usd(item.payload.amount)}
                                    contentStyle={{ backgroundColor: '#0f172a', border: '1px solid #334155' }}
                                />
                                <Bar dataKey="range" radius={[3, 3, 0, 0]}>
                                    {chartData.map((d) => (
                                        <Cell key={d.name} fill={d.kind === 'up' ? '#3b82f6' : d.kind === 'down' ? '#f97316' : '#94a3b8'} />
                                    ))}
                                    <LabelList dataKey="amount" position="top" fill="#cbd5e1" fontSize={11} formatter={(v) => usd(v)} />
                                </Bar>
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
                                    {invS.th('商品名', 'productName')}
                                    {invS.th('SKU', 'sku')}
                                    {invS.th('アマゾン送付数', 'sentQuantity')}{invS.th('在庫数', 'fulfillableQuantity')}
                                    {invS.th('直近30日販売数', 'soldLast30d')}
                                    {invS.th('在庫日数', 'daysOfStock')}
                                </tr>
                            </thead>
                            <tbody>
                                {invS.rows.map((item) => (
                                    <tr key={item.asin} className="border-t border-slate-800">
                                        <td className="px-2 py-2"><NameLink asin={item.asin} name={item.productName} /></td>
                                        <td className="px-2 py-2 text-xs">{item.sku}</td>
                                        <td className="px-2 py-2">{item.sentQuantity}</td>
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
                <h2 className="mb-3 text-sm font-semibold text-slate-300">商品別P&L(期間内)</h2>
                {products.length === 0 ? (
                    <p className="text-sm text-slate-500">注文データがありません。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="text-slate-400">
                                    {prodS.th('商品名', 'productName')}
                                    {prodS.th('数量', 'units')}
                                    {prodS.th('売上', 'revenueUsd')}
                                    {prodS.th('手数料', 'feesUsd')}
                                    {prodS.th('原価', 'cogsUsd')}
                                    {prodS.th('純利益', 'netProfitUsd')}
                                    {prodS.th('利益率', 'marginPct')}
                                </tr>
                            </thead>
                            <tbody>
                                {prodS.rows.map((row) => (
                                    <tr key={row.asin} className="border-t border-slate-800">
                                        <td className="px-2 py-2"><NameLink asin={row.asin} name={row.productName} /></td>
                                        <td className="px-2 py-2">{row.units}</td>
                                        <td className="px-2 py-2">{usd(row.revenueUsd)}{row.estimatedRevenueUsd > 0 ? ' *' : ''}</td>
                                        <td className="px-2 py-2">{usd(row.feesUsd)}{row.estimatedFeesUsd > 0 ? ' *' : ''}</td>
                                        <td className="px-2 py-2">{usd(row.cogsUsd)}</td>
                                        <td className={`px-2 py-2 font-semibold ${row.netProfitUsd >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                            {usd(row.netProfitUsd)}
                                        </td>
                                        <td className="px-2 py-2">
                                            {row.marginPct === null || row.marginPct === undefined ? '-' : `${row.marginPct}%`}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            <div className="rounded-xl bg-slate-900 p-4">
                <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold text-slate-300">
                    <Truck size={16} /> FBA納品便ごとのP&L
                </h2>
                <p className="mb-3 text-xs text-slate-500">
                    どの納品便から売れたかは正確には分からないため、納品便を納品期間の古い順に並べ、
                    売上を数量ベースでFIFO(先入れ先出し)的に割り当てた概算です。行をクリックすると詳細を確認できます。
                </p>
                {shipments.length === 0 ? (
                    <p className="text-sm text-slate-500">納品便データがありません。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="text-slate-400">
                                    {shipS.th('FBA Shipment ID', 'shipmentConfirmationId')}
                                    {shipS.th('状態', 'status')}
                                    {shipS.th('納品先FC', 'destinationFc')}
                                    {shipS.th('納品期間', 'deliveryWindowStart')}
                                    {shipS.th('出荷数', 'unitsShipped')}
                                    {shipS.th('販売数(概算)', 'unitsSold')}
                                    {shipS.th('原価(うち国内送料)', 'costUsd')}
                                    {shipS.th('売上(概算)', 'revenueUsd')}
                                    {shipS.th('手数料(* 見込み含む)', 'feesUsd')}
                                    {shipS.th('国際送料・関税(* 見積り)', 'intlShippingUsd')}
                                    {shipS.th('純利益(概算)', 'netProfitUsd')}
                                </tr>
                            </thead>
                            <tbody>
                                {shipS.rows.map((row) => (
                                    <tr
                                        key={row.shipmentId}
                                        className="cursor-pointer border-t border-slate-800 hover:bg-slate-800/40"
                                        onClick={() => { window.location.hash = `#shipment/${encodeURIComponent(row.shipmentId)}`; }}
                                    >
                                        <td className="px-2 py-2 font-mono text-xs text-cyan-400 hover:underline">
                                            {row.shipmentConfirmationId || row.shipmentId}
                                        </td>
                                        <td className="px-2 py-2 text-xs">{row.status}</td>
                                        <td className="px-2 py-2 text-xs">{row.destinationFc}</td>
                                        <td className="px-2 py-2 text-xs">
                                            {row.deliveryWindowStart ? formatDateTime(row.deliveryWindowStart) : '-'}
                                            {row.deliveryWindowEnd ? ` 〜 ${formatDateTime(row.deliveryWindowEnd)}` : ''}
                                        </td>
                                        <td className="px-2 py-2">{row.unitsShipped}</td>
                                        <td className="px-2 py-2">{row.unitsSold}</td>
                                        <td className="px-2 py-2">{usd(row.costUsd)}<span className="text-xs text-slate-500"> ({usd(row.domesticShippingUsd)})</span></td>
                                        <td className="px-2 py-2">{usd(row.revenueUsd)}</td>
                                        <td className="px-2 py-2">{usd(row.feesUsd)}{row.estimatedFeesUsd > 0 ? ' *' : ''}</td>
                                        <td className="px-2 py-2">{usd((row.intlShippingUsd || 0) + (row.dutyUsd || 0))}{row.intlCostEstimated ? ' *' : ''}</td>
                                        <td className={`px-2 py-2 font-semibold ${row.netProfitUsd >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                            {usd(row.netProfitUsd)}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            <div className="rounded-xl bg-slate-900 p-4">
                <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold text-slate-300">
                    <ShoppingCart size={16} /> 仕入れ一覧
                </h2>
                <p className="mb-3 text-xs text-slate-500">
                    1回の発注(発注日＋仕入先)を1伝票としてまとめています。行をクリックすると商品明細が開きます。
                </p>
                {purchaseOrders.length === 0 ? (
                    <p className="text-sm text-slate-500">仕入れデータがありません。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="text-slate-400">
                                    <th className="px-2 py-2" />
                                    {poS.th('発注日', 'orderDate')}
                                    {poS.th('仕入先', 'supplierName')}
                                    {poS.th('商品点数', 'itemCount')}
                                    {poS.th('合計数量', 'totalQuantity')}
                                    {poS.th('合計金額', 'totalAmountJpy')}
                                </tr>
                            </thead>
                            <tbody>
                                {poS.rows.map((po) => {
                                    const key = `${po.orderDate}__${po.supplierName}`;
                                    const isOpen = expandedOrders.has(key);
                                    return (
                                        <Fragment key={key}>
                                            <tr
                                                className="cursor-pointer border-t border-slate-800 hover:bg-slate-800/40"
                                                onClick={() => toggleOrder(key)}
                                            >
                                                <td className="px-2 py-2 text-slate-500">{isOpen ? '▾' : '▸'}</td>
                                                <td className="px-2 py-2 text-xs">{po.orderDate}</td>
                                                <td className="px-2 py-2">
                                                    <span onClick={(e) => e.stopPropagation()}>
                                                        <SupplierLink name={po.supplierName} />
                                                    </span>
                                                </td>
                                                <td className="px-2 py-2">{po.itemCount}点</td>
                                                <td className="px-2 py-2">{po.totalQuantity}</td>
                                                <td className="px-2 py-2">¥{Number(po.totalAmountJpy).toLocaleString('ja-JP')}</td>
                                            </tr>
                                            {isOpen && (
                                                <tr key={`${key}-detail`} className="border-t border-slate-800/50 bg-slate-950/40">
                                                    <td className="px-2 py-2" />
                                                    <td colSpan={5} className="px-2 py-2">
                                                        <table className="w-full text-left text-xs">
                                                            <thead>
                                                                <tr className="text-slate-500">
                                                                    <th className="px-2 py-1">商品名</th>
                                                                    <th className="px-2 py-1">ASIN</th>
                                                                    <th className="px-2 py-1">数量</th>
                                                                    <th className="px-2 py-1">単価</th>
                                                                    <th className="px-2 py-1">金額</th>
                                                                </tr>
                                                            </thead>
                                                            <tbody>
                                                                {po.items.map((item) => (
                                                                    <tr key={item.sdReceptionNo} className="border-t border-slate-800/50">
                                                                        <td className="px-2 py-1">
                                                                            <a
                                                                                href={`#purchase/${encodeURIComponent(item.sdReceptionNo)}`}
                                                                                className="text-cyan-400 hover:underline"
                                                                            >
                                                                                {item.productName}
                                                                            </a>
                                                                        </td>
                                                                        <td className="px-2 py-1"><AsinLink asin={item.asin} /></td>
                                                                        <td className="px-2 py-1">{item.quantity}</td>
                                                                        <td className="px-2 py-1">¥{Number(item.unitPriceJpy).toLocaleString('ja-JP')}</td>
                                                                        <td className="px-2 py-1">¥{Number(item.amountJpy).toLocaleString('ja-JP')}</td>
                                                                    </tr>
                                                                ))}
                                                            </tbody>
                                                        </table>
                                                    </td>
                                                </tr>
                                            )}
                                        </Fragment>
                                    );
                                })}
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
                                    {ordS.th('注文日', 'purchaseDate')}
                                    {ordS.th('商品名', 'productName')}
                                    {ordS.th('数量', 'quantity')}
                                    {ordS.th('金額', 'itemPriceUsd')}
                                    {ordS.th('状態', 'orderStatus')}
                                </tr>
                            </thead>
                            <tbody>
                                {ordS.rows.map((order) => (
                                    <tr key={order.orderId} className="border-t border-slate-800">
                                        <td className="px-2 py-2 text-xs">{formatDateTime(order.purchaseDate)}</td>
                                        <td className="px-2 py-2"><NameLink asin={order.asin} name={order.productName} /></td>
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
