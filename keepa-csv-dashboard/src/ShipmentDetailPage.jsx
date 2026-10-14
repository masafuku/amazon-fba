import { useEffect, useState } from 'react';
import { ArrowLeft, Truck } from 'lucide-react';
import { loadFinanceShipmentDetail } from './db';
import { formatDateTime } from './formatters';

function usd(value) {
    if (value === null || value === undefined) return '-';
    return `$${Number(value).toFixed(2)}`;
}

function AsinLink({ asin }) {
    if (!asin) return <span>-</span>;
    return (
        <a href={`#candidate/${encodeURIComponent(asin)}`} className="font-mono text-xs text-cyan-400 hover:underline">
            {asin}
        </a>
    );
}

export default function ShipmentDetailPage({ shipmentId, onBack }) {
    const [shipment, setShipment] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError('');
        loadFinanceShipmentDetail(shipmentId)
            .then((data) => { if (!cancelled) setShipment(data); })
            .catch((loadError) => { if (!cancelled) setError(loadError?.message || '納品便情報の読み込みに失敗しました。'); })
            .finally(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, [shipmentId]);

    return (
        <div className="space-y-6">
            <button
                onClick={onBack}
                className="flex items-center gap-1 text-sm text-slate-400 hover:text-slate-200"
            >
                <ArrowLeft size={14} /> 収支ページに戻る
            </button>

            <h1 className="flex items-center gap-2 text-lg font-bold text-slate-100 sm:text-xl">
                <Truck size={20} /> 納品便詳細
            </h1>

            {error && <div className="rounded-xl bg-rose-900/40 px-4 py-3 text-sm text-rose-200">{error}</div>}
            {loading && <p className="text-sm text-slate-500">読み込み中...</p>}
            {!loading && !error && !shipment && (
                <p className="text-sm text-slate-500">該当する納品便が見つかりませんでした。</p>
            )}

            {shipment && (
                <>
                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                        <InfoCard label="FBA Shipment ID" value={shipment.shipmentConfirmationId || shipment.shipmentId} />
                        <InfoCard label="状態" value={shipment.status} />
                        <InfoCard label="納品先FC" value={shipment.destinationFc} />
                        <InfoCard
                            label="納品期間"
                            value={
                                shipment.deliveryWindowStart
                                    ? `${formatDateTime(shipment.deliveryWindowStart)} 〜 ${formatDateTime(shipment.deliveryWindowEnd)}`
                                    : '-'
                            }
                        />
                    </div>

                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                        <InfoCard label="出荷数" value={shipment.unitsShipped} />
                        <InfoCard label="販売数(概算)" value={shipment.unitsSold} />
                        <InfoCard label="原価" value={usd(shipment.costUsd)} />
                        <InfoCard label="売上(概算)" value={usd(shipment.revenueUsd)} />
                        <InfoCard
                            label="純利益(概算)"
                            value={usd(shipment.netProfitUsd)}
                            emphasis={shipment.netProfitUsd >= 0 ? 'positive' : 'negative'}
                        />
                    </div>
                    <p className="text-xs text-slate-500">
                        販売数・売上・純利益は、どの納品便から売れたか正確には分からないため、FIFO(先入れ先出し)で
                        概算したものです。
                    </p>

                    <div className="rounded-xl bg-slate-900 p-4">
                        <h2 className="mb-3 text-sm font-semibold text-slate-300">出荷商品と仕入れ元</h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-left text-sm">
                                <thead>
                                    <tr className="text-slate-400">
                                        <th className="px-2 py-2">ASIN</th>
                                        <th className="px-2 py-2">SKU</th>
                                        <th className="px-2 py-2">出荷数量</th>
                                        <th className="px-2 py-2">紐づく仕入れ記録</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {(shipment.items || []).map((item) => {
                                        const purchases = shipment.purchasesByAsin?.[item.asin] || [];
                                        return (
                                            <tr key={item.asin} className="border-t border-slate-800 align-top">
                                                <td className="px-2 py-2"><AsinLink asin={item.asin} /></td>
                                                <td className="px-2 py-2 text-xs">{item.sku}</td>
                                                <td className="px-2 py-2">{item.quantity}</td>
                                                <td className="px-2 py-2">
                                                    {purchases.length === 0 ? (
                                                        <span className="text-xs text-slate-500">仕入れ記録が見つかりません</span>
                                                    ) : (
                                                        <ul className="space-y-1">
                                                            {purchases.map((p) => (
                                                                <li key={p.sdReceptionNo} className="text-xs">
                                                                    <a
                                                                        href={`#purchase/${encodeURIComponent(p.sdReceptionNo)}`}
                                                                        className="text-cyan-400 hover:underline"
                                                                    >
                                                                        {p.orderDate} {p.supplierName} ¥{Number(p.unitPriceJpy).toLocaleString('ja-JP')}×{p.quantity}
                                                                    </a>
                                                                </li>
                                                            ))}
                                                        </ul>
                                                    )}
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </>
            )}
        </div>
    );
}

function InfoCard({ label, value, emphasis }) {
    const emphasisClass =
        emphasis === 'positive' ? 'text-emerald-400' : emphasis === 'negative' ? 'text-rose-400' : 'text-slate-100';
    return (
        <div className="rounded-xl bg-slate-900 p-3">
            <div className="text-xs text-slate-400">{label}</div>
            <div className={`mt-1 text-sm font-bold ${emphasisClass}`}>{value ?? '-'}</div>
        </div>
    );
}
