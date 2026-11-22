import { Fragment, useEffect, useMemo, useState } from 'react';
import { ArrowLeft, Building2 } from 'lucide-react';

import { loadFinancePurchaseOrders, loadFinanceSupplierCandidates } from './db';

// AgentPage.jsx等と同じ定義(発注の優先度Tier、2026-09-28に旧・判定を統合)。
// このコードベースの既存パターンに合わせ、ページローカルにコピーを持つ。
const PRIORITY_TIER_STYLES = {
    S: 'bg-fuchsia-900/60 text-fuchsia-200',
    'A+': 'bg-emerald-800/70 text-emerald-100',
    'A-': 'bg-emerald-950/60 text-emerald-300',
    'B+': 'bg-sky-900/60 text-sky-200',
    'B-': 'bg-sky-950/60 text-sky-400',
    'C+': 'bg-amber-950/60 text-amber-300',
    'C-': 'bg-rose-950/60 text-rose-300',
    D: 'bg-slate-800 text-slate-500',
};

function AsinLink({ asin }) {
    if (!asin) return <span>-</span>;
    return (
        <a href={`#candidate/${encodeURIComponent(asin)}`} className="font-mono text-xs text-cyan-400 hover:underline">
            {asin}
        </a>
    );
}

function usd(value) {
    if (value === null || value === undefined) return '-';
    return `$${Number(value).toFixed(2)}`;
}

