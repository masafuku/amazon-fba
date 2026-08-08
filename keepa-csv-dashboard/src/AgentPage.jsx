import { useEffect, useMemo, useState } from 'react';
import { Star } from 'lucide-react';
import { loadAgentCandidates, saveFavorite } from './db';
import { formatDateTime } from './formatters';

export default function AgentPage() {
    const [candidates, setCandidates] = useState([]);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);
    const [savingAsin, setSavingAsin] = useState('');
    const [days, setDays] = useState(7);
    const [showRejected, setShowRejected] = useState(false);

    const refresh = async (period) => {
        setLoading(true);
        setError('');
        try {
            setCandidates(await loadAgentCandidates(period));
        } catch (loadError) {
            setError(loadError?.message || 'エージェント候補の読み込みに失敗しました。');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        refresh(days);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [days]);

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

    const visibleCandidates = useMemo(
        () => (showRejected ? candidates : candidates.filter((item) => item.qualified)),
        [candidates, showRejected]
    );

    const qualifiedCount = useMemo(() => candidates.filter((item) => item.qualified).length, [candidates]);

    return (
        <main className="space-y-6">
            <header>
                <p className="text-sm uppercase tracking-[0.2em] text-cyan-300">Research Agent</p>
                <h1 className="mt-1 text-3xl font-semibold text-white">🤖 エージェント</h1>
                <p className="mt-2 text-sm text-slate-400">
                    daily_scan.py が自動で調査した候補です。実質利益率(FBA手数料・国際送料込み)で
                    フィルタ済みですが、お気に入りへの追加は判断してから行ってください。
                </p>
            </header>

            {error ? (
                <p className="rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{error}</p>
            ) : null}

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
                                    <th className="px-4 py-3 font-medium text-slate-400">判定</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">カテゴリ</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">ASIN</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">商品名</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">US価格($)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">JP価格(円)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">実質利益率</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">1個あたり利益($)</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">ランキング</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">レビュー数</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">調査日時</th>
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
                                            {candidate.unitProfitUsd == null ? '-' : `$${Number(candidate.unitProfitUsd).toFixed(2)}`}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">{candidate.salesRank ?? '-'}</td>
                                        <td className="px-4 py-3 text-slate-200">{candidate.reviewCount ?? '-'}</td>
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
        </main>
    );
}
