import { useEffect, useMemo, useState } from 'react';
import { Trash2 } from 'lucide-react';
import { addKeywordToPool, deleteKeywordFromPool, loadKeywordPool } from './db';
import { formatDateTime } from './formatters';

const SOURCE_LABEL = {
    manual: '手動追加',
    favorite: 'お気に入り',
    expanded: '関連キーワード拡張',
};

const SOURCE_BADGE_CLASS = {
    manual: 'bg-cyan-900/40 text-cyan-200',
    favorite: 'bg-amber-900/40 text-amber-200',
    expanded: 'bg-violet-900/40 text-violet-200',
};

export default function KeywordPoolPage() {
    const [keywords, setKeywords] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [newKeyword, setNewKeyword] = useState('');
    const [adding, setAdding] = useState(false);
    const [deletingKeyword, setDeletingKeyword] = useState('');
    const [sourceFilter, setSourceFilter] = useState('all');
    const [sortKey, setSortKey] = useState('timesUsed');
    const [sortOrder, setSortOrder] = useState('asc');

    const refresh = async () => {
        setLoading(true);
        setError('');
        try {
            setKeywords(await loadKeywordPool());
        } catch (loadError) {
            setError(loadError?.message || 'キーワードプールの読み込みに失敗しました。');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        refresh();
    }, []);

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

    const handleAdd = async (event) => {
        event.preventDefault();
        const keyword = newKeyword.trim();
        if (!keyword) return;
        setAdding(true);
        setError('');
        try {
            const result = await addKeywordToPool(keyword);
            if (!result.added) {
                setError(`「${keyword}」はすでにプールに存在します。`);
            }
            setNewKeyword('');
            await refresh();
        } catch (addError) {
            setError(addError?.message || 'キーワードの追加に失敗しました。');
        } finally {
            setAdding(false);
        }
    };

    const handleDelete = async (keyword) => {
        setDeletingKeyword(keyword);
        setError('');
        try {
            await deleteKeywordFromPool(keyword);
            setKeywords((current) => current.filter((item) => item.keyword !== keyword));
        } catch (deleteError) {
            setError(deleteError?.message || `「${keyword}」の削除に失敗しました。`);
        } finally {
            setDeletingKeyword('');
        }
    };

    const visibleKeywords = useMemo(() => {
        const base = sourceFilter === 'all' ? keywords : keywords.filter((item) => item.source === sourceFilter);
        return [...base].sort((left, right) => {
            const dateKeys = new Set(['addedAt', 'lastUsedAt']);
            const leftValue = dateKeys.has(sortKey) ? Date.parse(left[sortKey]) || 0 : left[sortKey];
            const rightValue = dateKeys.has(sortKey) ? Date.parse(right[sortKey]) || 0 : right[sortKey];
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
    }, [keywords, sourceFilter, sortKey, sortOrder]);

    return (
        <main className="space-y-6">
            <header>
                <p className="text-sm uppercase tracking-[0.2em] text-cyan-300">Keyword / Category Agent</p>
                <h1 className="mt-1 text-3xl font-semibold text-white">🔑 キーワードプール</h1>
                <p className="mt-2 text-sm text-slate-400">
                    daily_scan.py が --keyword 未指定時に自動で選ぶキーワードの一覧です。お気に入り登録済み商品
                    (source=お気に入り)やKeepaカテゴリの関連キーワード拡張(source=関連キーワード拡張、
                    daily_scan.py --expand)に加えて、ここから手動でキーワードを追加できます。
                </p>
            </header>

            {error ? (
                <p className="rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">{error}</p>
            ) : null}

            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <h2 className="mb-3 text-lg font-semibold text-white">手動で追加</h2>
                <form onSubmit={handleAdd} className="flex flex-wrap gap-2">
                    <input
                        type="text"
                        value={newKeyword}
                        onChange={(event) => setNewKeyword(event.target.value)}
                        placeholder="例: kitchen gadget"
                        className="min-w-0 flex-1 rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white outline-none focus:border-cyan-400"
                    />
                    <button
                        type="submit"
                        disabled={adding || !newKeyword.trim()}
                        className="rounded-xl bg-cyan-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-50"
                    >
                        {adding ? '追加中...' : '追加'}
                    </button>
                </form>
            </section>

            <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                    <div className="text-slate-300">
                        プール <span className="font-semibold text-cyan-300">{keywords.length}</span>件 / 表示中 {visibleKeywords.length}件
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                        <label className="text-sm text-slate-400">
                            ソース
                            <select
                                value={sourceFilter}
                                onChange={(event) => setSourceFilter(event.target.value)}
                                className="ml-2 rounded-xl border border-slate-700 bg-slate-900 px-2 py-1 text-white outline-none focus:border-cyan-400"
                            >
                                <option value="all">すべて</option>
                                <option value="manual">手動追加</option>
                                <option value="favorite">お気に入り</option>
                                <option value="expanded">関連キーワード拡張</option>
                            </select>
                        </label>
                        <button
                            type="button"
                            onClick={refresh}
                            className="rounded-2xl bg-slate-800 px-4 py-2 text-sm font-semibold text-slate-200"
                        >
                            再読み込み
                        </button>
                    </div>
                </div>

                {loading ? (
                    <p className="py-12 text-center text-slate-500">読み込み中...</p>
                ) : visibleKeywords.length === 0 ? (
                    <p className="py-12 text-center text-slate-500">
                        キーワードプールは空です。上のフォームで手動追加するか、
                        daily_scan.py --seed-from-favorites / --expand で追加してください。
                    </p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full border-collapse text-left text-sm">
                            <thead className="bg-slate-950/90">
                                <tr>
                                    {renderSortHeader('キーワード', 'keyword')}
                                    {renderSortHeader('ソース', 'source')}
                                    {renderSortHeader('由来', 'seedKeyword')}
                                    {renderSortHeader('追加日時', 'addedAt')}
                                    {renderSortHeader('使用回数', 'timesUsed')}
                                    {renderSortHeader('合格件数', 'totalQualified')}
                                    {renderSortHeader('最終使用日時', 'lastUsedAt')}
                                    <th className="px-4 py-3 font-medium text-slate-400">操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {visibleKeywords.map((item) => (
                                    <tr key={item.keyword} className="border-t border-slate-800 bg-slate-950/80">
                                        <td className="px-4 py-3 font-semibold text-white">{item.keyword}</td>
                                        <td className="px-4 py-3">
                                            <span
                                                className={`rounded-lg px-2 py-1 text-xs font-semibold ${SOURCE_BADGE_CLASS[item.source] || 'bg-slate-800 text-slate-300'}`}
                                            >
                                                {SOURCE_LABEL[item.source] || item.source}
                                            </span>
                                        </td>
                                        <td className="px-4 py-3 text-slate-400">{item.seedKeyword || '-'}</td>
                                        <td className="px-4 py-3 text-slate-400">{formatDateTime(item.addedAt)}</td>
                                        <td className="px-4 py-3 text-slate-200">{item.timesUsed ?? 0}</td>
                                        <td className="px-4 py-3 text-slate-200">{item.totalQualified ?? 0}</td>
                                        <td className="px-4 py-3 text-slate-400">
                                            {item.lastUsedAt ? formatDateTime(item.lastUsedAt) : '未使用'}
                                        </td>
                                        <td className="px-4 py-3">
                                            <button
                                                type="button"
                                                onClick={() => handleDelete(item.keyword)}
                                                disabled={deletingKeyword === item.keyword}
                                                className="inline-flex items-center gap-1 rounded-lg bg-rose-950/60 px-2 py-1 text-xs font-semibold text-rose-200 hover:bg-rose-900 disabled:opacity-50"
                                            >
                                                <Trash2 className="h-3.5 w-3.5" />
                                                {deletingKeyword === item.keyword ? '削除中...' : '削除'}
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
