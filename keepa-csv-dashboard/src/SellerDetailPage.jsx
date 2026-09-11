import { useEffect, useMemo, useState } from 'react';
import { Star } from 'lucide-react';
import { loadSellerCandidates, loadSellerPool, saveFavorite } from './db';
import { formatDateTime } from './formatters';

// AgentPage.jsx / SellerMiningPage.jsx / CandidateDetailPage.jsxと同じ定義
// (合格ラインの多段階化)。このコードベースの既存パターンに合わせ、各ページ
// ローカルにコピーを持つ(共通モジュール化は今回のスコープ外)。
const TIER_STYLES = {
    pass: { label: '合格', className: 'bg-emerald-900/60 text-emerald-200' },
    consider: { label: '要検討', className: 'bg-amber-900/60 text-amber-200' },
    reference: { label: '参考', className: 'bg-slate-700/60 text-slate-300' },
    reject: { label: '不合格', className: 'bg-slate-800 text-slate-400' },
};
const resolveTier = (candidate) => candidate.tier ?? (candidate.qualified ? 'pass' : 'reject');

function StatCard({ label, value, tone, title }) {
    const toneClass = tone === 'positive' ? 'text-emerald-400' : tone === 'negative' ? 'text-rose-400' : 'text-white';
    return (
        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3" title={title}>
            <p className="text-xs text-slate-400">{label}</p>
            <p className={`mt-1 text-lg font-semibold ${toneClass}`}>{value}</p>
        </div>
    );
}

