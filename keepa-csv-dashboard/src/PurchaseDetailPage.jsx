import { useEffect, useState } from 'react';
import { ArrowLeft, ShoppingCart } from 'lucide-react';

import { loadFinancePurchaseDetail } from './db';

function AsinLink({ asin }) {
    if (!asin) return <span>-</span>;
    return (
        <a href={`#candidate/${encodeURIComponent(asin)}`} className="font-mono text-xs text-cyan-400 hover:underline">
            {asin}
        </a>
    );
}

export default function PurchaseDetailPage({ sdReceptionNo, onBack }) {
    const [purchase, setPurchase] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError('');
        loadFinancePurchaseDetail(sdReceptionNo)
            .then((data) => { if (!cancelled) setPurchase(data); })
            .catch((loadError) => { if (!cancelled) setError(loadError?.message || '仕入れ情報の読み込みに失敗しました。'); })
            .finally(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, [sdReceptionNo]);

    return (
        <div className="space-y-6">
            <button
                onClick={onBack}
                className="flex items-center gap-1 text-sm text-slate-400 hover:text-slate-200"
            >
                <ArrowLeft size={14} /> 収支ページに戻る
            </button>

            <h1 className="flex items-center gap-2 text-lg font-bold text-slate-100 sm:text-xl">
                <ShoppingCart size={20} /> 仕入れ詳細
            </h1>

            {error && <div className="rounded-xl bg-rose-900/40 px-4 py-3 text-sm text-rose-200">{error}</div>}
            {loading && <p className="text-sm text-slate-500">読み込み中...</p>}
            {!loading && !error && !purchase && (
                <p className="text-sm text-slate-500">該当する仕入れ記録が見つかりませんでした。</p>
            )}

            {purchase && (
                <div className="rounded-xl bg-slate-900 p-4">
                    <h2 className="mb-4 text-base font-semibold text-slate-200">{purchase.productName}</h2>
                    <dl className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2">
                        <Field label="受付番号" value={purchase.sdReceptionNo} />
                        <Field label="発注日" value={purchase.orderDate} />
                        <Field
                            label="仕入先"
                            value={
                                <a href={`#supplier/${encodeURIComponent(purchase.supplierName)}`} className="text-cyan-400 hover:underline">
                                    {purchase.supplierName}
                                </a>
                            }
                        />
                        <Field label="SD品番" value={purchase.sdProductNo} />
                        <Field label="JANコード" value={purchase.janCode} />
                        <Field label="ASIN" value={<AsinLink asin={purchase.asin} />} />
                        <Field label="内訳(バリエーション)" value={purchase.variant} />
                        <Field label="数量" value={purchase.quantity} />
                        <Field label="単価" value={`¥${Number(purchase.unitPriceJpy).toLocaleString('ja-JP')}`} />
                        <Field label="金額" value={`¥${Number(purchase.amountJpy).toLocaleString('ja-JP')}`} />
                        <Field
                            label="配分送料"
                            value={purchase.shippingCostJpy == null ? '未配分' : `¥${Number(purchase.shippingCostJpy).toLocaleString('ja-JP')}`}
                        />
                        <Field
                            label="着地原価/個"
                            value={`¥${(
                                Number(purchase.unitPriceJpy || 0) +
                                (purchase.shippingCostJpy && purchase.quantity ? purchase.shippingCostJpy / purchase.quantity : 0)
                            ).toLocaleString('ja-JP', { maximumFractionDigits: 1 })}`}
                        />
                    </dl>
                </div>
            )}
        </div>
    );
}

function Field({ label, value }) {
    return (
        <div>
            <dt className="text-xs text-slate-400">{label}</dt>
            <dd className="mt-0.5 text-sm text-slate-100">{value ?? '-'}</dd>
        </div>
    );
}