export default function SupplierDetailPage({ supplierName, onBack }) {
    const [purchaseOrders, setPurchaseOrders] = useState([]);
    const [expandedOrders, setExpandedOrders] = useState(() => new Set());
    const [purchasesLoading, setPurchasesLoading] = useState(true);
    const [purchasesError, setPurchasesError] = useState('');

    const [candidates, setCandidates] = useState([]);
    const [candidatesLoading, setCandidatesLoading] = useState(true);
    const [candidatesError, setCandidatesError] = useState('');

    useEffect(() => {
        let cancelled = false;
        setPurchasesLoading(true);
        setPurchasesError('');
        loadFinancePurchaseOrders(supplierName)
            .then((data) => { if (!cancelled) setPurchaseOrders(data); })
            .catch((loadError) => { if (!cancelled) setPurchasesError(loadError?.message || '仕入れ実績の読み込みに失敗しました。'); })
            .finally(() => { if (!cancelled) setPurchasesLoading(false); });
        return () => { cancelled = true; };
    }, [supplierName]);

    const toggleOrder = (key) => {
        setExpandedOrders((prev) => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key); else next.add(key);
            return next;
        });
    };

    useEffect(() => {
        let cancelled = false;
        setCandidatesLoading(true);
        setCandidatesError('');
        loadFinanceSupplierCandidates(supplierName)
            .then((data) => { if (!cancelled) setCandidates(data); })
            .catch((loadError) => { if (!cancelled) setCandidatesError(loadError?.message || '仕入れ候補の読み込みに失敗しました。'); })
            .finally(() => { if (!cancelled) setCandidatesLoading(false); });
        return () => { cancelled = true; };
    }, [supplierName]);

    const purchases = useMemo(() => purchaseOrders.flatMap((po) => po.items), [purchaseOrders]);
    const purchasedAsins = useMemo(() => new Set(purchases.map((p) => p.asin).filter(Boolean)), [purchases]);
    const totalAmountJpy = useMemo(() => purchaseOrders.reduce((sum, po) => sum + Number(po.totalAmountJpy || 0), 0), [purchaseOrders]);
    const orderCount = purchaseOrders.length;
    const lastOrderDate = useMemo(
        () => purchaseOrders.reduce((latest, po) => (!latest || po.orderDate > latest ? po.orderDate : latest), null),
        [purchaseOrders],
    );

    // 既に発注済みの候補は「仕入れ候補」欄で重複表示しない(仕入れ一覧側で確認できるため)。
    const unorderedCandidates = candidates.filter((c) => !purchasedAsins.has(c.asin));

    return (
        <div className="space-y-6">
            <button
                onClick={onBack}
                className="flex items-center gap-1 text-sm text-slate-400 hover:text-slate-200"
            >
                <ArrowLeft size={14} /> 収支ページに戻る
            </button>

            <h1 className="flex items-center gap-2 text-lg font-bold text-slate-100 sm:text-xl">
                <Building2 size={20} /> {supplierName}
            </h1>

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <StatCard label="累計仕入額" value={`¥${totalAmountJpy.toLocaleString('ja-JP')}`} />
                <StatCard label="発注件数" value={orderCount} />
                <StatCard label="取引商品数" value={purchasedAsins.size} />
                <StatCard label="直近の取引日" value={lastOrderDate || '-'} />
            </div>

            <div className="rounded-xl bg-slate-900 p-4">
                <h2 className="mb-1 text-sm font-semibold text-slate-300">仕入れ実績</h2>
                <p className="mb-3 text-xs text-slate-500">1回の発注を1伝票としてまとめています。行をクリックすると商品明細が開きます。</p>
                {purchasesError && <div className="mb-2 rounded-lg bg-rose-900/40 px-3 py-2 text-xs text-rose-200">{purchasesError}</div>}
                {purchasesLoading ? (
                    <p className="text-sm text-slate-500">読み込み中...</p>
                ) : purchaseOrders.length === 0 ? (
                    <p className="text-sm text-slate-500">この仕入先からの発注記録はありません。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="text-slate-400">
                                    <th className="px-2 py-2" />
                                    <th className="px-2 py-2">発注日</th>
                                    <th className="px-2 py-2">商品点数</th>
                                    <th className="px-2 py-2">合計数量</th>
                                    <th className="px-2 py-2">合計金額</th>
                                </tr>
                            </thead>
                            <tbody>
                                {purchaseOrders.map((po) => {
                                    const isOpen = expandedOrders.has(po.orderDate);
                                    return (
                                        <Fragment key={po.orderDate}>
                                            <tr
                                                className="cursor-pointer border-t border-slate-800 hover:bg-slate-800/40"
                                                onClick={() => toggleOrder(po.orderDate)}
                                            >
                                                <td className="px-2 py-2 text-slate-500">{isOpen ? '▾' : '▸'}</td>
                                                <td className="px-2 py-2 text-xs">{po.orderDate}</td>
                                                <td className="px-2 py-2">{po.itemCount}点</td>
                                                <td className="px-2 py-2">{po.totalQuantity}</td>
                                                <td className="px-2 py-2">¥{Number(po.totalAmountJpy).toLocaleString('ja-JP')}</td>
                                            </tr>
                                            {isOpen && (
                                                <tr className="border-t border-slate-800/50 bg-slate-950/40">
                                                    <td className="px-2 py-2" />
                                                    <td colSpan={4} className="px-2 py-2">
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
                <h2 className="mb-1 text-sm font-semibold text-slate-300">仕入れ候補(未発注)</h2>
                <p className="mb-3 text-xs text-slate-500">
                    キーワード検索でこの仕入れ先が卸価格の出所として見つかっている、まだ発注していない商品です。
                </p>
                {candidatesError && <div className="mb-2 rounded-lg bg-rose-900/40 px-3 py-2 text-xs text-rose-200">{candidatesError}</div>}
                {candidatesLoading ? (
                    <p className="text-sm text-slate-500">読み込み中...</p>
                ) : unorderedCandidates.length === 0 ? (
                    <p className="text-sm text-slate-500">未発注の仕入れ候補は見つかりませんでした。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="text-slate-400">
                                    <th className="px-2 py-2">ASIN</th>
                                    <th className="px-2 py-2">商品名</th>
                                    <th className="px-2 py-2">優先度</th>
                                    <th className="px-2 py-2">JP原価</th>
                                    <th className="px-2 py-2">US価格</th>
                                    <th className="px-2 py-2">粗利/個</th>
                                    <th className="px-2 py-2">ROI</th>
                                </tr>
                            </thead>
                            <tbody>
                                {unorderedCandidates.map((row) => (
                                    <tr key={row.asin} className="border-t border-slate-800">
                                        <td className="px-2 py-2"><AsinLink asin={row.asin} /></td>
                                        <td className="px-2 py-2 max-w-xs truncate" title={row.title}>{row.title}</td>
                                        <td className="px-2 py-2">
                                            {row.priorityTier ? (
                                                <span className={`rounded px-1.5 py-0.5 text-xs font-semibold ${PRIORITY_TIER_STYLES[row.priorityTier] || 'bg-slate-800 text-slate-400'}`}>
                                                    {row.priorityTier}
                                                </span>
                                            ) : '-'}
                                        </td>
                                        <td className="px-2 py-2">
                                            {row.jpCostJpy == null ? '-' : `¥${Number(row.jpCostJpy).toLocaleString('ja-JP')}`}
                                        </td>
                                        <td className="px-2 py-2">{usd(row.usPriceUsd)}</td>
                                        <td className="px-2 py-2">{usd(row.unitProfitUsd)}</td>
                                        <td className="px-2 py-2">
                                            {row.roiPct == null ? '-' : `${(row.roiPct * 100).toFixed(0)}%`}
                                        </td>
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

function StatCard({ label, value }) {
    return (
        <div className="rounded-xl bg-slate-900 p-3">
            <div className="text-xs text-slate-400">{label}</div>
            <div className="mt-1 text-lg font-bold text-slate-100">{value}</div>
        </div>
    );
}
