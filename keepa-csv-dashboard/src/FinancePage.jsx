import { Fragment, useEffect, useMemo, useState } from 'react';
import { BarChart3, Package, RefreshCw, ShoppingCart, Truck } from 'lucide-react';
import {
    Bar,
    BarChart,
    CartesianGrid,
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

function SupplierLink({ name }) {
    if (!name) return <span>-</span>;
    return (
        <a href={`#supplier/${encodeURIComponent(name)}`} className="text-cyan-400 hover:underline">
            {name}
        </a>
    );
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

    const coveragePct = summary?.fixedCostCoveragePct;
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
                                        <td className="px-2 py-2"><AsinLink asin={item.asin} /></td>
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
                <h2 className="mb-3 text-sm font-semibold text-slate-300">商品別P&L(期間内)</h2>
                {products.length === 0 ? (
                    <p className="text-sm text-slate-500">注文データがありません。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="text-slate-400">
                                    <th className="px-2 py-2">ASIN</th>
                                    <th className="px-2 py-2">数量</th>
                                    <th className="px-2 py-2">売上</th>
                                    <th className="px-2 py-2">手数料</th>
                                    <th className="px-2 py-2">原価</th>
                                    <th className="px-2 py-2">純利益</th>
                                    <th className="px-2 py-2">利益率</th>
                                </tr>
                            </thead>
                            <tbody>
                                {products.map((row) => (
                                    <tr key={row.asin} className="border-t border-slate-800">
                                        <td className="px-2 py-2"><AsinLink asin={row.asin} /></td>
                                        <td className="px-2 py-2">{row.units}</td>
                                        <td className="px-2 py-2">{usd(row.revenueUsd)}</td>
                                        <td className="px-2 py-2">{usd(row.feesUsd)}</td>
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
                                    <th className="px-2 py-2">FBA Shipment ID</th>
                                    <th className="px-2 py-2">状態</th>
                                    <th className="px-2 py-2">納品先FC</th>
                                    <th className="px-2 py-2">納品期間</th>
                                    <th className="px-2 py-2">出荷数</th>
                                    <th className="px-2 py-2">販売数(概算)</th>
                                    <th className="px-2 py-2">原価</th>
                                    <th className="px-2 py-2">売上(概算)</th>
                                    <th className="px-2 py-2">純利益(概算)</th>
                                </tr>
                            </thead>
                            <tbody>
                                {shipments.map((row) => (
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
                                        <td className="px-2 py-2">{usd(row.costUsd)}</td>
                                        <td className="px-2 py-2">{usd(row.revenueUsd)}</td>
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
                                    <th className="px-2 py-2">発注日</th>
                                    <th className="px-2 py-2">仕入先</th>
                                    <th className="px-2 py-2">商品点数</th>
                                    <th className="px-2 py-2">合計数量</th>
                                    <th className="px-2 py-2">合計金額</th>
                                </tr>
                            </thead>
                            <tbody>
                                {purchaseOrders.map((po) => {
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
                                        <td className="px-2 py-2"><AsinLink asin={order.asin} /></td>
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
