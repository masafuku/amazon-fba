import { useEffect, useMemo, useState } from 'react';
import { Star, Trash2 } from 'lucide-react';
import { deleteFavorite, fetchKeepaProduct, loadFavorites, saveFavorite } from './db';
import { getShippingPerItem, loadShippingSettings, saveShippingSettings } from './calculationSettings';

const EXCHANGE_RATE = 150;

const getMarketPrice = (market) => market?.currentBuyBoxPrice ?? market?.currentNewPrice ?? market?.currentAmazonPrice ?? null;

const getImageUrl = (favorite) => {
    const data = favorite.data || {};
    const source = data.US || data.JP || data;
    const raw = source.imageUrl
        ?? data.imageUrl
        ?? data['画像']
        ?? data['画像URL']
        ?? data.Image
        ?? data['Image URL']
        ?? data.image
        ?? data.imagesCSV
        ?? '';
    const imageValue = String(raw).split(';')[0].trim();
    if (!imageValue) return '';
    return imageValue.startsWith('http://') || imageValue.startsWith('https://')
        ? imageValue
        : `https://images-na.ssl-images-amazon.com/images/I/${imageValue}.jpg`;
};

const toNumber = (value, fallback = 0) => {
    if (value === null || value === undefined || value === '') return fallback;
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
};

const getCalculatedRow = (favorite, shippingCost) => {
    const data = favorite.data || {};
    const us = data.US || {};
    const jp = data.JP || {};
    const isFinderData = Boolean(data.US || data.JP);
    const isAgentData = favorite.source === 'agent';

    let title, category, usPrice, jpPrice, referralPercent, pickPackUsd, sales, amazonSeller;

    if (isFinderData) {
        title = us.title || jp.title;
        category = us.productCategory || jp.productCategory || '未分類';
        usPrice = getMarketPrice(us);
        jpPrice = getMarketPrice(jp);
        referralPercent = toNumber(us.referralFeePercentage, 15);
        pickPackUsd = toNumber(us.fbaPickAndPackFee, 0);
        sales = us.monthlySold;
        amazonSeller = us.amazonAvailability === 1;
    } else if (isAgentData) {
        // Researchエージェント(daily_scan.py)発見の候補。ops_finance.py が
        // 既に実質利益率まで計算済みだが、このページ独自の送料入力
        // (下の「1回の送料総額」欄)で再計算するため、そのための元データ
        // (USD価格・JPY原価・手数料額)だけを取り出す。
        title = data.title;
        category = data.category || '未分類';
        usPrice = data.us_price_usd ?? null;
        jpPrice = data.jp_cost_jpy ?? null;
        referralPercent = usPrice ? (toNumber(data.amazon_fee_usd, 0) / usPrice) * 100 : 15;
        pickPackUsd = toNumber(data.fba_fee_usd, 0);
        sales = null;
        amazonSeller = null;
    } else {
        title = data.title;
        category = data.productCategory || '未分類';
        usPrice = data.buyBoxUsd ?? (data.usPriceJpy == null ? null : toNumber(data.usPriceJpy) / EXCHANGE_RATE);
        jpPrice = data.jpCost ?? null;
        referralPercent = toNumber(data.referralPercent, 15);
        pickPackUsd = toNumber(data.fbaPickPackUsd, 0);
        sales = data.lastMonthSales;
        amazonSeller = Boolean(data.amazonSeller);
    }

    const usPriceJpy = usPrice === null || usPrice === undefined ? null : toNumber(usPrice) * EXCHANGE_RATE;
    const priceDiffJpy = usPriceJpy === null || jpPrice === null ? null : usPriceJpy - jpPrice;
    const amazonFee = usPriceJpy === null ? null : usPriceJpy * referralPercent / 100 + pickPackUsd * EXCHANGE_RATE;
    const profit = usPriceJpy === null || jpPrice === null || amazonFee === null || shippingCost === null
        ? null
        : usPriceJpy - jpPrice - amazonFee - shippingCost;

    return {
        title: favorite.title || title || data.title || '-',
        category,
        usPrice,
        usPriceJpy,
        jpPrice,
        priceDiffJpy,
        fee: amazonFee,
        sales,
        amazonSeller,
        profit,
        profitRate: profit !== null && usPriceJpy ? profit / usPriceJpy * 100 : null,
    };
};