// CEO: 「セラーサーチで見つけたセラーの結果をもう少しみやすくしたい。少なくとも、
// そのセラーのページを一枚作ること。さらには、セラーが売っている一覧とその
// パフォーマンスを表形式で確認して、何が良いのかを理解しやすくしてほしい。」
export default function SellerDetailPage({ sellerId, onBack }) {
    const [seller, setSeller] = useState(null);
    const [sellerLoading, setSellerLoading] = useState(true);
    const [sellerError, setSellerError] = useState('');

    const [candidates, setCandidates] = useState([]);
    const [candidatesLoading, setCandidatesLoading] = useState(true);
    const [candidatesError, setCandidatesError] = useState('');

    const [sortKey, setSortKey] = useState('marginPct');
    const [sortOrder, setSortOrder] = useState('desc');
    const [savingAsin, setSavingAsin] = useState('');
    const [favoriteError, setFavoriteError] = useState('');

    useEffect(() => {
        let cancelled = false;
        setSellerLoading(true);
        setSellerError('');
        loadSellerPool()
            .then((sellers) => {
                if (cancelled) return;
                setSeller(sellers.find((item) => item.sellerId === sellerId) || null);
            })
            .catch((loadError) => {
                if (!cancelled) setSellerError(loadError?.message || 'セラー情報の読み込みに失敗しました。');
            })
            .finally(() => {
                if (!cancelled) setSellerLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [sellerId]);

    useEffect(() => {
        let cancelled = false;
        setCandidatesLoading(true);
        setCandidatesError('');
        loadSellerCandidates(sellerId)
            .then((result) => {
                if (!cancelled) setCandidates(result);
            })
            .catch((loadError) => {
                if (!cancelled) setCandidatesError(loadError?.message || '出品商品の読み込みに失敗しました。');
            })
            .finally(() => {
                if (!cancelled) setCandidatesLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [sellerId]);

    const tierBreakdown = useMemo(() => {
        const counts = { pass: 0, consider: 0, reference: 0, reject: 0 };
        candidates.forEach((item) => {
            const tier = resolveTier(item);
            counts[tier] = (counts[tier] || 0) + 1;
        });
        return counts;
    }, [candidates]);

    const selectSortKey = (nextKey) => {
        if (sortKey === nextKey) {
            setSortOrder((current) => (current === 'desc' ? 'asc' : 'desc'));
            return;
        }
        setSortKey(nextKey);
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

    const sortedCandidates = useMemo(() => {
        const tierOrder = { pass: 0, consider: 1, reference: 2, reject: 3 };
        return [...candidates].sort((left, right) => {
            let leftValue;
            let rightValue;
            if (sortKey === 'tier') {
                leftValue = tierOrder[resolveTier(left)] ?? 9;
                rightValue = tierOrder[resolveTier(right)] ?? 9;
            } else if (sortKey === 'createdAt') {
                leftValue = Date.parse(left.createdAt) || 0;
                rightValue = Date.parse(right.createdAt) || 0;
            } else {
                leftValue = left[sortKey];
                rightValue = right[sortKey];
            }
            const leftMissing = leftValue === null || leftValue === undefined;
            const rightMissing = rightValue === null || rightValue === undefined;
            if (leftMissing || rightMissing) {
                if (leftMissing && rightMissing) return 0;
                return leftMissing ? 1 : -1;
            }
            const comparison = typeof leftValue === 'string'
                ? leftValue.localeCompare(String(rightValue), 'ja')
                : Number(leftValue) - Number(rightValue);
            return sortOrder === 'desc' ? -comparison : comparison;
        });
    }, [candidates, sortKey, sortOrder]);

    const addToFavorites = async (candidate) => {
        setSavingAsin(candidate.asin);
        setFavoriteError('');
        try {
            await saveFavorite({
                asin: candidate.asin,
                title: candidate.title,
                source: 'seller',
                data: { ...candidate.data, category: candidate.category },
            });
            setCandidates((current) =>
                current.map((item) => (item.asin === candidate.asin ? { ...item, alreadyFavorited: true } : item))
            );
        } catch (saveError) {
            setFavoriteError(`${candidate.asin} のお気に入り登録に失敗しました: ${saveError?.message || '不明なエラー'}`);
        } finally {
            setSavingAsin('');
        }
    };

    const passRate = seller?.productCount ? seller.passCount / seller.productCount : null;

    return (
        <main className="space-y-6">
            <button type="button" onClick={onBack} className="text-sm text-cyan-300 hover:underline">
                ← セラーマイニングに戻る
            </button>

            <header>
                <p className="text-sm uppercase tracking-[0.2em] text-cyan-300">Seller</p>
                {sellerLoading ? (
                    <p className="mt-1 text-slate-500">読み込み中...</p>
                ) : (
                    <>
                        <h1 className="mt-1 text-3xl font-semibold text-white">
                            {seller?.sellerName || 'セラー名不明'}
                        </h1>
                        <p className="mt-1 font-mono text-sm text-slate-400">{sellerId}</p>
                    </>
                )}
            </header>

            {sellerError ? (
                <p className="rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{sellerError}</p>
            ) : null}

            {/* セラー統計サマリー(セラーマイニングページの一覧行と同じ集計値、
                ここでは1セラー分をカードで大きく見せる) */}
            {seller ? (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                    <StatCard
                        label="優秀さ(合格率)"
                        value={passRate == null ? '-' : `${(passRate * 100).toFixed(0)}%`}
                        tone={passRate != null && passRate >= 0.2 ? 'positive' : undefined}
                        title={`合格${tierBreakdown.pass} / 要検討${tierBreakdown.consider} / 参考${tierBreakdown.reference} / 不合格${tierBreakdown.reject}`}
                    />
                    <StatCard label="商品数" value={seller.productCount ?? 0} />
                    <StatCard
                        label="販売数合計"
                        value={seller.totalMonthlySold != null ? `${seller.totalMonthlySold.toLocaleString()}個` : '-'}
                    />
                    <StatCard
                        label="想定利益率平均"
                        value={seller.avgMarginPct == null ? '-' : `${(seller.avgMarginPct * 100).toFixed(1)}%`}
                        tone={seller.avgMarginPct == null ? undefined : seller.avgMarginPct >= 0 ? 'positive' : 'negative'}
                    />
                    <StatCard label="調査回数" value={seller.timesMined ?? 0} />
                    <StatCard
                        label="最終調査日時"
                        value={seller.lastMinedAt ? formatDateTime(seller.lastMinedAt) : '未調査'}
                    />
                </div>
            ) : !sellerLoading ? (
                <p className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4 text-sm text-slate-400">
                    このセラーはセラー別統計にまだ登録されていません(商品一覧だけ下に表示しています)。
                </p>
            ) : null}

            {/* 出品商品一覧(パフォーマンス表) */}
            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                    <h2 className="text-lg font-semibold text-white">
                        出品商品 <span className="font-semibold text-cyan-300">{candidates.length}</span>件
                    </h2>
                    <p className="text-xs text-slate-500">
                        合格{tierBreakdown.pass} / 要検討{tierBreakdown.consider} / 参考{tierBreakdown.reference} / 不合格{tierBreakdown.reject}
                    </p>
                </div>

                {favoriteError ? (
                    <p className="mb-4 rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{favoriteError}</p>
                ) : null}
                {candidatesError ? (
                    <p className="mb-4 rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{candidatesError}</p>
                ) : null}

                {candidatesLoading ? (
                    <p className="py-12 text-center text-slate-500">読み込み中...</p>
                ) : candidates.length === 0 ? (
                    <p className="py-12 text-center text-slate-500">
                        このセラーの出品商品はまだ評価されていません。セラーマイニングページの
                        「②セラーIDを指定して出品を評価」から実行してください。
                    </p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full border-collapse text-left text-sm">
                            <thead className="bg-slate-950/90">
                                <tr>
                                    <th className="px-4 py-3 font-medium text-slate-400">画像</th>
                                    {renderSortHeader('判定', 'tier')}
                                    {renderSortHeader('ASIN', 'asin')}
                                    {renderSortHeader('商品名', 'title')}
                                    {renderSortHeader('ROI', 'roiPct')}
                                    {renderSortHeader('実質利益率', 'marginPct')}
                                    {renderSortHeader('先月の販売個数', 'monthlySold')}
                                    {renderSortHeader('US価格($)', 'usPriceUsd')}
                                    {renderSortHeader('JP原価(円)', 'jpCostJpy')}
                                    {renderSortHeader('売上ランク', 'salesRank')}
                                    {renderSortHeader('レビュー数', 'reviewCount')}
                                    {renderSortHeader('発見日時', 'createdAt')}
                                    <th className="px-4 py-3 font-medium text-slate-400">リンク</th>
                                    <th className="px-4 py-3 font-medium text-slate-400">操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {sortedCandidates.map((candidate) => {
                                    const tier = resolveTier(candidate);
                                    const style = TIER_STYLES[tier] || TIER_STYLES.reject;
                                    const roiPct = candidate.data?.roi_pct ?? null;
                                    return (
                                        <tr key={candidate.asin} className="border-t border-slate-800 bg-slate-950/80">
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
                                                <span
                                                    className={`rounded-lg px-2 py-1 text-xs font-semibold ${style.className}`}
                                                    title={tier === 'pass' ? '' : candidate.reason || ''}
                                                >
                                                    {style.label}
                                                </span>
                                            </td>
                                            <td className="px-4 py-3 font-semibold">
                                                <a
                                                    href={`#candidate/${encodeURIComponent(candidate.asin)}`}
                                                    className="text-cyan-300 underline decoration-cyan-700 underline-offset-2 hover:text-cyan-200"
                                                >
                                                    {candidate.asin}
                                                </a>
                                            </td>
                                            <td className="max-w-xl px-4 py-3 text-slate-200">{candidate.title || '-'}</td>
                                            <td className={`px-4 py-3 font-semibold ${roiPct == null ? '' : roiPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                                {roiPct == null ? '-' : `${(roiPct * 100).toFixed(1)}%`}
                                            </td>
                                            <td className={`px-4 py-3 font-semibold ${candidate.marginPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                                {candidate.marginPct == null ? '-' : `${(candidate.marginPct * 100).toFixed(1)}%`}
                                            </td>
                                            <td className="px-4 py-3 text-slate-200">
                                                {candidate.monthlySold != null
                                                    ? `${candidate.monthlySold.toLocaleString()}個`
                                                    : candidate.data?.sales_rank_drops_30 != null
                                                      ? `ランク変動30日 ${candidate.data.sales_rank_drops_30}回`
                                                      : '-'}
                                            </td>
                                            <td className="px-4 py-3 text-slate-200">
                                                {candidate.usPriceUsd == null ? '-' : `$${Number(candidate.usPriceUsd).toFixed(2)}`}
                                            </td>
                                            <td className="px-4 py-3 text-slate-200">
                                                {candidate.jpCostJpy == null ? '-' : `¥${Number(candidate.jpCostJpy).toFixed(0)}`}
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
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                )}
            </section>
        </main>
    );
}
