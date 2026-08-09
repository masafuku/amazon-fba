import { useEffect, useMemo, useState } from 'react';
import { Trash2 } from 'lucide-react';
import {
    addSellerToPool,
    deleteSellerFromPool,
    discoverSellersForAsin,
    expandFromSeller,
    loadAgentRuns,
    loadKeepaTokenStatus,
    loadSellerPool,
} from './db';
import { formatDateTime, formatDuration, formatElapsedSince } from './formatters';

const SELLER_SOURCE_LABEL = {
    manual: '手動追加',
    manual_expand: 'ダッシュボード発見',
    keyword_expansion: 'キーワード検索経由',
};

const DEFAULT_MAX_SELLERS = 5;
const DEFAULT_MAX_CANDIDATES = 15;

// 合格ラインの多段階化(CEO: 「合格ラインは何段階かに分けてください」)。
// このページでは個別商品バッジではなく、セラー別統計の「優秀さ」列の
// 内訳ツールチップのラベルとして使う。
const TIER_STYLES = {
    pass: { label: '合格', className: 'bg-emerald-900/60 text-emerald-200' },
    consider: { label: '要検討', className: 'bg-amber-900/60 text-amber-200' },
    reference: { label: '参考', className: 'bg-slate-700/60 text-slate-300' },
    reject: { label: '不合格', className: 'bg-slate-800 text-slate-400' },
};