export default function FavoritesPage() {
    const [favorites, setFavorites] = useState([]);
    const [error, setError] = useState('');
    const [loadingAsin, setLoadingAsin] = useState('');
    const [sortKey, setSortKey] = useState('profit');
    const [sortOrder, setSortOrder] = useState('desc');
    const [shippingTotal, setShippingTotal] = useState(() => loadShippingSettings().shippingTotal);
    const [purchaseQuantity, setPurchaseQuantity] = useState(() => loadShippingSettings().purchaseQuantity);

    const shippingCost = getShippingPerItem(shippingTotal, purchaseQuantity);

    useEffect(() => {
        saveShippingSettings(shippingTotal, purchaseQuantity);
    }, [shippingTotal, purchaseQuantity]);

    const refresh = async () => {
        try {
            setFavorites(await loadFavorites());
        } catch (loadError) {
            setError(loadError?.message || 'お気に入りの読み込みに失敗しました。');
        }
    };

    useEffect(() => {
        refresh();
    }, []);

    const remove = async (asin) => {
        try {
            await deleteFavorite(asin);
            setFavorites((current) => current.filter((favorite) => favorite.asin !== asin));
        } catch (deleteError) {
            setError(deleteError?.message || 'お気に入りの削除に失敗しました。');
        }
    };

    const refreshProduct = async (favorite) => {
        setLoadingAsin(favorite.asin);
        setError('');
        try {
            const response = await fetchKeepaProduct({ asin: favorite.asin });
            const updatedFavorite = {
                ...favorite,
                title: response.markets?.US?.title || response.markets?.JP?.title || favorite.title,
                data: response.markets || {},
            };
            await saveFavorite(updatedFavorite);
            setFavorites((current) => current.map((item) => item.asin === favorite.asin ? updatedFavorite : item));
        } catch (refreshError) {
            setError(`${favorite.asin} の再取得に失敗しました: ${refreshError?.message || '不明なエラー'}`);
        } finally {
            setLoadingAsin('');
        }
    };

    const rows = useMemo(() => {
        const calculatedRows = favorites.map((favorite) => ({ favorite, calculated: getCalculatedRow(favorite, shippingCost) }));
        return calculatedRows.sort((left, right) => {
            const leftValue = left.calculated[sortKey];
            const rightValue = right.calculated[sortKey];
            const leftMissing = leftValue === null || leftValue === undefined || leftValue === '';
            const rightMissing = rightValue === null || rightValue === undefined || rightValue === '';
            if (leftMissing || rightMissing) {
                if (leftMissing && rightMissing) return 0;
                return leftMissing ? 1 : -1;
            }
            const comparison = typeof leftValue === 'string'
                ? leftValue.localeCompare(String(rightValue), 'ja')
                : toNumber(leftValue) - toNumber(rightValue);
            return sortOrder === 'desc' ? -comparison : comparison;
        });
    }, [favorites, sortKey, sortOrder, shippingCost]);

    return (
        <main className="space-y-6">
            <header>
                <p className="text-sm uppercase tracking-[0.2em] text-amber-300">Favorites</p>
                <h1 className="mt-1 text-3xl font-semibold text-white">お気に入り</h1>
                <p className="mt-2 text-sm text-slate-400">FinderとCSV分析から登録した商品をまとめて管理できます。</p>
            </header>

            {error ? <p className="rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{error}</p> : null}

            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="mb-4 flex items-center gap-2 text-slate-300">
                    <Star className="h-5 w-5 text-amber-300" fill="currentColor" />
                    <span>{favorites.length}件</span>
                </div>
                <div className="mb-4 grid gap-3 rounded-2xl border border-slate-800 bg-slate-950/60 p-4 sm:grid-cols-2">
                    <label className="block text-sm text-slate-400">
                        1回の送料総額（円）
                        <input
                            type="number"
                            min="0"
                            step="1"
                            value={shippingTotal}
                            onChange={(event) => setShippingTotal(Number(event.target.value))}
                            className="mt-2 w-full rounded-xl border border-slate-700 bg-slate-900 px-3 py-2 text-white outline-none focus:border-cyan-400"
                        />
                    </label>
                    <label className="block text-sm text-slate-400">
                        仕入れ想定個数
                        <input
                            type="number"
                            min="1"
                            step="1"
                            value={purchaseQuantity}
                            onChange={(event) => setPurchaseQuantity(Number(event.target.value))}
                            className="mt-2 w-full rounded-xl border border-slate-700 bg-slate-900 px-3 py-2 text-white outline-none focus:border-cyan-400"
                        />
                    </label>
                    <p className="text-sm text-slate-400 sm:col-span-2">
                        商品1個あたりの想定送料: <span className="font-semibold text-white">{shippingCost === null ? '-' : `¥${shippingCost.toFixed(0)}`}</span>
                    </p>
                </div>
                {favorites.length === 0 ? (
                    <p className="py-12 text-center text-slate-500">お気に入りはまだありません。</p>
                ) : (
                    <div className="overflow-x-auto">
                        <div className="mb-4 flex flex-wrap items-center gap-2">
                            <span className="mr-1 text-sm text-slate-400">並び順</span>
                            {[
                                ['profit', '利益順'],
                                ['profitRate', '利益率順'],
                                ['priceDiffJpy', '差額順'],
                                ['sales', '先月売上順'],
                            ].map(([key, label]) => (
                                <button
                                    key={key}
                                    type="button"
                                    onClick={() => setSortKey(key)}
                                    className={`rounded-2xl px-4 py-2 text-sm font-semibold ${sortKey === key ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-300'}`}
                                >
                                    {label}
                                </button>
                            ))}
                            <button
                                type="button"
                                onClick={() => setSortOrder(sortOrder === 'desc' ? 'asc' : 'desc')}
                                className="rounded-2xl bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-200"
                            >
                                {sortOrder === 'desc' ? '高い順' : '低い順'}
                            </button>
                        </div>
                        <table className="min-w-full border-collapse text-left text-sm">
                            <thead className="bg-slate-950/90">
                                <tr>
                                    <th className="px-4 py-3 font-medium text-slate-400">画像</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">ASIN</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">商品名</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">商品カテゴリ</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">US価格($)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">US価格(円)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">JP価格(円)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">差額(円)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">手数料合計(円)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">先月売上</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">Amazonセラー</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">純利益(円)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">利益率(%)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">登録元</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">リンク</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows.map(({ favorite, calculated }) => (
                                    <tr key={favorite.asin} className="border-t border-slate-800 bg-slate-950/80">
                                        <td className="px-4 py-3">
                                            {getImageUrl(favorite) ? (
                                                <img
                                                    src={getImageUrl(favorite)}
                                                    alt={calculated.title || 'thumbnail'}
                                                    className="h-12 w-12 rounded-md border border-slate-700 object-cover"
                                                    loading="lazy"
                                                />
                                            ) : (
                                                <span className="text-slate-500">-</span>
                                            )}
                                        </td>
                                        <td className="px-4 py-3 font-semibold text-white">{favorite.asin}</td>
                                        <td className="max-w-xl px-4 py-3 text-slate-200">{calculated.title}</td>
                                        <td className="px-4 py-3 text-slate-300">{calculated.category}</td>
                                        <td className="px-4 py-3 text-slate-200">{calculated.usPrice === null ? '-' : `$${toNumber(calculated.usPrice).toFixed(2)}`}</td>
                                        <td className="px-4 py-3 text-slate-200">{calculated.usPriceJpy === null ? '-' : `¥${calculated.usPriceJpy.toFixed(0)}`}</td>
                                        <td className="px-4 py-3 text-slate-200">{calculated.jpPrice === null ? '-' : `¥${calculated.jpPrice.toFixed(0)}`}</td>
                                        <td className="px-4 py-3 text-amber-200">{calculated.priceDiffJpy === null ? '-' : `¥${calculated.priceDiffJpy.toFixed(0)}`}</td>
                                        <td className="px-4 py-3 text-slate-200">{calculated.fee === null ? '-' : `¥${calculated.fee.toFixed(0)}`}</td>
                                        <td className="px-4 py-3 text-cyan-200">{calculated.sales ?? '-'}</td>
                                        <td className="px-4 py-3 text-slate-200">{calculated.amazonSeller ? 'Yes' : 'No'}</td>
                                        <td className={`px-4 py-3 font-semibold ${calculated.profit >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{calculated.profit === null ? '-' : `¥${calculated.profit.toFixed(0)}`}</td>
                                        <td className={`px-4 py-3 font-semibold ${calculated.profitRate >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{calculated.profitRate === null ? '-' : `${calculated.profitRate.toFixed(1)}%`}</td>
                                        <td className="px-4 py-3 text-slate-400">
                                            {favorite.source === 'finder' ? 'Keepa Finder' : favorite.source === 'agent' ? '🤖 エージェント' : 'CSV分析'}
                                        </td>
                                        <td className="px-4 py-3">
                                            <div className="flex flex-wrap gap-2">
                                                <a href={`https://www.amazon.com/dp/${favorite.asin}`} target="_blank" rel="noreferrer" className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-700">US</a>
                                                <a href={favorite.data?.jp_url || `https://www.amazon.co.jp/dp/${favorite.asin}`} target="_blank" rel="noreferrer" className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-700">JP</a>
                                            </div>
                                        </td>
                                        <td className="px-4 py-3">
                                            <div className="flex flex-wrap gap-2">
                                                {favorite.source === 'agent' ? (
                                                    <span className="rounded-lg bg-slate-800/50 px-2 py-1 text-xs text-slate-500" title="US/JPでASINが異なるため、この画面からの再取得には対応していません">
                                                        再取得非対応
                                                    </span>
                                                ) : (
                                                    <button type="button" onClick={() => refreshProduct(favorite)} disabled={loadingAsin === favorite.asin} className="rounded-lg bg-cyan-800/70 px-2 py-1 text-xs font-semibold text-cyan-100 hover:bg-cyan-700 disabled:opacity-50">
                                                        {loadingAsin === favorite.asin ? '取得中...' : 'US/JP再取得'}
                                                    </button>
                                                )}
                                                <button type="button" onClick={() => remove(favorite.asin)} className="inline-flex items-center gap-1 rounded-lg bg-rose-900/70 px-2 py-1 text-xs font-semibold text-rose-200 hover:bg-rose-800">
                                                    <Trash2 className="h-3.5 w-3.5" />削除
                                                </button>
                                            </div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </section>
        </main>
    );
}
