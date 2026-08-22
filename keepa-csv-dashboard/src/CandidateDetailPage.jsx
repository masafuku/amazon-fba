import { useEffect, useMemo, useState } from 'react';
import { Star } from 'lucide-react';
import {
    CartesianGrid,
    Line,
    LineChart,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { fetchCandidateHistory, fetchSellerCount, loadAgentCandidateDetail, loadCandidateHistory, lookupAsin, saveFavorite } from './db';
import { formatDateTime } from './formatters';

const EXCHANGE_RATE = 150;

// AgentPage.jsxと同じ定義(合格ラインの多段階化)。
const TIER_STYLES = {
    pass: { label: '合格', className: 'bg-emerald-900/60 text-emerald-200' },
    consider: { label: '要検討', className: 'bg-amber-900/60 text-amber-200' },
    reference: { label: '参考', className: 'bg-slate-700/60 text-slate-300' },
    reject: { label: '不合格', className: 'bg-slate-800 text-slate-400' },
};
const resolveTier = (candidate) => candidate.tier ?? (candidate.qualified ? 'pass' : 'reject');

// https://keepa.com/#!product/<domainId>-<asin> という既知のURL形式。
// keepa_mcp/keepa_client.py の DOMAIN_IDS と同じ値(バックエンド変更不要、
// フロントエンドの小さな定数として持つだけでよい)。
const KEEPA_DOMAIN_IDS = { US: 1, JP: 5 };
const keepaProductUrl = (asin, domain) => {
    const domainId = KEEPA_DOMAIN_IDS[domain] ?? KEEPA_DOMAIN_IDS.US;
    return `https://keepa.com/#!product/${domainId}-${asin}`;
};

const usd = (value) => (value == null ? '-' : `$${Number(value).toFixed(2)}`);
const jpy = (value) => (value == null ? '-' : `¥${Number(value).toLocaleString()}`);
const usdAsJpy = (value) => (value == null ? '-' : `¥${Math.round(Number(value) * EXCHANGE_RATE).toLocaleString()}`);
const pct = (value) => (value == null ? '-' : `${(Number(value) * 100).toFixed(1)}%`);

function StatCard({ label, value, tone }) {
    const toneClass = tone === 'positive' ? 'text-emerald-400' : tone === 'negative' ? 'text-rose-400' : 'text-white';
    return (
        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3">
            <p className="text-xs text-slate-400">{label}</p>
            <p className={`mt-1 text-lg font-semibold ${toneClass}`}>{value}</p>
        </div>
    );
}

// 価格履歴のうちnew/amazon/buy_box_shippingを1本のTimeシリーズにマージする
// (recharts用: 同じtimeキーの下に複数の値を持たせ、欠けている系列はnullのまま線を途切れさせる)。
function mergePriceSeries(priceHistory) {
    if (!priceHistory) return [];
    const points = new Map();
    const addSeries = (key, series) => {
        (series || []).forEach(({ time, value }) => {
            if (!points.has(time)) points.set(time, { time });
            points.get(time)[key] = value;
        });
    };
    addSeries('new', priceHistory.new);
    addSeries('amazon', priceHistory.amazon);
    addSeries('buyBox', priceHistory.buy_box_shipping);
    return Array.from(points.values()).sort((a, b) => a.time - b.time);
}

function formatChartDate(time) {
    const date = new Date(time);
    return date.toLocaleDateString('ja-JP', { year: '2-digit', month: '2-digit', day: '2-digit' });
}

export default function CandidateDetailPage({ asin, onBack }) {
    const [candidate, setCandidate] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [saving, setSaving] = useState(false);

    const [history, setHistory] = useState(null); // { domain, priceHistory, rankHistory, fetchedAt } | null
    const [historyLoading, setHistoryLoading] = useState(true);
    const [historyFetching, setHistoryFetching] = useState(false);
    const [historyError, setHistoryError] = useState('');
    const [copyStatus, setCopyStatus] = useState('');
    const [investigating, setInvestigating] = useState(false);
    const [investigateError, setInvestigateError] = useState('');
    const [sellerCountFetching, setSellerCountFetching] = useState(false);
    const [sellerCountError, setSellerCountError] = useState('');

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError('');
        loadAgentCandidateDetail(asin)
            .then((result) => {
                if (!cancelled) setCandidate(result);
            })
            .catch((loadError) => {
                if (!cancelled) setError(loadError?.message || '候補データの読み込みに失敗しました。');
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [asin]);

    // 保存済みの履歴データをDBから読むだけ(Keepaへは問い合わせない・トークン消費なし)。
    // CEOの明示的な指示: 「一度データ取得したものは...明示的にボタンを押さない限り、
    // 再取得しないで良いようにしてください」。
    useEffect(() => {
        let cancelled = false;
        setHistoryLoading(true);
        setHistoryError('');
        loadCandidateHistory(asin)
            .then((result) => {
                if (!cancelled) setHistory(result);
            })
            .catch((loadError) => {
                if (!cancelled) setHistoryError(loadError?.message || '履歴データの読み込みに失敗しました。');
            })
            .finally(() => {
                if (!cancelled) setHistoryLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [asin]);

    const handleFetchHistory = async () => {
        setHistoryFetching(true);
        setHistoryError('');
        try {
            const result = await fetchCandidateHistory(asin);
            if (!result.ok) {
                setHistoryError(result.error || '履歴データの取得に失敗しました。');
                return;
            }
            if (!result.found) {
                setHistoryError(result.error || 'この商品の履歴データはKeepaに見つかりませんでした。');
                return;
            }
            setHistory(result.history);
        } catch (fetchError) {
            setHistoryError(fetchError?.message || '履歴データの取得に失敗しました(トークン不足の可能性があります)。');
        } finally {
            setHistoryFetching(false);
        }
    };

    // CEO: 「候補商品に対して、セラーの数...を取得できますか？」「すべての商品
    // ではなく、有力候補のみ。」「選択的にバックフィルをしたい。」— 新規の合格
    // 候補は最初から取得済みだが、過去の合格候補はこのボタンでCEOが気になった
    // ものだけ個別に取得する(一括再実行はしない)。
    const handleFetchSellerCount = async () => {
        if (!candidate) return;
        setSellerCountFetching(true);
        setSellerCountError('');
        try {
            const result = await fetchSellerCount(candidate.runId, asin);
            if (!result.ok) {
                setSellerCountError(result.error || 'セラー数の取得に失敗しました。');
                return;
            }
            setCandidate((current) => (current
                ? { ...current, data: { ...current.data, competitor_seller_count: result.competitorSellerCount } }
                : current));
        } catch (fetchError) {
            setSellerCountError(fetchError?.message || 'セラー数の取得に失敗しました(トークン不足の可能性があります)。');
        } finally {
            setSellerCountFetching(false);
        }
    };

    const handleAddFavorite = async () => {
        if (!candidate) return;
        setSaving(true);
        setError('');
        try {
            await saveFavorite({
                asin: candidate.asin,
                title: candidate.title,
                source: 'agent',
                data: { ...candidate.data, category: candidate.category },
            });
            setCandidate((current) => (current ? { ...current, alreadyFavorited: true } : current));
        } catch (saveError) {
            setError(`お気に入り登録に失敗しました: ${saveError?.message || '不明なエラー'}`);
        } finally {
            setSaving(false);
        }
    };

    // CEO: 「お気に入りページからもASINによる詳細ページに飛びたいです」— お気に入りは
    // agent_candidates に無いASIN(Finder/CSV分析経由)も多いため、未調査なら
    // その場で調査できるようにする(AgentPage.jsxの「ASIN指定調査」入力と同じアクション)。
    const handleInvestigate = async () => {
        setInvestigating(true);
        setInvestigateError('');
        try {
            const result = await lookupAsin(asin);
            if (!result.ok) {
                setInvestigateError(result.error || 'ASINの調査に失敗しました。');
                return;
            }
            const detail = await loadAgentCandidateDetail(asin);
            setCandidate(detail);
        } catch (investigateErr) {
            setInvestigateError(investigateErr?.message || 'ASINの調査に失敗しました。');
        } finally {
            setInvestigating(false);
        }
    };

    const handleCopyLink = async () => {
        const url = window.location.href;
        try {
            if (!navigator.clipboard?.writeText) {
                // navigator.clipboard は https/localhost 限定(セキュアコンテキスト)。
                // このダッシュボードはAWS上でプレーンHTTP配信のため本番では使えず、
                // ここに来ると必ず失敗していた(CEO報告: 「コピーに失敗しました」)。
                throw new Error('clipboard API unavailable');
            }
            await navigator.clipboard.writeText(url);
            setCopyStatus('コピーしました');
        } catch {
            // document.execCommand('copy') は非推奨だが非セキュアコンテキストでも
            // 動作する唯一の実用的なフォールバック。
            const textarea = document.createElement('textarea');
            textarea.value = url;
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.focus();
            textarea.select();
            try {
                // execCommand は例外を投げずにfalseを返すだけで失敗することがある
                // (実際に確認済み)ため、戻り値も見て正確に成否を判定する。
                const succeeded = document.execCommand('copy');
                setCopyStatus(succeeded ? 'コピーしました' : 'コピーに失敗しました');
            } catch {
                setCopyStatus('コピーに失敗しました');
            } finally {
                document.body.removeChild(textarea);
            }
        } finally {
            setTimeout(() => setCopyStatus(''), 2000);
        }
    };

    const priceChartData = useMemo(() => mergePriceSeries(history?.priceHistory), [history]);
    const rankChartData = useMemo(
        () => (history?.rankHistory || []).map(({ time, value }) => ({ time, rank: value })),
        [history]
    );

    if (loading) {
        return <main className="py-16 text-center text-slate-500">読み込み中...</main>;
    }

    if (!candidate) {
        return (
            <main className="space-y-4">
                <button type="button" onClick={onBack} className="text-sm text-cyan-300 hover:underline">
                    ← エージェント一覧に戻る
                </button>
                {error ? (
                    <p className="rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{error}</p>
                ) : (
                    <div className="space-y-3 rounded-2xl border border-slate-800 bg-slate-900/80 p-6 text-center">
                        <p className="text-slate-300">
                            ASIN <span className="font-semibold text-white">{asin}</span> はまだ調査されていません。
                        </p>
                        {investigateError ? <p className="text-sm text-rose-300">{investigateError}</p> : null}
                        <button
                            type="button"
                            onClick={handleInvestigate}
                            disabled={investigating}
                            className="rounded-2xl bg-cyan-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                        >
                            {investigating ? '調査中...' : 'このASINを調査する'}
                        </button>
                    </div>
                )}
            </main>
        );
    }

    const data = candidate.data || {};
    const tier = resolveTier(candidate);
    const tierStyle = TIER_STYLES[tier] || TIER_STYLES.reject;
    const grossProfitUsd = candidate.usPriceUsd != null && data.jp_cost_usd != null ? candidate.usPriceUsd - data.jp_cost_usd : null;
    const grossMarginPct = grossProfitUsd != null && candidate.usPriceUsd ? grossProfitUsd / candidate.usPriceUsd : null;

    return (
        <main className="space-y-6">
            <button type="button" onClick={onBack} className="text-sm text-cyan-300 hover:underline">
                ← エージェント一覧に戻る
            </button>

            {error ? (
                <p className="rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{error}</p>
            ) : null}

            <section className="flex flex-wrap items-start gap-4 rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                {candidate.imageUrl ? (
                    <img
                        src={candidate.imageUrl}
                        alt={candidate.title || 'thumbnail'}
                        className="h-28 w-28 flex-shrink-0 rounded-xl border border-slate-700 object-cover"
                    />
                ) : (
                    <div className="flex h-28 w-28 flex-shrink-0 items-center justify-center rounded-xl border border-slate-700 bg-slate-950 text-xs text-slate-500">
                        画像なし
                    </div>
                )}
                <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                        <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${tierStyle.className}`}>{tierStyle.label}</span>
                        {data.brand ? <span className="text-xs text-slate-400">ブランド: {data.brand}</span> : null}
                    </div>
                    <h1 className="mt-1 text-xl font-semibold text-white">{candidate.title || '(商品名不明)'}</h1>
                    <p className="mt-1 text-sm text-slate-400">
                        ASIN: {candidate.asin}
                        {candidate.category ? <> &middot; カテゴリ: {candidate.category}</> : null}
                        {candidate.sellerName || candidate.sellerId ? (
                            <> &middot; セラー: {candidate.sellerName || candidate.sellerId}</>
                        ) : null}
                        {data.netsea_shop_name ? (
                            <>
                                {' '}
                                &middot; 仕入れ先:{' '}
                                {data.netsea_product_url ? (
                                    <a
                                        href={data.netsea_product_url}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="text-cyan-300 underline decoration-cyan-700 underline-offset-2 hover:text-cyan-200"
                                    >
                                        {data.netsea_shop_name}
                                    </a>
                                ) : (
                                    data.netsea_shop_name
                                )}
                            </>
                        ) : null}
                    </p>
                    {tier !== 'pass' && candidate.reason ? (
                        <p className="mt-1 text-xs text-amber-300">理由: {candidate.reason}</p>
                    ) : null}
                </div>
            </section>

            <section className="space-y-4 rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <h2 className="text-lg font-semibold text-white">価格・利益の内訳</h2>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <StatCard label="US価格" value={usd(candidate.usPriceUsd)} />
                    <StatCard label="Amazon JP価格" value={jpy(data.jp_amazon_cost_jpy)} />
                    <StatCard label="卸価格" value={jpy(data.wholesale_cost_jpy)} />
                    <StatCard label="採用原価" value={jpy(candidate.jpCostJpy)} />
                    <StatCard
                        label="表面利益(US-JP)"
                        value={usdAsJpy(grossProfitUsd)}
                        tone={grossProfitUsd == null ? undefined : grossProfitUsd >= 0 ? 'positive' : 'negative'}
                    />
                    <StatCard
                        label="表面利益率"
                        value={pct(grossMarginPct)}
                        tone={grossMarginPct == null ? undefined : grossMarginPct >= 0 ? 'positive' : 'negative'}
                    />
                    <StatCard label="Amazon手数料" value={data.amazon_fee_usd != null ? `${usd(data.amazon_fee_usd)} (${usdAsJpy(data.amazon_fee_usd)})` : '-'} />
                    <StatCard label="FBA手数料" value={data.fba_fee_usd != null ? `${usd(data.fba_fee_usd)} (${usdAsJpy(data.fba_fee_usd)})` : '-'} />
                    <StatCard label="輸送費" value={usdAsJpy(data.shipping_cost_usd)} />
                    <StatCard label="関税(概算)" value={usdAsJpy(data.import_duty_usd)} />
                    <StatCard
                        label="実質利益"
                        value={usdAsJpy(candidate.unitProfitUsd)}
                        tone={candidate.unitProfitUsd == null ? undefined : candidate.unitProfitUsd >= 0 ? 'positive' : 'negative'}
                    />
                    <StatCard
                        label="実質利益率"
                        value={`${pct(candidate.marginPct)}${candidate.feeEstimated ? ' *' : ''}`}
                        tone={candidate.marginPct == null ? undefined : candidate.marginPct >= 0 ? 'positive' : 'negative'}
                    />
                    <StatCard
                        label="ROI(投下資本利益率)"
                        value={pct(data.roi_pct)}
                        tone={data.roi_pct == null ? undefined : data.roi_pct >= 0 ? 'positive' : 'negative'}
                    />
                    <StatCard
                        label="先月の販売個数"
                        value={
                            candidate.monthlySold != null
                                ? `${candidate.monthlySold.toLocaleString()}個`
                                : data.sales_rank_drops_30 != null
                                  ? `ランク変動30日 ${data.sales_rank_drops_30}回(推定)`
                                  : '-'
                        }
                    />
                    <StatCard
                        label="重量"
                        value={candidate.weightKg != null ? `${candidate.weightKg}kg${candidate.weightEstimated ? '(仮値)' : ''}` : '-'}
                    />
                    <StatCard label="粗選別時の価格差率" value={pct(candidate.priceDiffRateGross)} />
                </div>
                {candidate.feeEstimated ? (
                    <p className="text-xs text-slate-500">* 手数料データなし、仮値で計算</p>
                ) : null}
            </section>

            <section className="space-y-4 rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <h2 className="text-lg font-semibold text-white">その他情報</h2>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <StatCard label="ランキング" value={candidate.salesRank ?? '-'} />
                    <StatCard label="レビュー数" value={candidate.reviewCount ?? '-'} />
                    <StatCard
                        label="セラー数(競合)"
                        value={
                            data.competitor_seller_count != null ? (
                                data.competitor_seller_count
                            ) : (
                                <button
                                    type="button"
                                    onClick={handleFetchSellerCount}
                                    disabled={sellerCountFetching}
                                    className="text-sm text-cyan-300 underline decoration-cyan-700 underline-offset-2 hover:text-cyan-200 disabled:opacity-50"
                                >
                                    {sellerCountFetching ? '取得中...' : 'セラー数を取得'}
                                </button>
                            )
                        }
                    />
                    <StatCard
                        label="評価"
                        value={
                            data.rating != null || data.jp_rating != null
                                ? [data.rating != null ? `US ${data.rating.toFixed(1)}` : null, data.jp_rating != null ? `JP ${data.jp_rating.toFixed(1)}` : null]
                                      .filter(Boolean)
                                      .join(' / ')
                                : '-'
                        }
                    />
                    <StatCard label="価格変動(90日)" value={candidate.priceVolatility90d != null ? `±${(candidate.priceVolatility90d * 100).toFixed(0)}%` : '-'} />
                    <StatCard label="UPC" value={data.upc || '-'} />
                    <StatCard label="EAN" value={data.ean || '-'} />
                    <StatCard label="調査日時" value={formatDateTime(candidate.createdAt)} />
                </div>
                {sellerCountError ? <p className="text-sm text-rose-300">{sellerCountError}</p> : null}
            </section>

            <section className="space-y-4 rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <h2 className="text-lg font-semibold text-white">価格・ランキング推移</h2>
                    {history ? (
                        <div className="flex items-center gap-2 text-xs text-slate-400">
                            <span>取得日時: {formatDateTime(history.fetchedAt)}</span>
                            <button
                                type="button"
                                onClick={handleFetchHistory}
                                disabled={historyFetching}
                                className="rounded-lg bg-slate-800 px-3 py-1.5 font-semibold text-slate-200 hover:bg-slate-700 disabled:opacity-50"
                            >
                                {historyFetching ? '再取得中...' : '再取得'}
                            </button>
                        </div>
                    ) : null}
                </div>

                {historyLoading ? (
                    <p className="py-6 text-center text-slate-500">確認中...</p>
                ) : history ? (
                    <div className="grid gap-6 lg:grid-cols-2">
                        <div>
                            <p className="mb-2 text-sm text-slate-400">価格推移(USD)</p>
                            <div className="h-64">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={priceChartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                                        <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                                        <XAxis dataKey="time" tickFormatter={formatChartDate} stroke="#94a3b8" minTickGap={40} />
                                        <YAxis stroke="#94a3b8" domain={['auto', 'auto']} />
                                        <Tooltip
                                            labelFormatter={(time) => formatDateTime(time)}
                                            formatter={(value, name) => [
                                                value == null ? '-' : `$${Number(value).toFixed(2)}`,
                                                { new: '新品', amazon: 'Amazon', buyBox: 'カート価格' }[name] || name,
                                            ]}
                                        />
                                        <Line type="monotone" dataKey="buyBox" stroke="#22d3ee" dot={false} connectNulls name="buyBox" />
                                        <Line type="monotone" dataKey="new" stroke="#a78bfa" dot={false} connectNulls name="new" />
                                        <Line type="monotone" dataKey="amazon" stroke="#f472b6" dot={false} connectNulls name="amazon" />
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                        </div>
                        <div>
                            <p className="mb-2 text-sm text-slate-400">販売ランキング推移(数値が小さいほど売れている)</p>
                            <div className="h-64">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={rankChartData} margin={{ top: 10, right: 10, left: 10, bottom: 0 }}>
                                        <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                                        <XAxis dataKey="time" tickFormatter={formatChartDate} stroke="#94a3b8" minTickGap={40} />
                                        <YAxis
                                            stroke="#94a3b8"
                                            reversed
                                            domain={['auto', 'auto']}
                                            width={76}
                                            tickFormatter={(value) => value.toLocaleString()}
                                        />
                                        <Tooltip labelFormatter={(time) => formatDateTime(time)} formatter={(value) => [value, 'ランキング']} />
                                        <Line type="monotone" dataKey="rank" stroke="#34d399" dot={false} connectNulls />
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                        </div>
                        {historyError ? <p className="text-sm text-rose-300 lg:col-span-2">{historyError}</p> : null}
                    </div>
                ) : (
                    <div className="space-y-3 py-6 text-center">
                        <p className="text-slate-400">この商品の価格・ランキング履歴はまだ取得していません。</p>
                        {historyError ? <p className="text-sm text-rose-300">{historyError}</p> : null}
                        <button
                            type="button"
                            onClick={handleFetchHistory}
                            disabled={historyFetching}
                            className="rounded-2xl bg-cyan-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                        >
                            {historyFetching ? '取得中...' : '詳細データ(価格・ランキング履歴)を取得'}
                        </button>
                    </div>
                )}
            </section>

            <section className="flex flex-wrap items-center gap-2 rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <a
                    href={candidate.usUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-lg bg-slate-800 px-3 py-2 text-sm font-semibold text-slate-200 hover:bg-slate-700"
                >
                    Amazon (US) で見る
                </a>
                {candidate.jpUrl ? (
                    <a
                        href={candidate.jpUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="rounded-lg bg-slate-800 px-3 py-2 text-sm font-semibold text-slate-200 hover:bg-slate-700"
                    >
                        Amazon (JP) で見る
                    </a>
                ) : null}
                <a
                    href={keepaProductUrl(candidate.asin, 'US')}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-lg bg-slate-800 px-3 py-2 text-sm font-semibold text-slate-200 hover:bg-slate-700"
                >
                    Keepa (US) で見る
                </a>
                {candidate.jpAsin ? (
                    <a
                        href={keepaProductUrl(candidate.jpAsin, 'JP')}
                        target="_blank"
                        rel="noreferrer"
                        className="rounded-lg bg-slate-800 px-3 py-2 text-sm font-semibold text-slate-200 hover:bg-slate-700"
                    >
                        Keepa (JP) で見る
                    </a>
                ) : null}
                <button
                    type="button"
                    onClick={handleCopyLink}
                    className="rounded-lg bg-slate-800 px-3 py-2 text-sm font-semibold text-slate-200 hover:bg-slate-700"
                >
                    {copyStatus || 'リンクをコピー'}
                </button>
                <div className="ml-auto">
                    {candidate.alreadyFavorited ? (
                        <span className="inline-flex items-center gap-1.5 rounded-lg bg-amber-900/40 px-3 py-2 text-sm font-semibold text-amber-200">
                            <Star className="h-4 w-4" fill="currentColor" />
                            追加済み
                        </span>
                    ) : (
                        <button
                            type="button"
                            onClick={handleAddFavorite}
                            disabled={saving}
                            className="inline-flex items-center gap-1.5 rounded-lg bg-amber-400 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-amber-300 disabled:opacity-50"
                        >
                            <Star className="h-4 w-4" />
                            {saving ? '追加中...' : 'お気に入りに追加'}
                        </button>
                    )}
                </div>
            </section>
        </main>
    );
}