export default function SellerMiningPage() {
    const [runs, setRuns] = useState([]);
    const [tokenStatus, setTokenStatus] = useState(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);
    // 実行履歴セクションの絞り込み期間(セラー別統計は常に全期間集計、
    // こちらは直近の動きを見るためのウィンドウ)。
    const [days, setDays] = useState(7);
    const [showRunHistory, setShowRunHistory] = useState(true);

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

    // セラー別統計(旧セラープールを拡張したもの。定期セラーマイニングが
    // 巡回する対象でもある - run_all_day.shの深夜時間帯にキーワード検索の
    // 代わりに1件ずつマイニングされる)。
    const [sellerPool, setSellerPool] = useState([]);
    const [sellerPoolLoading, setSellerPoolLoading] = useState(true);
    const [sellerPoolError, setSellerPoolError] = useState('');
    const [newPoolSellerId, setNewPoolSellerId] = useState('');
    const [addingToPool, setAddingToPool] = useState(false);
    const [deletingSellerId, setDeletingSellerId] = useState('');
    const [poolSortKey, setPoolSortKey] = useState('productCount');
    const [poolSortOrder, setPoolSortOrder] = useState('desc');

    const refreshSellerPool = async () => {
        setSellerPoolLoading(true);
        setSellerPoolError('');
        try {
            setSellerPool(await loadSellerPool());
        } catch (poolError) {
            setSellerPoolError(poolError?.message || 'セラー別統計の読み込みに失敗しました。');
        } finally {
            setSellerPoolLoading(false);
        }
    };

    const refresh = async (period) => {
        setLoading(true);
        setError('');
        try {
            setRuns(await loadAgentRuns(Math.max(period, 30)));
        } catch (loadError) {
            setError(loadError?.message || 'セラーマイニング実行履歴の読み込みに失敗しました。');
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

    // 優秀さ(合格率)を事前計算して並び替え可能にする。商品数0件のセラーは
    // 「優秀さ不明」として並び替え時は常に末尾に来るようnullにする(0%と
    // 区別する)。
    const sellerPoolWithRate = useMemo(
        () =>
            sellerPool.map((item) => ({
                ...item,
                passRate: item.productCount ? item.passCount / item.productCount : null,
            })),
        [sellerPool]
    );

    const selectPoolSortKey = (nextSortKey) => {
        if (poolSortKey === nextSortKey) {
            setPoolSortOrder((current) => (current === 'desc' ? 'asc' : 'desc'));
            return;
        }
        setPoolSortKey(nextSortKey);
        setPoolSortOrder('desc');
    };

    const renderPoolSortHeader = (label, key) => (
        <th key={key} className="px-4 py-3 font-medium text-slate-400">
            <button
                type="button"
                onClick={() => selectPoolSortKey(key)}
                className="inline-flex items-center gap-1 whitespace-nowrap text-left hover:text-cyan-300"
            >
                {label}
                <span className="text-xs text-cyan-300" aria-hidden="true">
                    {poolSortKey === key ? (poolSortOrder === 'desc' ? '▼' : '▲') : '↕'}
                </span>
            </button>
        </th>
    );

    const sortedSellerPool = useMemo(() => {
        return [...sellerPoolWithRate].sort((left, right) => {
            const dateKeys = new Set(['addedAt', 'lastMinedAt']);
            const leftValue = dateKeys.has(poolSortKey) ? Date.parse(left[poolSortKey]) || 0 : left[poolSortKey];
            const rightValue = dateKeys.has(poolSortKey) ? Date.parse(right[poolSortKey]) || 0 : right[poolSortKey];
            const leftMissing = leftValue === null || leftValue === undefined || leftValue === '';
            const rightMissing = rightValue === null || rightValue === undefined || rightValue === '';
            if (leftMissing || rightMissing) {
                if (leftMissing && rightMissing) return 0;
                return leftMissing ? 1 : -1;
            }
            const comparison = typeof leftValue === 'string'
                ? leftValue.localeCompare(String(rightValue), 'ja')
                : Number(leftValue) - Number(rightValue);
            return poolSortOrder === 'desc' ? -comparison : comparison;
        });
    }, [sellerPoolWithRate, poolSortKey, poolSortOrder]);

    const runningRun = useMemo(
        () => runs.find((run) => run.status === 'running' && run.sourceType === 'seller'),
        [runs]
    );

    // 実行履歴セクション: セラーマイニング由来のrunだけに絞り込む。
    const sellerRuns = useMemo(() => runs.filter((run) => run.sourceType === 'seller'), [runs]);

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
                // 新しいrunが記録され、そのセラーの全期間統計も変わりうるので両方更新する。
                await Promise.all([refresh(days), refreshSellerPool()]);
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

    return (
        <main className="space-y-6">
            <header className="flex flex-wrap items-start justify-between gap-4">
                <div>
                    <p className="text-sm uppercase tracking-[0.2em] text-cyan-300">Seller Mining</p>
                    <h1 className="mt-1 text-3xl font-semibold text-white">🕵️ セラーマイニング</h1>
                    <p className="mt-2 text-sm text-slate-400">
                        合格候補のセラーが他に何を売っているかを調べて候補を広げます。
                        「よく売れている日本のものを売っているセラーは、他にも同じようなものを売っていることが多い」
                        という考え方に基づく手法です。個別の発見商品は🤖エージェントページで確認できます
                        — このページはセラーごとの参考特徴(優秀さ・商品数・販売数・想定利益率・キーワード)と
                        実行履歴を中心に掲載しています。
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
                            — 下のセラー一覧・実行履歴に反映しました。
                        </p>
                    ) : (
                        <p className="mt-4 text-sm text-rose-300">{expandResult.error}</p>
                    )
                ) : null}
            </section>

            {/* セラー別統計(定期マイニング対象、CEOの希望: 優秀さ・商品数・販売数・
                想定利益率・カテゴリ・キーワードを参考にすべき特徴として掲載) */}
            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <h2 className="mb-1 text-lg font-semibold text-white">セラー別統計</h2>
                <p className="mb-4 text-sm text-slate-400">
                    合格候補から自動発見・ダッシュボードから手動発見したセラーの一覧です。統計は常に全期間の
                    集計(このセラーを再訪すべきかの恒久的な参考情報のため)。深夜1:00〜6:00(JST、既定)の間、
                    run_all_day.shがここから調査回数の少ないセラーを1件ずつ選んでマイニングします
                    (<span className="text-cyan-300">次回選択</span>マークが目印)。カテゴリ列は現時点では未実装のプレースホルダーです。
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
                        セラー <span className="font-semibold text-cyan-300">{sellerPool.length}</span>件
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
                        セラーはまだ登録されていません。上のフォームで手動追加するか、合格候補が出ると自動的に追加されます。
                    </p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full border-collapse text-left text-sm">
                            <thead className="bg-slate-950/90">
                                <tr>
                                    {renderPoolSortHeader('セラーID', 'sellerId')}
                                    {renderPoolSortHeader('名前', 'sellerName')}
                                    {renderPoolSortHeader('由来', 'source')}
                                    {renderPoolSortHeader('キーワード', 'seedKeyword')}
                                    {renderPoolSortHeader('優秀さ', 'passRate')}
                                    {renderPoolSortHeader('商品数', 'productCount')}
                                    {renderPoolSortHeader('販売数', 'totalMonthlySold')}
                                    {renderPoolSortHeader('想定利益率', 'avgMarginPct')}
                                    {renderPoolSortHeader('調査回数', 'timesMined')}
                                    {renderPoolSortHeader('最終調査日時', 'lastMinedAt')}
                                    <th className="px-4 py-3 font-medium text-slate-400">カテゴリ</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">状態</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {sortedSellerPool.map((item) => {
                                    const tierBreakdown =
                                        `${TIER_STYLES.pass.label}${item.passCount ?? 0} / ` +
                                        `${TIER_STYLES.consider.label}${item.considerCount ?? 0} / ` +
                                        `${TIER_STYLES.reference.label}${item.referenceCount ?? 0} / ` +
                                        `${TIER_STYLES.reject.label}${item.rejectCount ?? 0}`;
                                    return (
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
                                            <td className="px-4 py-3 text-slate-400">{item.seedKeyword || '-'}</td>
                                            <td className="px-4 py-3" title={tierBreakdown}>
                                                {item.passRate == null ? (
                                                    <span className="text-slate-500">-</span>
                                                ) : (
                                                    <span className={item.passRate >= 0.2 ? 'font-semibold text-emerald-400' : 'text-slate-300'}>
                                                        {(item.passRate * 100).toFixed(0)}%
                                                    </span>
                                                )}
                                            </td>
                                            <td className="px-4 py-3 text-slate-200">{item.productCount ?? 0}</td>
                                            <td className="px-4 py-3 text-slate-200">
                                                {item.totalMonthlySold != null ? `${item.totalMonthlySold.toLocaleString()}個` : '-'}
                                            </td>
                                            <td
                                                className={`px-4 py-3 font-semibold ${
                                                    item.avgMarginPct == null
                                                        ? 'text-slate-500'
                                                        : item.avgMarginPct >= 0
                                                            ? 'text-emerald-400'
                                                            : 'text-rose-400'
                                                }`}
                                            >
                                                {item.avgMarginPct == null ? '-' : `${(item.avgMarginPct * 100).toFixed(1)}%`}
                                            </td>
                                            <td className="px-4 py-3 text-slate-200">{item.timesMined ?? 0}</td>
                                            <td className="px-4 py-3 text-slate-400">
                                                {item.lastMinedAt ? formatDateTime(item.lastMinedAt) : '未調査'}
                                            </td>
                                            <td className="px-4 py-3 text-slate-500">-</td>
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
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                )}
            </section>

            {/* セラーマイニング実行履歴 */}
            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                    <h2 className="text-lg font-semibold text-white">セラーマイニング実行履歴</h2>
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
                            onClick={() => refresh(days)}
                            className="rounded-2xl bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-200"
                        >
                            再読み込み
                        </button>
                        <button
                            type="button"
                            onClick={() => setShowRunHistory((current) => !current)}
                            className="rounded-2xl bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-200"
                        >
                            {showRunHistory ? '隠す' : '表示'}
                        </button>
                    </div>
                </div>
                {showRunHistory ? (
                    loading ? (
                        <p className="py-6 text-center text-slate-500">読み込み中...</p>
                    ) : sellerRuns.length === 0 ? (
                        <p className="py-6 text-center text-slate-500">
                            セラーマイニングの実行履歴はまだありません。上のフォームから開始してください。
                        </p>
                    ) : (
                        <div className="overflow-x-auto">
                            <table className="min-w-full border-collapse text-left text-sm">
                                <thead className="bg-slate-950/90">
                                    <tr>
                                        <th className="px-4 py-3 font-medium text-slate-400">実行日時</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">セラー</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">所要時間</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">評価件数</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">合格/却下</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">状態</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">通知</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {sellerRuns.map((run) => (
                                        <tr key={run.runId} className="border-t border-slate-800 bg-slate-950/80">
                                            <td className="px-4 py-3 text-slate-200">{formatDateTime(run.startedAt)}</td>
                                            <td className="px-4 py-3 text-slate-300">
                                                <p>{run.sellerName || run.sellerId || '-'}</p>
                                                {run.category ? <p className="text-xs text-slate-500">{run.category}</p> : null}
                                            </td>
                                            <td className="px-4 py-3 text-slate-400">
                                                {run.status === 'running' ? formatElapsedSince(run.startedAt) : formatDuration(run.durationSeconds)}
                                            </td>
                                            <td className="px-4 py-3 text-slate-200">
                                                {run.status === 'running' ? '-' : `${run.evaluated ?? '-'}件中 粗選別${run.mcpMatched ?? '-'}件`}
                                            </td>
                                            <td className="px-4 py-3 text-slate-200">
                                                <span className="text-emerald-300">{run.qualifiedCount ?? '-'}</span>
                                                {' / '}
                                                <span className="text-slate-400">{run.rejectedCount ?? '-'}</span>
                                            </td>
                                            <td className="px-4 py-3">
                                                {run.status === 'running' ? (
                                                    <span className="inline-flex items-center gap-1 rounded-lg bg-cyan-900/50 px-2 py-1 text-xs font-semibold text-cyan-200">
                                                        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-300" aria-hidden="true" />
                                                        実行中
                                                    </span>
                                                ) : run.error ? (
                                                    <span className="rounded-lg bg-rose-900/60 px-2 py-1 text-xs font-semibold text-rose-200" title={run.error}>
                                                        エラー
                                                    </span>
                                                ) : run.stoppedEarlyForTokens ? (
                                                    <span className="rounded-lg bg-amber-900/40 px-2 py-1 text-xs font-semibold text-amber-200">
                                                        途中で打ち切り
                                                    </span>
                                                ) : (
                                                    <span className="rounded-lg bg-emerald-900/60 px-2 py-1 text-xs font-semibold text-emerald-200">
                                                        完了
                                                    </span>
                                                )}
                                            </td>
                                            <td className="px-4 py-3">
                                                {run.notifyStatus === 'sent' ? (
                                                    <span className="text-emerald-300">送信済み</span>
                                                ) : run.notifyStatus === 'failed' ? (
                                                    <span className="text-rose-300" title={run.notifyError || ''}>失敗</span>
                                                ) : run.notifyStatus === 'skipped' ? (
                                                    <span className="text-slate-500">未設定でスキップ</span>
                                                ) : (
                                                    <span className="text-slate-600">-</span>
                                                )}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )
                ) : null}
            </section>
        </main>
    );
}
