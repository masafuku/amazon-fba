import { useEffect, useMemo, useState } from 'react';
import { Star } from 'lucide-react';
import { loadAgentCandidates, loadAgentRuns, loadKeepaTokenStatus, saveFavorite } from './db';
import { formatDateTime } from './formatters';

const EXCHANGE_RATE = 150;

const formatDuration = (seconds) => {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '-';
    if (seconds < 60) return `${seconds.toFixed(0)}秒`;
    return `${Math.floor(seconds / 60)}分${Math.round(seconds % 60)}秒`;
};

// 実行中の検索は経過時間を秒精度で表示したいので、durationSecondsではなく
// startedAtから現在時刻までを都度計算する。
const formatElapsedSince = (startedAt) => {
    const startedMs = Date.parse(startedAt);
    if (Number.isNaN(startedMs)) return '-';
    return formatDuration((Date.now() - startedMs) / 1000);
};

// 実行中(status='running')の間だけ有効なポーリング間隔。
const RUNNING_POLL_INTERVAL_MS = 10000;

export default function AgentPage() {
    const [candidates, setCandidates] = useState([]);
    const [runs, setRuns] = useState([]);
    const [tokenStatus, setTokenStatus] = useState(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);
    const [savingAsin, setSavingAsin] = useState('');
    const [days, setDays] = useState(7);
    const [showRejected, setShowRejected] = useState(false);
    const [showRunHistory, setShowRunHistory] = useState(true);
    const [sortKey, setSortKey] = useState('marginPct');
    const [sortOrder, setSortOrder] = useState('desc');

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
            setError(loadError?.message || 'エージェント候補の読み込みに失敗しました。');
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
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [days]);

    const runningRun = useMemo(() => runs.find((run) => run.status === 'running'), [runs]);

    // 実行中の検索がある間は、状況が分かるように自動でポーリングする
    // (完了したら自然にポーリングが止まる)。
    useEffect(() => {
        if (!runningRun) return undefined;
        const timer = setInterval(() => {
            refresh(days);
            refreshTokenStatus();
        }, RUNNING_POLL_INTERVAL_MS);
        return () => clearInterval(timer);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [runningRun?.runId, days]);

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
                source: 'agent',
                data: { ...candidate.data, category: candidate.category },
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

    const visibleCandidates = useMemo(() => {
        const base = showRejected ? candidates : candidates.filter((item) => item.qualified);
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
    }, [candidates, showRejected, sortKey, sortOrder]);

    const qualifiedCount = useMemo(() => candidates.filter((item) => item.qualified).length, [candidates]);

    return (
        <main className="space-y-6">
            <header className="flex flex-wrap items-start justify-between gap-4">
                <div>
                    <p className="text-sm uppercase tracking-[0.2em] text-cyan-300">Research Agent</p>
                    <h1 className="mt-1 text-3xl font-semibold text-white">🤖 エージェント</h1>
                    <p className="mt-2 text-sm text-slate-400">
                        daily_scan.py が自動で調査した候補です。実質利益率(FBA手数料・国際送料込み)で
                        フィルタ済みですが、お気に入りへの追加は判断してから行ってください。
                    </p>
                </div>
                <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm">
                    <p className="text-slate-400">Keepaトークン残高</p>
                    {tokenStatus?.error ? (
                        <p className="text-rose-300">{tokenStatus.error}</p>
                    ) : (
                        <>
                            <p className="text-xl font-semibold text-white">
                                {tokenStatus?.tokensLeft ?? '-'}
                            </p>
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
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-cyan-800 bg-cyan-950/30 px-4 py-3">
                    <div className="flex items-center gap-3">
                        <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-cyan-400" aria-hidden="true" />
                        <p className="text-sm text-cyan-100">
                            <span className="font-semibold">🔄 検索実行中:</span> {runningRun.keyword}
                            {runningRun.category ? ` / ${runningRun.category}` : ''}
                            <span className="ml-2 text-cyan-300">経過 {formatElapsedSince(runningRun.startedAt)}</span>
                        </p>
                    </div>
                    <p className="text-xs text-cyan-400">{RUNNING_POLL_INTERVAL_MS / 1000}秒ごとに自動更新中</p>
                </div>
            ) : null}

            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="mb-4 flex items-center justify-between gap-3">
                    <h2 className="text-lg font-semibold text-white">実行履歴</h2>
                    <button
                        type="button"
                        onClick={() => setShowRunHistory((current) => !current)}
                        className="rounded-2xl bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-200"
                    >
                        {showRunHistory ? '隠す' : '表示'}
                    </button>
                </div>
                {showRunHistory ? (
                    loading ? (
                        <p className="py-6 text-center text-slate-500">読み込み中...</p>
                    ) : runs.length === 0 ? (
                        <p className="py-6 text-center text-slate-500">daily_scan.py はまだ実行されていません。</p>
                    ) : (
                        <div className="overflow-x-auto">
                            <table className="min-w-full border-collapse text-left text-sm">
                                <thead className="bg-slate-950/90">
                                    <tr>
                                        <th className="px-4 py-3 font-medium text-slate-400">実行日時</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">キーワード / カテゴリ</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">所要時間</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">評価件数</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">合格/却下</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">状態</th>
                                        <th className="px-4 py-3 font-medium text-slate-400">通知</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {runs.map((run) => (
                                        <tr key={run.runId} className="border-t border-slate-800 bg-slate-950/80">
                                            <td className="px-4 py-3 text-slate-200">{formatDateTime(run.startedAt)}</td>
                                            <td className="px-4 py-3 text-slate-300">
                                                {run.keyword || '-'}
                                                {run.category ? ` / ${run.category}` : ''}
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
                        この期間の候補はまだありません。daily_scan.py を実行してください。
                    </p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full border-collapse text-left text-sm">
                            <thead className="bg-slate-950/90">
                                <tr>
                                    <th className="px-4 py-3 font-medium text-slate-400">画像</th>
                                    {renderSortHeader('判定', 'qualified')}
                                    {renderSortHeader('カテゴリ', 'category')}
                                    {renderSortHeader('ASIN', 'asin')}
                                    {renderSortHeader('商品名', 'title')}
                                    {renderSortHeader('US価格($)', 'usPriceUsd')}
                                    {renderSortHeader('JP価格(円)', 'jpCostJpy')}
                                    {renderSortHeader('実質利益率', 'marginPct')}
                                    {renderSortHeader('1個あたり利益(円)', 'unitProfitUsd')}
                                    {renderSortHeader('価格変動(90日)', 'priceVolatility90d')}
                                    {renderSortHeader('ランキング', 'salesRank')}
                                    {renderSortHeader('レビュー数', 'reviewCount')}
                                    {renderSortHeader('調査日時', 'createdAt')}
                                    {renderSortHeader('発見回数', 'timesSeen')}
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
                                        <td className="px-4 py-3 text-slate-300">{candidate.category || '-'}</td>
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
                                                <span className="ml-1 text-xs text-slate-500" title="手数料データなし、仮値で計算">
                                                    *
                                                </span>
                                            ) : null}
                                        </td>
                                        <td className={`px-4 py-3 font-semibold ${candidate.unitProfitUsd >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                            {candidate.unitProfitUsd == null ? '-' : `¥${Math.round(Number(candidate.unitProfitUsd) * EXCHANGE_RATE).toLocaleString()}`}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">
                                            {candidate.priceVolatility90d == null ? '-' : `±${(candidate.priceVolatility90d * 100).toFixed(0)}%`}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">{candidate.salesRank ?? '-'}</td>
                                        <td className="px-4 py-3 text-slate-200">{candidate.reviewCount ?? '-'}</td>
                                        <td className="px-4 py-3 text-slate-400">{formatDateTime(candidate.createdAt)}</td>
                                        <td className="px-4 py-3 text-slate-200">
                                            {candidate.timesSeen > 1 ? (
                                                <span className="rounded-lg bg-cyan-900/40 px-2 py-1 text-xs font-semibold text-cyan-200" title="複数回のスキャンで繰り返し見つかっている候補">
                                                    {candidate.timesSeen}回
                                                </span>
                                            ) : (
                                                candidate.timesSeen ?? '-'
                                            )}
                                        </td>
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
        </main>
    );
}
