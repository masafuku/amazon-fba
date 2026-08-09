import { useEffect, useMemo, useState } from 'react';
import { Star, Trash2 } from 'lucide-react';
import {
    addSellerToPool,
    deleteSellerFromPool,
    discoverSellersForAsin,
    expandFromSeller,
    loadAgentCandidates,
    loadAgentRuns,
    loadKeepaTokenStatus,
    loadSellerPool,
    saveFavorite,
} from './db';
import { formatDateTime } from './formatters';

const SELLER_SOURCE_LABEL = {
    manual: '手動追加',
    manual_expand: 'ダッシュボード発見',
    keyword_expansion: 'キーワード検索経由',
};

const EXCHANGE_RATE = 150;
const DEFAULT_MAX_SELLERS = 5;
const DEFAULT_MAX_CANDIDATES = 15;

export default function SellerMiningPage() {
    const [candidates, setCandidates] = useState([]);
    const [runs, setRuns] = useState([]);
    const [tokenStatus, setTokenStatus] = useState(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);
    const [savingAsin, setSavingAsin] = useState('');
    const [days, setDays] = useState(7);
    // CEOの明示的な希望: 「不合格でも良いので、日本からFBAで輸出してる商品
    // カテゴリと、売れている商品を探すため」 - AgentPageとは逆に、デフォルトで
    // 不合格候補も表示する。
    const [showRejected, setShowRejected] = useState(true);
    const [sortKey, setSortKey] = useState('marginPct');
    const [sortOrder, setSortOrder] = useState('desc');
    const [sellerFilter, setSellerFilter] = useState('');

    // 起点A: ASINからセラーを発見
    const [discoverAsin, setDiscoverAsin] = useState('');
    const [discoverMaxSellers, setDiscoverMaxSellers] = useState(DEFAULT_MAX_SELLERS);
    const [discoverConfirming, setDiscoverConfirming] = useState(false);
    const [discoverBusy, setDiscoverBusy] = useState(false);
    const [discoverResult, setDiscoverResult] = useState(null);

    // 起点B: セラーIDを直接指定して展開
    const [expandSellerId, setExpandSellerId] = useState('');
    const [expandSeedAsin, setExpandSeedAsin] = useState('');
    const [expandMaxCandidates, setExpandMaxCandidates] = useState(DEFAULT_MAX_CANDIDATES);
    const [expandConfirming, setExpandConfirming] = useState(false);
    const [expandBusy, setExpandBusy] = useState(false);
    const [expandResult, setExpandResult] = useState(null);

    // セラープール(定期セラーマイニングが巡回する対象。run_all_day.shの
    // 深夜時間帯にキーワード検索の代わりに1件ずつマイニングされる)。
    const [sellerPool, setSellerPool] = useState([]);
    const [sellerPoolLoading, setSellerPoolLoading] = useState(true);
    const [sellerPoolError, setSellerPoolError] = useState('');
    const [newPoolSellerId, setNewPoolSellerId] = useState('');
    const [addingToPool, setAddingToPool] = useState(false);
    const [deletingSellerId, setDeletingSellerId] = useState('');

    const refreshSellerPool = async () => {
        setSellerPoolLoading(true);
        setSellerPoolError('');
        try {
            setSellerPool(await loadSellerPool());
        } catch (poolError) {
            setSellerPoolError(poolError?.message || 'セラープールの読み込みに失敗しました。');
        } finally {
            setSellerPoolLoading(false);
        }
    };

    const refresh = async (period) => {
        setLoading(true);
        setError('');
        try {
            const [candidateList, runList] = await Promise.all([
                loadAgentCandidates(period),
                loadAgentRuns(Math.max(period, 30)),
            ]);
            setCandidates(candidateList);
            setRuns(runList);
        } catch (loadError) {
            setError(loadError?.message || 'セラーマイニング結果の読み込みに失敗しました。');
        } finally {
            setLoading(false);
        }
    };

    const refreshTokenStatus = async () => {
        try {
            setTokenStatus(await loadKeepaTokenStatus());
        } catch (tokenError) {
            setTokenStatus({ error: tokenError?.message || 'トークン残高の取得に失敗しました。' });
        }
    };

    useEffect(() => {
        refresh(days);
        refreshTokenStatus();
        refreshSellerPool();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [days]);

    const handleAddToPool = async (event) => {
        event.preventDefault();
        const sellerId = newPoolSellerId.trim();
        if (!sellerId) return;
        setAddingToPool(true);
        setSellerPoolError('');
        try {
            const result = await addSellerToPool(sellerId);
            if (!result.added) {
                setSellerPoolError(`セラー「${sellerId}」はすでにプールに存在します。`);
            }
            setNewPoolSellerId('');
            await refreshSellerPool();
        } catch (addError) {
            setSellerPoolError(addError?.message || 'セラーの追加に失敗しました。');
        } finally {
            setAddingToPool(false);
        }
    };

    const handleDeleteFromPool = async (sellerId) => {
        setDeletingSellerId(sellerId);
        setSellerPoolError('');
        try {
            await deleteSellerFromPool(sellerId);
            setSellerPool((current) => current.filter((item) => item.sellerId !== sellerId));
        } catch (deleteError) {
            setSellerPoolError(deleteError?.message || `「${sellerId}」の削除に失敗しました。`);
        } finally {
            setDeletingSellerId('');
        }
    };

    // seller_pool は times_mined昇順→last_mined_at昇順で返ってくる(pick_next_seller()と
    // 同じ並び順)ので、最初のactive行が次に定期マイニングで選ばれる。
    const nextPickSellerId = useMemo(
        () => sellerPool.find((item) => item.status === 'active')?.sellerId,
        [sellerPool]
    );

    const selectSortKey = (nextSortKey) => {
        if (sortKey === nextSortKey) {
            setSortOrder((current) => (current === 'desc' ? 'asc' : 'desc'));
            return;
        }
        setSortKey(nextSortKey);
        setSortOrder('desc');
    };

    const renderSortHeader = (label, key) => (
        <th key={key} className="px-4 py-3 font-medium text-slate-400">
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

    const addToFavorites = async (candidate) => {
        setSavingAsin(candidate.asin);
        setError('');
        try {
            await saveFavorite({
                asin: candidate.asin,
                title: candidate.title,
                source: 'seller-mining',
                data: {
                    ...candidate.data,
                    category: candidate.category,
                    sellerId: candidate.sellerId,
                    sellerName: candidate.sellerName,
                    seedAsin: candidate.seedAsin,
                },
            });
            setCandidates((current) =>
                current.map((item) => (item.asin === candidate.asin ? { ...item, alreadyFavorited: true } : item))
            );
        } catch (saveError) {
            setError(`${candidate.asin} のお気に入り登録に失敗しました: ${saveError?.message || '不明なエラー'}`);
        } finally {
            setSavingAsin('');
        }
    };

    // セラーマイニング由来の候補だけに絞り込む(このページの対象)。
    const sellerCandidates = useMemo(
        () => candidates.filter((item) => item.sourceType === 'seller'),
        [candidates]
    );

    const sellerSummary = useMemo(() => {
        const bySeller = new Map();
        for (const c of sellerCandidates) {
            const key = c.sellerId || c.sellerName || 'unknown';
            const entry = bySeller.get(key) || {
                key, sellerId: c.sellerId, sellerName: c.sellerName || c.sellerId || '(不明)',
                total: 0, qualified: 0, seedAsins: new Set(),
            };
            entry.total += 1;
            if (c.qualified) entry.qualified += 1;
            if (c.seedAsin) entry.seedAsins.add(c.seedAsin);
            bySeller.set(key, entry);
        }
        return [...bySeller.values()]
            .map((e) => ({ ...e, rate: e.total ? e.qualified / e.total : 0, seedAsins: [...e.seedAsins] }))
            .sort((a, b) => b.rate - a.rate || b.total - a.total);
    }, [sellerCandidates]);

    const visibleCandidates = useMemo(() => {
        let base = showRejected ? sellerCandidates : sellerCandidates.filter((item) => item.qualified);
        if (sellerFilter) {
            base = base.filter((item) => (item.sellerId || item.sellerName) === sellerFilter);
        }
        return [...base].sort((left, right) => {
            const leftValue = sortKey === 'createdAt' ? Date.parse(left.createdAt) || 0 : left[sortKey];
            const rightValue = sortKey === 'createdAt' ? Date.parse(right.createdAt) || 0 : right[sortKey];
            const leftMissing = leftValue === null || leftValue === undefined || leftValue === '';
            const rightMissing = rightValue === null || rightValue === undefined || rightValue === '';
            if (leftMissing || rightMissing) {
                if (leftMissing && rightMissing) return 0;
                return leftMissing ? 1 : -1;
            }
            const comparison = typeof leftValue === 'string'
                ? leftValue.localeCompare(String(rightValue), 'ja')
                : Number(leftValue) - Number(rightValue);
            return sortOrder === 'desc' ? -comparison : comparison;
        });
    }, [sellerCandidates, showRejected, sellerFilter, sortKey, sortOrder]);

    const qualifiedCount = useMemo(() => sellerCandidates.filter((item) => item.qualified).length, [sellerCandidates]);

    const runningRun = useMemo(
        () => runs.find((run) => run.status === 'running' && run.sourceType === 'seller'),
        [runs]
    );

    const handleDiscover = async () => {
        setDiscoverBusy(true);
        setError('');
        try {
            const result = await discoverSellersForAsin({ asin: discoverAsin.trim().toUpperCase(), maxSellers: discoverMaxSellers });
            if (!result.ok) {
                setError(result.error || 'セラーの発見に失敗しました。');
            }
            setDiscoverResult(result);
        } catch (discoverError) {
            setError(discoverError?.message || 'セラーの発見に失敗しました。');
        } finally {
            setDiscoverBusy(false);
            setDiscoverConfirming(false);
        }
    };

    const handleExpand = async () => {
        setExpandBusy(true);
        setError('');
        try {
            const result = await expandFromSeller({
                sellerId: expandSellerId.trim(),
                maxCandidates: expandMaxCandidates,
                seedAsin: expandSeedAsin.trim() || undefined,
            });
            if (!result.ok) {
                setError(result.error || 'セラー出品の評価に失敗しました。');
            } else {
                await refresh(days);
            }
            setExpandResult(result);
        } catch (expandError) {
            setError(expandError?.message || 'セラー出品の評価に失敗しました。');
        } finally {
            setExpandBusy(false);
            setExpandConfirming(false);
        }
    };

    const useSellerForExpand = (sellerId, seedAsin) => {
        setExpandSellerId(sellerId);
        if (seedAsin) setExpandSeedAsin(seedAsin);
        setExpandResult(null);
        const el = document.getElementById('seller-mining-expand-form');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };

    const useAsinForDiscover = (asin) => {
        setDiscoverAsin(asin);
        setDiscoverResult(null);
        const el = document.getElementById('seller-mining-discover-form');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };

    return (
        <main className="space-y-6">
            <header className="flex flex-wrap items-start justify-between gap-4">
                <div>
                    <p className="text-sm uppercase tracking-[0.2em] text-cyan-300">Seller Mining</p>
                    <h1 className="mt-1 text-3xl font-semibold text-white">🕵️ セラーマイニング</h1>
                    <p className="mt-2 text-sm text-slate-400">
                        合格候補のセラーが他に何を売っているかを調べて候補を広げます。
                        「よく売れている日本のものを売っているセラーは、他にも同じようなものを売っていることが多い」
                        という考え方に基づく手法です。不合格候補も、カテゴリ傾向・販売数を見るためにデフォルトで表示しています。
                    </p>
                </div>
                <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm">
                    <p className="text-slate-400">Keepaトークン残高</p>
                    {tokenStatus?.error ? (
                        <p className="text-rose-300">{tokenStatus.error}</p>
                    ) : (
                        <>
                            <p className="text-xl font-semibold text-white">{tokenStatus?.tokensLeft ?? '-'}</p>
                            <p className="text-xs text-slate-500">
                                {tokenStatus?.refillRate != null ? `${tokenStatus.refillRate}トークン/分で回復` : ''}
                            </p>
                        </>
                    )}
                    <button
                        type="button"
                        onClick={refreshTokenStatus}
                        className="mt-2 rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-700"
                    >
                        更新
                    </button>
                </div>
            </header>

            {error ? (
                <p className="rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{error}</p>
            ) : null}

            {runningRun ? (
                <div className="flex items-center gap-3 rounded-2xl border border-cyan-800 bg-cyan-950/30 px-4 py-3">
                    <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-cyan-400" aria-hidden="true" />
                    <p className="text-sm text-cyan-100">🔄 セラーマイニング実行中: {runningRun.sellerName || runningRun.keyword}</p>
                </div>
            ) : null}

            {/* 起点A: ASINからセラーを発見 */}
            <section id="seller-mining-discover-form" className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <h2 className="mb-1 text-lg font-semibold text-white">① ASINからセラーを発見</h2>
                <p className="mb-4 text-sm text-slate-400">
                    合格候補のASINを入れると、「他のセラー」欄に相当する出品セラー一覧を取得します(約7トークン)。
                </p>
                <div className="flex flex-wrap items-end gap-3">
                    <label className="text-sm text-slate-400">
                        ASIN
                        <input
                            type="text"
                            value={discoverAsin}
                            onChange={(event) => setDiscoverAsin(event.target.value)}
                            placeholder="B0XXXXXXXX"
                            className="mt-1 block w-48 rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-white outline-none focus:border-cyan-400"
                        />
                    </label>
                    <label className="text-sm text-slate-400">
                        最大セラー数
                        <input
                            type="number"
                            min={1}
                            max={10}
                            value={discoverMaxSellers}
                            onChange={(event) => setDiscoverMaxSellers(Number(event.target.value) || DEFAULT_MAX_SELLERS)}
                            className="mt-1 block w-24 rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-white outline-none focus:border-cyan-400"
                        />
                    </label>
                    <button
                        type="button"
                        onClick={() => setDiscoverConfirming(true)}
                        disabled={!discoverAsin.trim() || discoverBusy}
                        className="rounded-xl bg-cyan-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                    >
                        セラーを発見
                    </button>
                </div>

                {discoverConfirming ? (
                    <div className="mt-4 rounded-2xl border border-cyan-800 bg-cyan-950/20 p-4 text-sm">
                        <p className="text-cyan-100">
                            ASIN <span className="font-semibold">{discoverAsin.trim().toUpperCase()}</span> の出品セラーを最大{discoverMaxSellers}件取得します。
                            想定コスト: <span className="font-semibold">約7トークン</span>(現在残高: {tokenStatus?.tokensLeft ?? '-'})
                        </p>
                        <div className="mt-3 flex gap-2">
                            <button
                                type="button"
                                onClick={handleDiscover}
                                disabled={discoverBusy}
                                className="rounded-lg bg-cyan-500 px-3 py-1.5 text-xs font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                            >
                                {discoverBusy ? '実行中...' : 'この内容で送信'}
                            </button>
                            <button
                                type="button"
                                onClick={() => setDiscoverConfirming(false)}
                                disabled={discoverBusy}
                                className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-700"
                            >
                                キャンセル
                            </button>
                        </div>
                    </div>
                ) : null}

                {discoverResult ? (
                    discoverResult.ok && discoverResult.found ? (
                        <div className="mt-4 space-y-2">
                            <p className="text-sm text-slate-300">セラー{discoverResult.sellerIds.length}件を発見しました:</p>
                            <ul className="space-y-1">
                                {discoverResult.sellerIds.map((sellerId) => (
                                    <li key={sellerId} className="flex items-center justify-between rounded-lg bg-slate-950/60 px-3 py-2">
                                        <span className="font-mono text-sm text-slate-200">{sellerId}</span>
                                        <button
                                            type="button"
                                            onClick={() => useSellerForExpand(sellerId, discoverResult.asin)}
                                            className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-cyan-300 hover:bg-slate-700"
                                        >
                                            このセラーを展開 ↓
                                        </button>
                                    </li>
                                ))}
                            </ul>
                        </div>
                    ) : (
                        <p className="mt-4 text-sm text-slate-500">{discoverResult.note || discoverResult.error || '出品セラーが見つかりませんでした。'}</p>
                    )
                ) : null}
            </section>

            {/* 起点B: セラーIDを直接指定して展開 */}
            <section id="seller-mining-expand-form" className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <h2 className="mb-1 text-lg font-semibold text-white">② セラーIDを指定して出品を評価</h2>
                <p className="mb-4 text-sm text-slate-400">
                    Amazonのセラーページ(URLの<code className="rounded bg-slate-800 px-1">seller=</code>パラメータ)から手動で見つけたセラーIDも直接貼り付けられます。
                </p>
                <div className="flex flex-wrap items-end gap-3">
                    <label className="text-sm text-slate-400">
                        セラーID
                        <input
                            type="text"
                            value={expandSellerId}
                            onChange={(event) => setExpandSellerId(event.target.value)}
                            placeholder="A1234567890ABC"
                            className="mt-1 block w-56 rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-white outline-none focus:border-cyan-400"
                        />
                    </label>
                    <label className="text-sm text-slate-400">
                        最大候補数
                        <input
                            type="number"
                            min={1}
                            max={50}
                            value={expandMaxCandidates}
                            onChange={(event) => setExpandMaxCandidates(Number(event.target.value) || DEFAULT_MAX_CANDIDATES)}
                            className="mt-1 block w-24 rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-white outline-none focus:border-cyan-400"
                        />
                    </label>
                    <label className="text-sm text-slate-400">
                        起点ASIN(任意)
                        <input
                            type="text"
                            value={expandSeedAsin}
                            onChange={(event) => setExpandSeedAsin(event.target.value)}
                            placeholder="B0XXXXXXXX"
                            className="mt-1 block w-40 rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-white outline-none focus:border-cyan-400"
                        />
                    </label>
                    <button
                        type="button"
                        onClick={() => setExpandConfirming(true)}
                        disabled={!expandSellerId.trim() || expandBusy}
                        className="rounded-xl bg-cyan-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                    >
                        出品を評価
                    </button>
                </div>

                {expandConfirming ? (
                    <div className="mt-4 rounded-2xl border border-cyan-800 bg-cyan-950/20 p-4 text-sm">
                        <p className="text-cyan-100">
                            セラー <span className="font-mono font-semibold">{expandSellerId.trim()}</span> の出品を最大{expandMaxCandidates}件評価します。
                            想定コスト: <span className="font-semibold">最大 約{1 + expandMaxCandidates * 2}トークン</span>
                            (キャッシュ済みならもっと少ない、現在残高: {tokenStatus?.tokensLeft ?? '-'})。
                            数十秒〜数分かかることがあります。
                        </p>
                        <div className="mt-3 flex gap-2">
                            <button
                                type="button"
                                onClick={handleExpand}
                                disabled={expandBusy}
                                className="rounded-lg bg-cyan-500 px-3 py-1.5 text-xs font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                            >
                                {expandBusy ? '評価中...' : 'この内容で送信'}
                            </button>
                            <button
                                type="button"
                                onClick={() => setExpandConfirming(false)}
                                disabled={expandBusy}
                                className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-700"
                            >
                                キャンセル
                            </button>
                        </div>
                    </div>
                ) : null}

                {expandResult ? (
                    expandResult.ok ? (
                        <p className="mt-4 text-sm text-emerald-300">
                            セラー「{expandResult.sellerName}」: {expandResult.evaluated}件評価 / 合格{expandResult.qualifiedCount}件 / 不合格{expandResult.rejectedCount}件
                            {expandResult.stoppedEarlyForTokens ? '(トークン不足で途中打ち切り)' : ''}
                            — 下の一覧に反映しました。
                        </p>
                    ) : (
                        <p className="mt-4 text-sm text-rose-300">{expandResult.error}</p>
                    )
                ) : null}
            </section>

            {/* セラー別サマリー */}
            {sellerSummary.length > 0 ? (
                <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                    <h2 className="mb-4 text-lg font-semibold text-white">セラー別サマリー(合格率順)</h2>
                    <div className="flex flex-wrap gap-2">
                        {sellerFilter ? (
                            <button
                                type="button"
                                onClick={() => setSellerFilter('')}
                                className="rounded-xl bg-slate-800 px-3 py-2 text-xs font-semibold text-slate-300 hover:bg-slate-700"
                            >
                                絞り込み解除
                            </button>
                        ) : null}
                        {sellerSummary.map((s) => (
                            <button
                                key={s.key}
                                type="button"
                                onClick={() => setSellerFilter(s.key === sellerFilter ? '' : s.key)}
                                className={`rounded-xl px-3 py-2 text-xs font-semibold ${
                                    sellerFilter === s.key ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-200 hover:bg-slate-700'
                                }`}
                                title={s.seedAsins.length ? `発見元ASIN: ${s.seedAsins.join(', ')}` : ''}
                            >
                                {s.sellerName}: {s.qualified}/{s.total}合格 ({(s.rate * 100).toFixed(0)}%)
                            </button>
                        ))}
                    </div>
                </section>
            ) : null}

            {/* メイン一覧 */}
            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                    <div className="text-slate-300">
                        合格候補 <span className="font-semibold text-emerald-300">{qualifiedCount}</span>件 / 表示中 {visibleCandidates.length}件
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                        <label className="text-sm text-slate-400">
                            期間
                            <select
                                value={days}
                                onChange={(event) => setDays(Number(event.target.value))}
                                className="ml-2 rounded-xl border border-slate-700 bg-slate-900 px-2 py-1 text-white outline-none focus:border-cyan-400"
                            >
                                <option value={1}>直近1日</option>
                                <option value={7}>直近7日</option>
                                <option value={30}>直近30日</option>
                            </select>
                        </label>
                        <button
                            type="button"
                            onClick={() => setShowRejected((current) => !current)}
                            className={`rounded-2xl px-4 py-2 text-sm font-semibold ${showRejected ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-300'}`}
                        >
                            不合格候補も表示
                        </button>
                        <button
                            type="button"
                            onClick={() => refresh(days)}
                            className="rounded-2xl bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-200"
                        >
                            再読み込み
                        </button>
                    </div>
                </div>

                {loading ? (
                    <p className="py-12 text-center text-slate-500">読み込み中...</p>
                ) : visibleCandidates.length === 0 ? (
                    <p className="py-12 text-center text-slate-500">
                        セラーマイニングの結果はまだありません。上のフォームから開始してください。
                    </p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full border-collapse text-left text-sm">
                            <thead className="bg-slate-950/90">
                                <tr>
                                    <th className="px-4 py-3 font-medium text-slate-400">画像</th>
                                    {renderSortHeader('判定', 'qualified')}
                                    {renderSortHeader('ASIN', 'asin')}
                                    {renderSortHeader('商品名', 'title')}
                                    {renderSortHeader('US価格($)', 'usPriceUsd')}
                                    {renderSortHeader('JP価格(円)', 'jpCostJpy')}
                                    {renderSortHeader('実質利益率', 'marginPct')}
                                    {renderSortHeader('月間販売個数', 'monthlySold')}
                                    {renderSortHeader('1個あたり利益(円)', 'unitProfitUsd')}
                                    {renderSortHeader('ランキング', 'salesRank')}
                                    {renderSortHeader('レビュー数', 'reviewCount')}
                                    {renderSortHeader('セラー', 'sellerName')}
                                    {renderSortHeader('発見元ASIN', 'seedAsin')}
                                    {renderSortHeader('調査日時', 'createdAt')}
                                    <th className="px-4 py-3 font-medium text-slate-400">リンク</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {visibleCandidates.map((candidate) => (
                                    <tr
                                        key={`${candidate.runId}-${candidate.asin}`}
                                        className="border-t border-slate-800 bg-slate-950/80"
                                    >
                                        <td className="px-4 py-3">
                                            {candidate.imageUrl ? (
                                                <img
                                                    src={candidate.imageUrl}
                                                    alt={candidate.title || 'thumbnail'}
                                                    className="h-12 w-12 rounded-md border border-slate-700 object-cover"
                                                    loading="lazy"
                                                />
                                            ) : (
                                                <span className="text-slate-500">-</span>
                                            )}
                                        </td>
                                        <td className="px-4 py-3">
                                            {candidate.qualified ? (
                                                <span className="rounded-lg bg-emerald-900/60 px-2 py-1 text-xs font-semibold text-emerald-200">
                                                    合格
                                                </span>
                                            ) : (
                                                <span
                                                    className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-400"
                                                    title={candidate.reason || ''}
                                                >
                                                    不合格
                                                </span>
                                            )}
                                        </td>
                                        <td className="px-4 py-3 font-semibold text-white">{candidate.asin}</td>
                                        <td className="max-w-xl px-4 py-3 text-slate-200">{candidate.title || '-'}</td>
                                        <td className="px-4 py-3 text-slate-200">
                                            {candidate.usPriceUsd == null ? '-' : `$${Number(candidate.usPriceUsd).toFixed(2)}`}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">
                                            {candidate.jpCostJpy == null ? '-' : `¥${Number(candidate.jpCostJpy).toFixed(0)}`}
                                        </td>
                                        <td className={`px-4 py-3 font-semibold ${candidate.marginPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                            {candidate.marginPct == null ? '-' : `${(candidate.marginPct * 100).toFixed(1)}%`}
                                            {candidate.feeEstimated ? (
                                                <span className="ml-1 text-xs text-slate-500" title="手数料データなし、仮値で計算">*</span>
                                            ) : null}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">
                                            {candidate.monthlySold != null ? `${candidate.monthlySold.toLocaleString()}個` : '-'}
                                        </td>
                                        <td className={`px-4 py-3 font-semibold ${candidate.unitProfitUsd >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                            {candidate.unitProfitUsd == null ? '-' : `¥${Math.round(Number(candidate.unitProfitUsd) * EXCHANGE_RATE).toLocaleString()}`}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">{candidate.salesRank ?? '-'}</td>
                                        <td className="px-4 py-3 text-slate-200">{candidate.reviewCount ?? '-'}</td>
                                        <td className="px-4 py-3 text-slate-300">{candidate.sellerName || candidate.sellerId || '-'}</td>
                                        <td className="px-4 py-3 text-slate-400">
                                            {candidate.seedAsin ? (
                                                <button
                                                    type="button"
                                                    onClick={() => useAsinForDiscover(candidate.seedAsin)}
                                                    className="underline decoration-dotted hover:text-cyan-300"
                                                    title="このASINからさらにセラーを探す"
                                                >
                                                    {candidate.seedAsin}
                                                </button>
                                            ) : '-'}
                                        </td>
                                        <td className="px-4 py-3 text-slate-400">{formatDateTime(candidate.createdAt)}</td>
                                        <td className="px-4 py-3">
                                            <div className="flex flex-wrap gap-2">
                                                <a
                                                    href={candidate.usUrl}
                                                    target="_blank"
                                                    rel="noreferrer"
                                                    className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-700"
                                                >
                                                    US
                                                </a>
                                                {candidate.jpUrl ? (
                                                    <a
                                                        href={candidate.jpUrl}
                                                        target="_blank"
                                                        rel="noreferrer"
                                                        className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-700"
                                                    >
                                                        JP
                                                    </a>
                                                ) : null}
                                            </div>
                                        </td>
                                        <td className="px-4 py-3">
                                            {candidate.alreadyFavorited ? (
                                                <span className="inline-flex items-center gap-1 rounded-lg bg-amber-900/40 px-2 py-1 text-xs font-semibold text-amber-200">
                                                    <Star className="h-3.5 w-3.5" fill="currentColor" />
                                                    追加済み
                                                </span>
                                            ) : (
                                                <button
                                                    type="button"
                                                    onClick={() => addToFavorites(candidate)}
                                                    disabled={savingAsin === candidate.asin}
                                                    className="inline-flex items-center gap-1 rounded-lg bg-amber-400 px-2 py-1 text-xs font-semibold text-slate-950 hover:bg-amber-300 disabled:opacity-50"
                                                >
                                                    <Star className="h-3.5 w-3.5" />
                                                    {savingAsin === candidate.asin ? '追加中...' : 'お気に入りに追加'}
                                                </button>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </section>

            {/* セラープール(定期セラーマイニングの巡回対象) */}
            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <h2 className="mb-1 text-lg font-semibold text-white">セラープール(定期マイニング対象)</h2>
                <p className="mb-4 text-sm text-slate-400">
                    合格候補から自動発見・ダッシュボードから手動発見したセラーが貯まるプールです。
                    深夜1:00〜6:00(JST、既定)の間、run_all_day.shがここから調査回数の少ないセラーを
                    1件ずつ選んでマイニングします(<span className="text-cyan-300">次回選択</span>マークが目印)。
                </p>

                <form onSubmit={handleAddToPool} className="mb-4 flex flex-wrap gap-2">
                    <input
                        type="text"
                        value={newPoolSellerId}
                        onChange={(event) => setNewPoolSellerId(event.target.value)}
                        placeholder="セラーIDを手動追加: A1234567890ABC"
                        className="min-w-0 flex-1 rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white outline-none focus:border-cyan-400"
                    />
                    <button
                        type="submit"
                        disabled={addingToPool || !newPoolSellerId.trim()}
                        className="rounded-xl bg-cyan-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                    >
                        {addingToPool ? '追加中...' : '追加'}
                    </button>
                </form>

                {sellerPoolError ? (
                    <p className="mb-4 rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{sellerPoolError}</p>
                ) : null}

                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                    <div className="text-slate-300">
                        プール <span className="font-semibold text-cyan-300">{sellerPool.length}</span>件
                    </div>
                    <button
                        type="button"
                        onClick={refreshSellerPool}
                        className="rounded-2xl bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-200"
                    >
                        再読み込み
                    </button>
                </div>

                {sellerPoolLoading ? (
                    <p className="py-12 text-center text-slate-500">読み込み中...</p>
                ) : sellerPool.length === 0 ? (
                    <p className="py-12 text-center text-slate-500">
                        セラープールは空です。上のフォームで手動追加するか、合格候補が出ると自動的に追加されます。
                    </p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full border-collapse text-left text-sm">
                            <thead className="bg-slate-950/90">
                                <tr>
                                    <th className="px-4 py-3 font-medium text-slate-400">セラーID</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">名前</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">由来</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">調査回数</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">合格件数</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">最終調査日時</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">状態</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {sellerPool.map((item) => (
                                    <tr
                                        key={item.sellerId}
                                        className={`border-t border-slate-800 ${
                                            item.sellerId === nextPickSellerId ? 'bg-cyan-950/30' : 'bg-slate-950/80'
                                        }`}
                                    >
                                        <td className="px-4 py-3 font-mono text-xs text-white">
                                            {item.sellerId}
                                            {item.sellerId === nextPickSellerId ? (
                                                <span className="ml-2 rounded-lg bg-cyan-500 px-2 py-0.5 text-[10px] font-semibold text-slate-950">
                                                    次回選択
                                                </span>
                                            ) : null}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">{item.sellerName || '-'}</td>
                                        <td className="px-4 py-3 text-slate-400">{SELLER_SOURCE_LABEL[item.source] || item.source}</td>
                                        <td className="px-4 py-3 text-slate-200">{item.timesMined ?? 0}</td>
                                        <td className="px-4 py-3 text-slate-200">{item.totalQualified ?? 0}</td>
                                        <td className="px-4 py-3 text-slate-400">
                                            {item.lastMinedAt ? formatDateTime(item.lastMinedAt) : '未調査'}
                                        </td>
                                        <td className="px-4 py-3">
                                            <span
                                                className={`rounded-lg px-2 py-1 text-xs font-semibold ${
                                                    item.status === 'active' ? 'bg-emerald-900/60 text-emerald-200' : 'bg-slate-800 text-slate-400'
                                                }`}
                                            >
                                                {item.status === 'active' ? '有効' : '一時停止'}
                                            </span>
                                        </td>
                                        <td className="px-4 py-3">
                                            <button
                                                type="button"
                                                onClick={() => handleDeleteFromPool(item.sellerId)}
                                                disabled={deletingSellerId === item.sellerId}
                                                className="inline-flex items-center gap-1 rounded-lg bg-rose-950/60 px-2 py-1 text-xs font-semibold text-rose-200 hover:bg-rose-900 disabled:opacity-50"
                                            >
                                                <Trash2 className="h-3.5 w-3.5" />
                                                {deletingSellerId === item.sellerId ? '削除中...' : '削除'}
                                            </button>
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
