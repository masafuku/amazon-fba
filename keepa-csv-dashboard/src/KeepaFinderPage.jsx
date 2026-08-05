import { useEffect, useMemo, useState } from 'react';
import { Search } from 'lucide-react';
import { fetchKeepaProduct, loadKeepaFinderRun, searchKeepaProductFinder } from './db';

const formatNumber = (value) => {
    if (value === null || value === undefined) return '-';
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    return number.toLocaleString();
};

const formatPrice = (value, currency) => {
    if (value === null || value === undefined) return '-';
    return `${currency}${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

const getMarketPrice = (market) => market?.currentBuyBoxPrice ?? market?.currentNewPrice ?? market?.currentAmazonPrice;

const getProfitRate = (market) => {
    const price = getMarketPrice(market);
    if (!Number.isFinite(Number(price)) || Number(price) <= 0) return null;

    const referralFee = Number(market?.referralFeePercentage);
    const pickAndPackFee = Number(market?.fbaPickAndPackFee);
    if (!Number.isFinite(referralFee) && !Number.isFinite(pickAndPackFee)) return null;

    const referralFeeAmount = Number.isFinite(referralFee) ? Number(price) * (referralFee / 100) : 0;
    const fixedFee = Number.isFinite(pickAndPackFee) ? pickAndPackFee : 0;
    return ((Number(price) - referralFeeAmount - fixedFee) / Number(price)) * 100;
};

export default function KeepaFinderPage() {
    const [keyword, setKeyword] = useState('Japan Import');
    const [minNewPriceYen, setMinNewPriceYen] = useState(0);
    const [maxSalesRank, setMaxSalesRank] = useState(99999999);
    const [domain, setDomain] = useState(1);
    const [page, setPage] = useState(0);
    const [perPage, setPerPage] = useState(2000);
    const [stats, setStats] = useState(false);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [result, setResult] = useState(null);
    const [showConfirm, setShowConfirm] = useState(false);
    const [productDetails, setProductDetails] = useState({});
    const [productLoading, setProductLoading] = useState({});
    const [productErrors, setProductErrors] = useState({});
    const [productDebug, setProductDebug] = useState({});
    const [filterText, setFilterText] = useState('');
    const [excludeMissingPrices, setExcludeMissingPrices] = useState(false);
    const [excludeZeroSales, setExcludeZeroSales] = useState(false);
    const [onlyFetched, setOnlyFetched] = useState(false);
    const [sortKey, setSortKey] = useState('monthlySold');
    const [sortOrder, setSortOrder] = useState('desc');
    const [exchangeRate, setExchangeRate] = useState(() => {
        const storedRate = Number(window.localStorage.getItem('keepaExchangeRate'));
        return Number.isFinite(storedRate) && storedRate > 0 ? storedRate : 150;
    });

    useEffect(() => {
        const savedRunId = window.localStorage.getItem('keepaFinderRunId');
        if (!savedRunId) return;

        let cancelled = false;
        loadKeepaFinderRun(savedRunId)
            .then((savedResult) => {
                if (cancelled) return;
                setResult(savedResult);
                setProductDetails(savedResult.marketsByAsin || {});
                setProductDebug(savedResult.debugByAsin || {});
                setProductErrors(savedResult.errorsByAsin || {});
            })
            .catch((loadError) => {
                if (!cancelled) {
                    setError(`保存済みFinder結果の復元に失敗しました: ${loadError?.message || '不明なエラー'}`);
                }
            });

        return () => {
            cancelled = true;
        };
    }, []);

    const asinList = result?.keepa?.asinList || [];
    const totalResults = result?.keepa?.totalResults;
    const tokensLeft = result?.keepa?.tokensLeft;
    const tokensConsumed = result?.keepa?.tokensConsumed;
    const filteredAsins = useMemo(() => {
        const normalizedFilter = filterText.trim().toLowerCase();
        const getSortValue = (asin) => {
            const details = productDetails[asin] || {};
            const usPrice = getMarketPrice(details.US);
            const jpPrice = getMarketPrice(details.JP);
            if (sortKey === 'asin') return String(asin).toLowerCase();
            if (sortKey === 'priceDiffJpy' && usPrice !== null && usPrice !== undefined && jpPrice !== null && jpPrice !== undefined) {
                return usPrice * exchangeRate - jpPrice;
            }
            if (sortKey === 'usPrice') return usPrice ?? -Infinity;
            if (sortKey === 'jpPrice') return jpPrice ?? -Infinity;
            if (sortKey === 'usProfitRate') return getProfitRate(details.US) ?? -Infinity;
            if (sortKey === 'jpProfitRate') return getProfitRate(details.JP) ?? -Infinity;
            if (sortKey === 'usFee') {
                const referralFee = Number(details.US?.referralFeePercentage);
                const pickAndPackFee = Number(details.US?.fbaPickAndPackFee);
                if (!Number.isFinite(referralFee) && !Number.isFinite(pickAndPackFee)) return -Infinity;
                const referralFeeAmount = Number.isFinite(referralFee) && Number.isFinite(Number(usPrice))
                    ? Number(usPrice) * (referralFee / 100)
                    : 0;
                return referralFeeAmount + (Number.isFinite(pickAndPackFee) ? pickAndPackFee : 0);
            }
            return details.US?.monthlySold ?? -Infinity;
        };

        return asinList
            .filter((asin) => !normalizedFilter || String(asin).toLowerCase().includes(normalizedFilter))
            .filter((asin) => !onlyFetched || Boolean(productDetails[asin]))
            .filter((asin) => {
                if (!excludeMissingPrices) return true;
                const details = productDetails[asin] || {};
                return getMarketPrice(details.US) !== null && getMarketPrice(details.US) !== undefined
                    && getMarketPrice(details.JP) !== null && getMarketPrice(details.JP) !== undefined;
            })
            .filter((asin) => !excludeZeroSales || (productDetails[asin]?.US?.monthlySold ?? 0) > 0)
            .sort((left, right) => {
                const leftValue = getSortValue(left);
                const rightValue = getSortValue(right);
                if (leftValue === rightValue) return 0;
                if (typeof leftValue === 'string' || typeof rightValue === 'string') {
                    const difference = String(leftValue).localeCompare(String(rightValue), 'ja');
                    return sortOrder === 'asc' ? difference : -difference;
                }
                const difference = leftValue - rightValue;
                return sortOrder === 'asc' ? difference : -difference;
            });
    }, [asinList, filterText, onlyFetched, excludeMissingPrices, excludeZeroSales, productDetails, sortKey, sortOrder, exchangeRate]);
    const rawResponseText = useMemo(() => {
        if (!result) return '';
        try {
            return JSON.stringify(result, null, 2);
        } catch {
            return String(result);
        }
    }, [result]);

    const debugPayload = useMemo(() => ({
        title: keyword,
        productType: ['0'],
        current_AMAZON_gte: -1,
        current_AMAZON_lte: -1,
        current_BUY_BOX_SHIPPING_gte: 3000,
        monthlySoldPeak_gte: 10,
        sort: [['current_SALES', 'asc'], ['monthlySold', 'desc']],
        page: Number(page) || 0,
        perPage: Number(perPage) || 2000,
        domain: Number(domain) || 1,
        stats,
    }), [keyword, page, perPage, domain, stats]);

    const domainLabel = useMemo(() => (Number(domain) === 5 ? 'co.jp (5)' : 'com (1)'), [domain]);

    const debugCurl = useMemo(() => {
        const payloadJson = JSON.stringify(debugPayload).replace(/'/g, "'\\''");
        return `curl -X POST 'http://127.0.0.1:8001/api/keepa/product-finder' -H 'Content-Type: application/json' --data '${payloadJson}'`;
    }, [debugPayload]);

    const executeSearch = async () => {
        setLoading(true);
        setError('');

        try {
            const response = await searchKeepaProductFinder(debugPayload);
            setResult(response);
            if (response?.saved?.runId) {
                window.localStorage.setItem('keepaFinderRunId', response.saved.runId);
            }
            setProductDetails({});
            setProductErrors({});
            setProductDebug({});
            setFilterText('');
            setShowConfirm(false);
        } catch (searchError) {
            setError(searchError?.message || '検索に失敗しました。');
            setResult(null);
        } finally {
            setLoading(false);
        }
    };

    const fetchProduct = async (asin) => {
        const runId = result?.saved?.runId;
        if (!runId) {
            setProductErrors((current) => ({ ...current, [asin]: '検索結果のrunIdがありません。先にFinder検索を実行してください。' }));
            return;
        }

        setProductLoading((current) => ({ ...current, [asin]: true }));
        setProductErrors((current) => ({ ...current, [asin]: '' }));
        try {
            const response = await fetchKeepaProduct({
                asin,
                runId,
            });
            setProductDetails((current) => ({ ...current, [asin]: response.markets || response.fields || {} }));
            setProductDebug((current) => ({ ...current, [asin]: response.debug }));
        } catch (productError) {
            setProductErrors((current) => ({
                ...current,
                [asin]: productError?.message || 'Product API取得に失敗しました。',
            }));
            if (productError?.debug) {
                setProductDebug((current) => ({ ...current, [asin]: productError.debug }));
            }
        } finally {
            setProductLoading((current) => ({ ...current, [asin]: false }));
        }
    };

    const onSearch = (event) => {
        event.preventDefault();
        setError('');
        setShowConfirm(true);
    };

    const selectSortKey = (nextSortKey) => {
        if (sortKey === nextSortKey) {
            setSortOrder((current) => current === 'desc' ? 'asc' : 'desc');
            return;
        }
        setSortKey(nextSortKey);
        setSortOrder('desc');
    };

    const renderSortHeader = (label, key) => (
        <th className="px-4 py-3 font-medium text-slate-400">
            <button
                type="button"
                onClick={() => selectSortKey(key)}
                className="inline-flex items-center gap-1 whitespace-nowrap text-left hover:text-cyan-300"
            >
                {label}
                <span className="text-xs text-cyan-300" aria-hidden="true">
                    {sortKey === key ? (sortOrder === 'desc' ? '▼' : '▲') : '↕'}
                </span>
            </button>
        </th>
    );

    return (
        <div className="space-y-6">
            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
                    <div>
                        <p className="text-sm uppercase tracking-[0.2em] text-cyan-300">Keepa Product Finder</p>
                        <h1 className="text-2xl font-semibold text-white">キーワードで在庫切れ候補を検索</h1>
                        <p className="mt-1 text-sm text-slate-400">Amazon在庫切れ条件 + 新品価格下限 + 売れ筋ランキングで絞り込みます。詳細値はASINごとのボタン押下時に取得します。</p>
                        <p className="mt-1 text-xs text-amber-300">為替レート: 1 USD = {formatNumber(exchangeRate)} 円（CSV分析ページの設定を使用）</p>
                    </div>
                    <a
                        href="#dashboard"
                        className="inline-flex items-center justify-center rounded-xl bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-100 hover:bg-slate-700"
                    >
                        CSV分析ページへ
                    </a>
                </div>

                <form onSubmit={onSearch} className="grid gap-4 md:grid-cols-2">
                    <label className="block text-sm text-slate-300 md:col-span-2">
                        キーワード
                        <input
                            type="text"
                            value={keyword}
                            onChange={(event) => setKeyword(event.target.value)}
                            placeholder="例: pilot frixion"
                            className="mt-2 w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-3 text-white outline-none transition focus:border-cyan-400"
                        />
                    </label>

                    <label className="block text-sm text-slate-300">
                        新品の現在価格 下限 (円)
                        <input
                            type="number"
                            min="0"
                            step="1"
                            value={minNewPriceYen}
                            onChange={(event) => setMinNewPriceYen(event.target.value)}
                            className="mt-2 w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-3 text-white outline-none transition focus:border-cyan-400"
                        />
                    </label>

                    <label className="block text-sm text-slate-300">
                        売れ筋ランキング 上限
                        <input
                            type="number"
                            min="1"
                            step="1"
                            value={maxSalesRank}
                            onChange={(event) => setMaxSalesRank(event.target.value)}
                            className="mt-2 w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-3 text-white outline-none transition focus:border-cyan-400"
                        />
                    </label>

                    <label className="block text-sm text-slate-300">
                        ドメイン
                        <select
                            value={domain}
                            onChange={(event) => setDomain(Number(event.target.value) || 1)}
                            className="mt-2 w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-3 text-white outline-none transition focus:border-cyan-400"
                        >
                            <option value={1}>US (1)</option>
                            <option value={5}>JP (5)</option>
                        </select>
                    </label>

                    <label className="block text-sm text-slate-300">
                        perPage (50-10000)
                        <input
                            type="number"
                            min="50"
                            max="10000"
                            step="1"
                            value={perPage}
                            onChange={(event) => setPerPage(event.target.value)}
                            className="mt-2 w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-3 text-white outline-none transition focus:border-cyan-400"
                        />
                    </label>

                    <label className="block text-sm text-slate-300">
                        page (0開始)
                        <input
                            type="number"
                            min="0"
                            step="1"
                            value={page}
                            onChange={(event) => setPage(event.target.value)}
                            className="mt-2 w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-3 text-white outline-none transition focus:border-cyan-400"
                        />
                    </label>

                    <label className="flex items-center gap-3 text-sm text-slate-300 md:col-span-2">
                        <input
                            type="checkbox"
                            checked={stats}
                            onChange={(event) => setStats(event.target.checked)}
                            className="h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500"
                        />
                        Search Insights を含める（追加トークン消費）
                    </label>

                    <div className="md:col-span-2 flex flex-wrap items-center gap-3">
                        <button
                            type="submit"
                            disabled={loading || !String(keyword).trim()}
                            className="inline-flex items-center gap-2 rounded-xl bg-cyan-500 px-4 py-3 text-sm font-semibold text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                            <Search className="h-4 w-4" />
                            {loading ? '検索中...' : '送信前に確認'}
                        </button>
                        <p className="text-sm text-slate-400">
                            Keepa価格指定値: <span className="font-semibold text-slate-100">{formatNumber(minNewPriceKeepa)}</span>
                            {' '}(
                            {formatNumber(minNewPriceYen)}円 × 100)
                        </p>
                    </div>
                </form>

                {showConfirm ? (
                    <div className="mt-4 rounded-2xl border border-cyan-800/60 bg-cyan-950/20 p-4 text-sm text-cyan-100">
                        <p className="font-semibold text-cyan-200">送信前確認</p>
                        <p className="mt-1 text-xs text-cyan-100/80">以下のオプションで API に送信します。</p>
                        <pre className="mt-3 overflow-auto rounded-xl border border-cyan-900/60 bg-slate-950/80 p-3 text-xs text-cyan-100">{JSON.stringify(debugPayload, null, 2)}</pre>
                        <div className="mt-3 flex flex-wrap gap-2">
                            <button
                                type="button"
                                onClick={executeSearch}
                                disabled={loading}
                                className="rounded-xl bg-cyan-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-400 disabled:cursor-not-allowed disabled:opacity-50"
                            >
                                この内容で送信
                            </button>
                            <button
                                type="button"
                                onClick={() => setShowConfirm(false)}
                                disabled={loading}
                                className="rounded-xl bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-100 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
                            >
                                キャンセル
                            </button>
                        </div>
                    </div>
                ) : null}

                <div className="mt-4 rounded-2xl border border-slate-800 bg-slate-950/70 p-4 text-sm text-slate-300">
                    <p className="font-semibold text-white">適用している固定条件</p>
                    <ul className="mt-2 list-disc space-y-1 pl-5">
                        <li>domain: {domainLabel}</li>
                        <li>Amazon在庫切れ: availabilityAmazon = [1,2,3,4]</li>
                        <li>予約品除外: buyBoxIsPreorder = false</li>
                        <li>バックオーダー除外: buyBoxIsBackorder = false</li>
                        <li>売れ筋ランキング: current_SALES_lte</li>
                        <li>新品価格下限: current_NEW_gte</li>
                    </ul>
                </div>

                <div className="mt-4 rounded-2xl border border-amber-800/50 bg-amber-950/20 p-4 text-sm text-amber-100">
                    <p className="font-semibold text-amber-200">デバッグ: API送信オプション</p>
                    <p className="mt-2 text-xs text-amber-100/80">現在のフォームから API に送信される payload</p>
                    <pre className="mt-2 overflow-auto rounded-xl border border-amber-900/60 bg-slate-950/80 p-3 text-xs text-amber-100">{JSON.stringify(debugPayload, null, 2)}</pre>
                    <p className="mt-3 text-xs text-amber-100/80">同等の curl コマンド</p>
                    <pre className="mt-2 overflow-auto rounded-xl border border-amber-900/60 bg-slate-950/80 p-3 text-xs text-amber-100">{debugCurl}</pre>
                </div>

                {error ? (
                    <div className="mt-4 rounded-2xl border border-rose-800 bg-rose-950/40 px-4 py-3 text-sm text-rose-200">
                        {error}
                    </div>
                ) : null}
            </section>

            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="mb-4 grid gap-2 text-sm text-slate-300 md:grid-cols-4">
                    <p>ヒット件数: <span className="font-semibold text-cyan-300">{formatNumber(totalResults)}</span></p>
                    <p>取得ASIN数: <span className="font-semibold text-cyan-300">{formatNumber(asinList.length)}</span></p>
                    <p>消費トークン: <span className="font-semibold text-slate-100">{formatNumber(tokensConsumed)}</span></p>
                    <p>残トークン: <span className="font-semibold text-slate-100">{formatNumber(tokensLeft)}</span></p>
                </div>

                <div className="mb-4 rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                    <div className="grid gap-3 md:grid-cols-2">
                        <label className="block text-sm text-slate-400 md:col-span-2">
                            ASINフィルター
                            <input
                                type="search"
                                value={filterText}
                                onChange={(event) => setFilterText(event.target.value)}
                                placeholder="ASINを入力"
                                className="mt-2 w-full rounded-xl border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-white outline-none focus:border-cyan-400"
                            />
                        </label>
                        <label className="flex items-center gap-3 text-sm text-slate-400">
                            <input type="checkbox" checked={excludeMissingPrices} onChange={(event) => setExcludeMissingPrices(event.target.checked)} className="h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500" />
                            US/JP価格が揃っていない商品を除外
                        </label>
                        <label className="flex items-center gap-3 text-sm text-slate-400">
                            <input type="checkbox" checked={excludeZeroSales} onChange={(event) => setExcludeZeroSales(event.target.checked)} className="h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500" />
                            先月販売数0の商品を除外
                        </label>
                        <label className="flex items-center gap-3 text-sm text-slate-400">
                            <input type="checkbox" checked={onlyFetched} onChange={(event) => setOnlyFetched(event.target.checked)} className="h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500" />
                            詳細取得済みのみ表示
                        </label>
                        <select value={sortKey} onChange={(event) => setSortKey(event.target.value)} className="rounded-xl border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-white outline-none focus:border-cyan-400">
                            <option value="monthlySold">先月販売数順</option>
                            <option value="priceDiffJpy">価格差順</option>
                            <option value="usPrice">US価格順</option>
                            <option value="jpPrice">JP価格順</option>
                            <option value="usProfitRate">US利益率順</option>
                            <option value="jpProfitRate">JP利益率順</option>
                        </select>
                        <button type="button" onClick={() => setSortOrder((current) => current === 'desc' ? 'asc' : 'desc')} className="rounded-xl bg-slate-800 px-3 py-2 text-sm font-semibold text-slate-200">
                            並び替え: {sortOrder === 'desc' ? '高い順' : '低い順'}
                        </button>
                    </div>
                    <p className="mt-3 text-xs text-slate-500">表示件数: {formatNumber(filteredAsins.length)} / {formatNumber(asinList.length)}</p>
                </div>

                <div className="overflow-hidden rounded-2xl border border-slate-800">
                    <table className="min-w-full border-collapse text-left text-sm">
                        <thead className="bg-slate-950/90">
                            <tr>
                                <th className="px-4 py-3 font-medium text-slate-400">#</th>
                                {renderSortHeader('ASIN', 'asin')}
                                {renderSortHeader('先月販売数', 'monthlySold')}
                                {renderSortHeader('US価格', 'usPrice')}
                                {renderSortHeader('JP価格', 'jpPrice')}
                                {renderSortHeader('価格差 (円)', 'priceDiffJpy')}
                                {renderSortHeader('US利益率', 'usProfitRate')}
                                {renderSortHeader('JP利益率', 'jpProfitRate')}
                                {renderSortHeader('US手数料', 'usFee')}
                                <th className="px-4 py-3 font-medium text-slate-400">Product API</th>
                                <th className="px-4 py-3 font-medium text-slate-400">リンク</th>
                            </tr>
                        </thead>
                        <tbody>
                            {filteredAsins.map((asin, index) => (
                                <tr key={`${asin}-${index}`} className="border-t border-slate-800 bg-slate-950/80 hover:bg-slate-900">
                                    <td className="px-4 py-3 text-slate-300">{index + 1}</td>
                                    <td className="px-4 py-3 font-semibold text-white">{asin}</td>
                                    <td className="px-4 py-3 text-cyan-200">
                                        {productDetails[asin]?.US?.monthlySold === null || productDetails[asin]?.US?.monthlySold === undefined
                                            ? '-'
                                            : formatNumber(productDetails[asin].US.monthlySold)}
                                    </td>
                                    <td className="px-4 py-3 text-slate-200">
                                        {formatPrice(productDetails[asin]?.US?.currentBuyBoxPrice ?? productDetails[asin]?.US?.currentNewPrice, '$')}
                                    </td>
                                    <td className="px-4 py-3 text-slate-200">
                                        {formatPrice(productDetails[asin]?.JP?.currentBuyBoxPrice ?? productDetails[asin]?.JP?.currentNewPrice, '￥')}
                                    </td>
                                    <td className="px-4 py-3 text-amber-200">
                                        {Number.isFinite(Number(getMarketPrice(productDetails[asin]?.US))) && Number.isFinite(Number(getMarketPrice(productDetails[asin]?.JP)))
                                            ? formatPrice(
                                                getMarketPrice(productDetails[asin].US) * exchangeRate - getMarketPrice(productDetails[asin].JP),
                                                '￥',
                                            )
                                            : '-'}
                                    </td>
                                    <td className="px-4 py-3 text-emerald-300">
                                        {getProfitRate(productDetails[asin]?.US) === null ? '-' : `${getProfitRate(productDetails[asin]?.US).toFixed(1)}%`}
                                    </td>
                                    <td className="px-4 py-3 text-emerald-300">
                                        {getProfitRate(productDetails[asin]?.JP) === null ? '-' : `${getProfitRate(productDetails[asin]?.JP).toFixed(1)}%`}
                                    </td>
                                    <td className="px-4 py-3 text-slate-300">
                                        {productDetails[asin]?.US?.referralFeePercentage == null && productDetails[asin]?.US?.fbaPickAndPackFee == null
                                            ? '-'
                                            : `${productDetails[asin]?.US?.referralFeePercentage ?? '-'}% + ${formatPrice(productDetails[asin]?.US?.fbaPickAndPackFee, '$')}`}
                                    </td>
                                    <td className="px-4 py-3">
                                        <button
                                            type="button"
                                            onClick={() => fetchProduct(asin)}
                                            disabled={productLoading[asin]}
                                            className="rounded-lg bg-cyan-800/70 px-3 py-1 text-xs font-semibold text-cyan-100 hover:bg-cyan-700 disabled:cursor-not-allowed disabled:opacity-50"
                                        >
                                            {productLoading[asin] ? 'US/JP取得中...' : productDetails[asin] ? '再取得' : 'US/JP詳細取得'}
                                        </button>
                                        {productErrors[asin] ? (
                                            <p className="mt-1 max-w-xs text-xs text-rose-300">{productErrors[asin]}</p>
                                        ) : null}
                                        {productDebug[asin] ? (
                                            <details className="mt-2 max-w-md text-xs text-slate-400">
                                                <summary className="cursor-pointer text-amber-300">デバッグ: コマンドと戻り値</summary>
                                                <p className="mt-1">HTTP {productDebug[asin].status}</p>
                                                <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap rounded border border-slate-700 bg-slate-950 p-2">{productDebug[asin].command}</pre>
                                                <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded border border-slate-700 bg-slate-950 p-2">{productDebug[asin].responseText}</pre>
                                            </details>
                                        ) : null}
                                    </td>
                                    <td className="px-4 py-3 text-slate-300">
                                        <div className="flex flex-wrap gap-2">
                                            <a
                                                href={`https://www.amazon.com/dp/${asin}`}
                                                target="_blank"
                                                rel="noreferrer"
                                                className="rounded-lg bg-slate-800 px-3 py-1 text-xs font-semibold text-slate-100 hover:bg-slate-700"
                                            >
                                                Amazon US
                                            </a>
                                            <a
                                                href={`https://www.amazon.co.jp/dp/${asin}`}
                                                target="_blank"
                                                rel="noreferrer"
                                                className="rounded-lg bg-slate-800 px-3 py-1 text-xs font-semibold text-slate-100 hover:bg-slate-700"
                                            >
                                                Amazon JP
                                            </a>
                                            <a
                                                href={`https://keepa.com/#!product/5-${asin}`}
                                                target="_blank"
                                                rel="noreferrer"
                                                className="rounded-lg bg-cyan-900/60 px-3 py-1 text-xs font-semibold text-cyan-100 hover:bg-cyan-800/70"
                                            >
                                                Keepa
                                            </a>
                                        </div>
                                    </td>
                                </tr>
                            ))}
                            {filteredAsins.length === 0 ? (
                                <tr>
                                    <td className="px-4 py-6 text-slate-500" colSpan={12}>検索結果はまだありません。</td>
                                </tr>
                            ) : null}
                        </tbody>
                    </table>
                </div>
            </section>

            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="mb-3 flex items-center justify-between">
                    <p className="text-sm uppercase tracking-[0.2em] text-slate-400">Raw API Response</p>
                    <p className="text-xs text-slate-500">デバッグ用 生データ</p>
                </div>
                {rawResponseText ? (
                    <pre className="max-h-[480px] overflow-auto rounded-2xl border border-slate-800 bg-slate-950/90 p-4 text-xs text-slate-200">{rawResponseText}</pre>
                ) : (
                    <p className="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-6 text-sm text-slate-500">検索後に API の生レスポンスをここに表示します。</p>
                )}
            </section>
        </div>
    );
}
