import { useEffect, useMemo, useState } from 'react';
import { Star } from 'lucide-react';
import { controlScanLoop, loadAgentCandidates, loadAgentRuns, loadKeepaTokenStatus, loadScanLoopStatus, lookupAsin, saveFavorite, setScanLoopMode } from './db';
import { formatDateTime, formatDuration, formatElapsedSince } from './formatters';

const EXCHANGE_RATE = 150;

const SCAN_LOOP_MODE_LABELS = {
    auto: '自動',
    'seller-mining': 'セラーマイニング固定',
    'keyword-search': 'キーワード検索固定',
};

// 合格ラインの多段階化(CEO: 「合格ラインは何段階かに分けてください」)。
const TIER_STYLES = {
    pass: { label: '合格', className: 'bg-emerald-900/60 text-emerald-200' },
    consider: { label: '要検討', className: 'bg-amber-900/60 text-amber-200' },
    reference: { label: '参考', className: 'bg-slate-700/60 text-slate-300' },
    reject: { label: '不合格', className: 'bg-slate-800 text-slate-400' },
};
const resolveTier = (candidate) => candidate.tier ?? (candidate.qualified ? 'pass' : 'reject');

export default function AgentPage() {
    const [candidates, setCandidates] = useState([]);
    const [runs, setRuns] = useState([]);
    const [tokenStatus, setTokenStatus] = useState(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);
    const [savingAsin, setSavingAsin] = useState('');
    const [days, setDays] = useState(7);
    const [showRejected, setShowRejected] = useState(false);
    const [showRunHistory, setShowRunHistory] = useState(false);
    const [sortKey, setSortKey] = useState('marginPct');
    const [sortOrder, setSortOrder] = useState('desc');
    const [scanLoopStatus, setScanLoopStatus] = useState(null);
    const [scanLoopBusy, setScanLoopBusy] = useState(false);
    const [lookupAsinInput, setLookupAsinInput] = useState('');
    const [lookupBusy, setLookupBusy] = useState(false);
    const [lookupError, setLookupError] = useState('');

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

    const refreshScanLoopStatus = async () => {
        try {
            setScanLoopStatus(await loadScanLoopStatus());
        } catch (scanLoopError) {
            setScanLoopStatus({ error: scanLoopError?.message || '検索ループの状態取得に失敗しました。' });
        }
    };

    const handleScanLoopAction = async (action) => {
        setScanLoopBusy(true);
        setError('');
        try {
            const result = await controlScanLoop(action);
            if (!result.ok) {
                setError(result.error || '検索ループの操作に失敗しました。');
            }
            setScanLoopStatus(result);
        } catch (scanLoopError) {
            setError(scanLoopError?.message || '検索ループの操作に失敗しました。');
        } finally {
            setScanLoopBusy(false);
        }
    };

    const handleScanLoopModeChange = async (mode) => {
        setScanLoopBusy(true);
        setError('');
        try {
            const result = await setScanLoopMode(mode);
            if (!result.ok) {
                setError(result.error || 'モードの切り替えに失敗しました。');
            }
            setScanLoopStatus(result);
        } catch (modeError) {
            setError(modeError?.message || 'モードの切り替えに失敗しました。');
        } finally {
            setScanLoopBusy(false);
        }
    };

    // CEO: 「ASIN指定で調査する入力UIを追加できますか？」。成功したら詳細ページに遷移する
    // (そちらで結果を表示すればよく、この一覧をここで更新する必要はない)。
    const handleAsinLookup = async () => {
        const asin = lookupAsinInput.trim().toUpperCase();
        if (!asin) return;
        setLookupBusy(true);
        setLookupError('');
        try {
            const result = await lookupAsin(asin);
            if (!result.ok) {
                setLookupError(result.error || 'ASINの調査に失敗しました。');
                return;
            }
            window.location.hash = `#candidate/${asin}`;
        } catch (lookupErr) {
            setLookupError(lookupErr?.message || 'ASINの調査に失敗しました。');
        } finally {
            setLookupBusy(false);
        }
    };

    useEffect(() => {
        refresh(days);
        refreshTokenStatus();
        refreshScanLoopStatus();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [days]);

    // 実行中の検索があれば状態表示に使う(バナー・実行履歴の「実行中」
    // バッジ)。自動ポーリングはしない(CEOの希望) - 最新状況を見たい
    // ときは「再読み込み」ボタンを押す。
    const runningRun = useMemo(() => runs.find((run) => run.status === 'running'), [runs]);

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
        const filtered = showRejected ? candidates : candidates.filter((item) => item.qualified);
        // 表面利益/表面利益率/手数料/輸送費はAPIの生フィールドではなくcandidate.dataから算出する値。
        // 既存の汎用ソート比較関数(candidate[sortKey]を直接参照する)にそのまま乗せられるよう、
        // ソート前に候補オブジェクトへ事前計算して付与する(SellerMiningPage.jsxのpassRateと同じパターン)。
        const base = filtered.map((item) => {
            const us = item.data?.us_price_usd;
            const jp = item.data?.jp_cost_usd;
            const amazonFee = item.data?.amazon_fee_usd;
            const fbaFee = item.data?.fba_fee_usd;
            const grossProfitUsd = us != null && jp != null ? us - jp : null;
            const grossMarginPct = grossProfitUsd != null && us ? grossProfitUsd / us : null;
            const feesUsd = amazonFee != null && fbaFee != null ? amazonFee + fbaFee : null;
            const shippingCostUsd = item.data?.shipping_cost_usd ?? null;
            const importDutyUsd = item.data?.import_duty_usd ?? null;
            const roiPct = item.data?.roi_pct ?? null;
            return { ...item, grossProfitUsd, grossMarginPct, feesUsd, shippingCostUsd, importDutyUsd, roiPct };
        });
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
                <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm">
                    <p className="text-slate-400">検索ループ(AWS)</p>
                    {scanLoopStatus?.error ? (
                        <p className="text-rose-300">{scanLoopStatus.error}</p>
                    ) : scanLoopStatus?.status === 'unavailable' ? (
                        <p className="text-slate-500 text-xs">{scanLoopStatus.note || '未対応の環境です。'}</p>
                    ) : (
                        <p className="text-xl font-semibold">
                            {scanLoopStatus?.status === 'running' ? (
                                <span className="inline-flex items-center gap-1.5 text-emerald-300">
                                    <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" aria-hidden="true" />
                                    稼働中
                                </span>
                            ) : scanLoopStatus?.status === 'stopping' ? (
                                <span className="text-amber-300">停止処理中</span>
                            ) : scanLoopStatus?.status === 'stopped' ? (
                                <span className="text-slate-400">停止中</span>
                            ) : (
                                <span className="text-slate-500">-</span>
                            )}
                        </p>
                    )}
                    <div className="mt-2 flex gap-2">
                        <button
                            type="button"
                            onClick={() => handleScanLoopAction('stop')}
                            disabled={scanLoopBusy || scanLoopStatus?.status === 'stopped' || scanLoopStatus?.status === 'stopping'}
                            className="rounded-lg bg-rose-950/60 px-2 py-1 text-xs font-semibold text-rose-200 hover:bg-rose-900 disabled:opacity-40"
                            title="実行中の検索は中断せず、次のサイクルから開始しないようにします"
                        >
                            停止
                        </button>
                        <button
                            type="button"
                            onClick={() => handleScanLoopAction('resume')}
                            disabled={scanLoopBusy || scanLoopStatus?.status === 'running'}
                            className="rounded-lg bg-emerald-950/60 px-2 py-1 text-xs font-semibold text-emerald-200 hover:bg-emerald-900 disabled:opacity-40"
                        >
                            再開
                        </button>
                        <button
                            type="button"
                            onClick={refreshScanLoopStatus}
                            className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-700"
                        >
                            更新
                        </button>
                    </div>
                    <div className="mt-2 flex items-center gap-2 border-t border-slate-800 pt-2">
                        <span className="text-xs text-slate-400">
                            モード: {SCAN_LOOP_MODE_LABELS[scanLoopStatus?.mode] ?? SCAN_LOOP_MODE_LABELS.auto}
                        </span>
                        <div className="flex gap-1">
                            {Object.entries(SCAN_LOOP_MODE_LABELS).map(([value, label]) => (
                                <button
                                    key={value}
                                    type="button"
                                    onClick={() => handleScanLoopModeChange(value)}
                                    disabled={scanLoopBusy || (scanLoopStatus?.mode ?? 'auto') === value}
                                    className={`rounded-lg px-2 py-1 text-xs font-semibold disabled:opacity-40 ${
                                        (scanLoopStatus?.mode ?? 'auto') === value
                                            ? 'bg-cyan-500 text-slate-950'
                                            : 'border border-slate-700 text-slate-200 hover:bg-slate-800'
                                    }`}
                                    title={
                                        value === 'auto'
                                            ? '深夜1:00〜6:00(JST、既定)はセラーマイニング、それ以外はキーワード検索'
                                            : '時間帯に関わらず次のサイクルからこのモードに固定します'
                                    }
                                >
                                    {label}
                                </button>
                            ))}
                        </div>
                    </div>
                </div>
                <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm">
                    <p className="text-slate-400">ASIN指定調査</p>
                    <div className="mt-2 flex gap-2">
                        <input
                            type="text"
                            value={lookupAsinInput}
                            onChange={(event) => setLookupAsinInput(event.target.value)}
                            onKeyDown={(event) => { if (event.key === 'Enter') handleAsinLookup(); }}
                            placeholder="B0XXXXXXXX"
                            className="w-32 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-white outline-none focus:border-cyan-400"
                        />
                        <button
                            type="button"
                            onClick={handleAsinLookup}
                            disabled={lookupBusy || !lookupAsinInput.trim()}
                            className="rounded-lg bg-cyan-500 px-3 py-1 text-xs font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                        >
                            {lookupBusy ? '調査中...' : '調査する'}
                        </button>
                    </div>
                    <p className="mt-1 text-xs text-slate-500">US/JPの実データで実質利益を計算し、詳細ページに遷移します</p>
                    {lookupError ? <p className="mt-1 text-xs text-rose-300">{lookupError}</p> : null}
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
                    <p className="text-xs text-cyan-400">最新状況は「再読み込み」で更新してください</p>
                </div>
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
                                    <th className="px-4 py-3 font-medium text-slate-400">画像</th>
                                    {renderSortHeader('判定', 'qualified')}
                                    {renderSortHeader('カテゴリ', 'category')}
                                    {renderSortHeader('セラー', 'sellerName')}
                                    {renderSortHeader('ASIN', 'asin')}
                                    {renderSortHeader('商品名', 'title')}
                                    {renderSortHeader('ROI(投下資本利益率)', 'roiPct')}
                                    {renderSortHeader('先月の販売個数', 'monthlySold')}
                                    {renderSortHeader('US価格($)', 'usPriceUsd')}
                                    {renderSortHeader('JP価格(円)', 'jpCostJpy')}
                                    {renderSortHeader('表面利益(US-JP)', 'grossProfitUsd')}
                                    {renderSortHeader('表面利益率', 'grossMarginPct')}
                                    {renderSortHeader('手数料(Amazon+FBA)', 'feesUsd')}
                                    {renderSortHeader('輸送費', 'shippingCostUsd')}
                                    {renderSortHeader('関税(概算)', 'importDutyUsd')}
                                    {renderSortHeader('実質利益', 'unitProfitUsd')}
                                    {renderSortHeader('実質利益率', 'marginPct')}
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
                                            {(() => {
                                                const tier = resolveTier(candidate);
                                                const style = TIER_STYLES[tier] || TIER_STYLES.reject;
                                                return (
                                                    <span
                                                        className={`rounded-lg px-2 py-1 text-xs font-semibold ${style.className}`}
                                                        title={tier === 'pass' ? '' : candidate.reason || ''}
                                                    >
                                                        {style.label}
                                                    </span>
                                                );
                                            })()}
                                        </td>
                                        <td className="px-4 py-3 text-slate-300">{candidate.category || '-'}</td>
                                        <td className="px-4 py-3 text-slate-300">{candidate.sellerName || candidate.sellerId || '-'}</td>
                                        <td className="px-4 py-3 font-semibold">
                                            <a
                                                href={`#candidate/${encodeURIComponent(candidate.asin)}`}
                                                className="text-cyan-300 underline decoration-cyan-700 underline-offset-2 hover:text-cyan-200"
                                            >
                                                {candidate.asin}
                                            </a>
                                        </td>
                                        <td className="max-w-xl px-4 py-3 text-slate-200">{candidate.title || '-'}</td>
                                        <td className={`px-4 py-3 font-semibold ${candidate.roiPct == null ? '' : candidate.roiPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`} title="実質利益 ÷ JP原価(投下資本)。US価格に対する実質利益率とは分母が異なる">
                                            {candidate.roiPct == null ? '-' : `${(candidate.roiPct * 100).toFixed(1)}%`}
                                        </td>
                                        <td className="px-4 py-3 font-semibold text-sky-300">
                                            {candidate.monthlySold != null ? `${candidate.monthlySold.toLocaleString()}個` : '-'}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">
                                            {candidate.usPriceUsd == null ? '-' : `$${Number(candidate.usPriceUsd).toFixed(2)}`}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">
                                            {candidate.jpCostJpy == null ? '-' : `¥${Number(candidate.jpCostJpy).toFixed(0)}`}
                                        </td>
                                        <td className={`px-4 py-3 font-semibold ${candidate.grossProfitUsd >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                            {candidate.grossProfitUsd == null ? '-' : `¥${Math.round(Number(candidate.grossProfitUsd) * EXCHANGE_RATE).toLocaleString()}`}
                                        </td>
                                        <td className={`px-4 py-3 font-semibold ${candidate.grossMarginPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                            {candidate.grossMarginPct == null ? '-' : `${(candidate.grossMarginPct * 100).toFixed(1)}%`}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">
                                            {candidate.feesUsd == null ? (
                                                '-'
                                            ) : (
                                                <details className="group">
                                                    <summary className="cursor-pointer list-none">
                                                        ¥{Math.round(Number(candidate.feesUsd) * EXCHANGE_RATE).toLocaleString()}
                                                    </summary>
                                                    <div className="mt-2 space-y-1 text-xs font-normal text-slate-400">
                                                        <p>
                                                            Amazon手数料: {candidate.data?.amazon_fee_usd != null
                                                                ? `$${Number(candidate.data.amazon_fee_usd).toFixed(2)} (¥${Math.round(Number(candidate.data.amazon_fee_usd) * EXCHANGE_RATE).toLocaleString()})`
                                                                : '-'}
                                                        </p>
                                                        <p>
                                                            FBA手数料: {candidate.data?.fba_fee_usd != null
                                                                ? `$${Number(candidate.data.fba_fee_usd).toFixed(2)} (¥${Math.round(Number(candidate.data.fba_fee_usd) * EXCHANGE_RATE).toLocaleString()})`
                                                                : '-'}
                                                        </p>
                                                    </div>
                                                </details>
                                            )}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200">
                                            {candidate.shippingCostUsd == null ? '-' : `¥${Math.round(Number(candidate.shippingCostUsd) * EXCHANGE_RATE).toLocaleString()}`}
                                        </td>
                                        <td className="px-4 py-3 text-slate-200" title="米国関税の概算(JP原価×12.5%、商品カテゴリにより実際の税率は変動)">
                                            {candidate.importDutyUsd == null ? '-' : `¥${Math.round(Number(candidate.importDutyUsd) * EXCHANGE_RATE).toLocaleString()}`}
                                        </td>
                                        <td className={`px-4 py-3 font-semibold ${candidate.unitProfitUsd >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                            {candidate.unitProfitUsd == null ? '-' : `¥${Math.round(Number(candidate.unitProfitUsd) * EXCHANGE_RATE).toLocaleString()}`}
                                        </td>
                                        <td className={`px-4 py-3 font-semibold ${candidate.marginPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                            {candidate.marginPct == null ? '-' : `${(candidate.marginPct * 100).toFixed(1)}%`}
                                            {candidate.feeEstimated ? (
                                                <span className="ml-1 text-xs text-slate-500" title="手数料データなし、仮値で計算">
                                                    *
                                                </span>
                                            ) : null}
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
        </main>
    );
}
