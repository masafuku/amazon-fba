import { useEffect, useMemo, useState } from 'react';
import Papa from 'papaparse';
import {
    Bar,
    BarChart,
    CartesianGrid,
    Cell,
    Legend,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { Download, FileText, UploadCloud } from 'lucide-react';
import { loadDbStatsFromDb, loadRowsFromDb, saveRowsToDb } from './db';
import FavoriteButton from './FavoriteButton';
import { formatDateTime } from './formatters';

const SAMPLE_CSV = `ASIN,Title,Buy Box Price,Amazon Price,Merchant Price,Sales Rank,Condition,Buy Box Price (USD),List Price (USD),"Buy Box: Amazon (USD)","Buy Box: Merchant (USD)","Buy Box: Amazon (JPY)","Buy Box: Merchant (JPY)","Amazon Price (JPY)","Merchant Price (JPY)","Country"
B08N5WRWNW,Sample Product A,29.99,34.99,28.99,12345,New,29.99,32.99,29.99,0.00,4498,0,4498,0,US
B07PGL2ZSL,Sample Product B,15.49,18.99,14.99,23456,New,15.49,17.99,15.49,0.00,2324,0,2324,0,US
B07YQXSGQ5,Sample Product C,8.99,10.99,7.99,34567,New,8.99,9.99,8.99,0.00,1348,0,1348,0,US`;

const SAMPLE_JP_CSV = `ASIN,商品名,Buy Box: 現在価格
B08N5WRWNW,Sample Product A,4580
B07PGL2ZSL,Sample Product B,2380
B07YQXSGQ5,Sample Product C,1480`;

const DEFAULT_RATE = 150;
const DEFAULT_FEE = 1500;
const DEFAULT_PROFIT_FILTER = false;

const parseCsv = (content) => {
    const result = Papa.parse(content, { header: true, skipEmptyLines: true });
    return result.data;
};

const getLocaleValue = (row) => {
    const value = row?.['ロケール'] ?? row?.Locale ?? row?.locale ?? '';
    return String(value).trim().toLowerCase();
};

const getImageUrl = (row) => {
    const sourceRow = row?.usRow || row?.jpRow || row;
    const raw = sourceRow?.['画像'] ?? sourceRow?.Image ?? sourceRow?.image ?? '';
    const first = String(raw).split(';')[0].trim();
    return first || '';
};

const detectMarketFromRows = (rows) => {
    const first = rows?.[0] || {};
    const localeValue = getLocaleValue(first);
    if (localeValue.includes('co.jp')) return 'JP';
    if (localeValue.includes('.com') || localeValue === 'com') return 'US';

    const countryValue = String(first.Country || first.country || '').toUpperCase();
    if (countryValue === 'JP') return 'JP';
    if (countryValue === 'US') return 'US';

    return 'US';
};

const toNumber = (value) => {
    if (value === null || value === undefined) return null;
    const raw = String(value).trim();
    if (!raw) return null;

    // Keepa exports can include currency symbols (e.g. ￥, 円, $) and extra text.
    // Extract the first numeric token and normalize separators.
    const match = raw.match(/-?\d[\d,]*(?:\.\d+)?/);
    if (!match) return null;

    const num = parseFloat(match[0].replace(/,/g, ''));
    return Number.isFinite(num) ? num : null;
};

const getFieldEntryByKeywords = (row, keywords) => {
    const keys = Object.keys(row);
    for (const keyword of keywords) {
        const exact = keys.find((key) => key === keyword);
        if (exact) return { key: exact, value: row[exact] };
    }
    for (const keyword of keywords) {
        const fuzzy = keys.find((key) => key.includes(keyword));
        if (fuzzy) return { key: fuzzy, value: row[fuzzy] };
    }
    return { key: null, value: null };
};

const getFieldByKeywords = (row, keywords) => {
    return getFieldEntryByKeywords(row, keywords).value;
};

const getAsin = (row) => {
    if (row?.asin) return row.asin;
    const value = getFieldByKeywords(row, ['ASIN', '親会社ASIN', 'バリエーションASIN']);
    return value || '';
};

const getTitle = (row) => {
    if (row?.title) return row.title;
    const value = getFieldByKeywords(row, ['商品名', 'Title', 'title']);
    return value || '';
};

const getProductCategory = (row) => {
    if (row?.productCategory) return String(row.productCategory).trim();
    if (row?.product_category) return String(row.product_category).trim();
    const value = getFieldByKeywords(row, ['売れ筋ランキング: 参照', '売れ筋ランキング', '参照', 'Best Sellers Rank']);
    return String(value || '').trim();
};

const getBuyBoxUsd = (row) => {
    const market = String(row.csvMarket || row.Country || row.country || '').toUpperCase();

    const explicitUsdEntry = getFieldEntryByKeywords(row, [
        'Buy Box Price (USD)',
        'Buy Box: Amazon (USD)',
        'Buy Box: Merchant (USD)',
    ]);

    const explicitUsdNum = toNumber(explicitUsdEntry.value);
    if (explicitUsdNum !== null && explicitUsdNum >= 0) return explicitUsdNum;

    const genericEntry = getFieldEntryByKeywords(row, [
        'Buy Box Price',
        'Buy Box: 現在価格',
        'Buy Box: Price',
        '現在価格',
    ]);

    const keyText = String(genericEntry.key || '').toLowerCase();
    const valueText = String(genericEntry.value || '');
    const looksJpy = keyText.includes('jpy') || keyText.includes('円') || valueText.includes('円') || valueText.includes('￥');
    if (looksJpy) return null;

    const num = toNumber(genericEntry.value);
    if (num === null || num < 0) return null;

    // For US CSV, generic buy box current-price columns are typically USD even when header labels are localized.
    if (market === 'US') return num;

    const looksUsd = keyText.includes('usd') || valueText.includes('$');
    if (!looksUsd) return null;

    return num !== null && num >= 0 ? num : null;
};

const getJpCost = (row) => {
    const value = getFieldByKeywords(row, [
        'JP仕入れ',
        'JP Cost',
        'Amazon Price (JPY)',
        'Merchant Price (JPY)',
        'Buy Box: Amazon (JPY)',
        'Buy Box: Merchant (JPY)',
        'Amazon: 現在価格',
        '新品: 現在価格',
        '参考価格: 現在価格',
    ]);
    const num = toNumber(value);
    return num !== null && num >= 0 ? num : 0;
};

const pickLatestRowsByAsin = (rows) => {
    const latestByAsin = new Map();

    rows.forEach((row) => {
        const asin = getAsin(row).trim();
        if (!asin) return;

        const current = latestByAsin.get(asin);
        if (!current) {
            latestByAsin.set(asin, row);
            return;
        }

        const currentTs = Date.parse(current.importedAt || '') || 0;
        const nextTs = Date.parse(row.importedAt || '') || 0;
        if (nextTs >= currentTs) {
            latestByAsin.set(asin, row);
        }
    });

    return Array.from(latestByAsin.values());
};

const getLatestTimestamp = (...values) => {
    const valid = values.filter(Boolean).map((value) => new Date(value)).filter((date) => !Number.isNaN(date.getTime()));
    if (!valid.length) return null;
    return valid.reduce((latest, date) => (date > latest ? date : latest)).toISOString();
};

const mergeRowsByAsin = (usRows, jpRows) => {
    const latestUsRows = pickLatestRowsByAsin(usRows);
    const latestJpRows = pickLatestRowsByAsin(jpRows);

    const jpMap = new Map();
    latestJpRows.forEach((row) => {
        const asin = getAsin(row).trim();
        if (asin && !jpMap.has(asin)) jpMap.set(asin, row);
    });

    return latestUsRows
        .map((usRow) => {
            const asin = getAsin(usRow).trim();
            if (!asin || !jpMap.has(asin)) return null;
            const jpRow = jpMap.get(asin);
            return {
                asin,
                title: getTitle(usRow) || getTitle(jpRow) || '',
                productCategory: getProductCategory(usRow) || getProductCategory(jpRow) || '',
                importedAt: getLatestTimestamp(usRow.importedAt, jpRow.importedAt),
                usRow,
                jpRow,
            };
        })
        .filter(Boolean);
};

const getLastMonthSales = (row) => {
    const value = getFieldByKeywords(row, [
        '月間売上トレンド: 先月の購入',
        'Last month sales',
        '先月の購入',
    ]);
    const num = toNumber(value);
    return Number.isFinite(num) ? Math.round(num) : 0;
};

const getFbaPickPackUsd = (row) => {
    const value = getFieldByKeywords(row, [
        'FBA Pick&Pack 料金',
        'FBA Pick&Pack Fee',
        'FBA Pick and Pack Fee',
    ]);
    const num = toNumber(value);
    return num !== null && num >= 0 ? num : 0;
};

const getReferralPercent = (row) => {
    const value = getFieldByKeywords(row, [
        '紹介料％',
        'Referral Fee %',
        'Referral Percentage',
    ]);
    const num = toNumber(value);
    return num !== null && num >= 0 ? num : 15;
};

const isAmazonSeller = (row) => {
    const value = getFieldByKeywords(row, [
        'Buy Box: Buy Box セラー',
        'Buy Box セラー',
        'Buy Box: セラー',
        'Buy Box: Buy Box Seller',
        'Amazon セラー',
    ]);
    if (!value) return false;
    return String(value).toLowerCase().includes('amazon');
};

const calculateRows = (rows, exchangeRate, shippingCost) => {
    return rows.map((row) => {
        const usRow = row.usRow || row;
        const jpRow = row.jpRow || row;

        const buyBoxUsd = getBuyBoxUsd(usRow);
        const jpCost = getJpCost(jpRow);
        const lastMonthSales = getLastMonthSales(usRow);
        const amazonSeller = isAmazonSeller(usRow);
        const productCategory = getProductCategory(usRow) || getProductCategory(jpRow) || '未分類';
        const fbaPickPackUsd = getFbaPickPackUsd(usRow);
        const referralPercent = getReferralPercent(usRow);
        const usPriceJpy = buyBoxUsd !== null ? buyBoxUsd * exchangeRate : null;
        const priceDiffJpy = usPriceJpy !== null ? usPriceJpy - jpCost : null;
        const referralFeeJpy = usPriceJpy !== null ? usPriceJpy * (referralPercent / 100) : null;
        const pickPackFeeJpy = fbaPickPackUsd * exchangeRate;
        const amazonFee = usPriceJpy !== null ? referralFeeJpy + pickPackFeeJpy : null;
        const hasZeroPrice = buyBoxUsd === 0 || jpCost === 0;
        const profit = hasZeroPrice ? 0 : (usPriceJpy !== null ? usPriceJpy - jpCost - amazonFee - shippingCost : null);
        const profitRate = hasZeroPrice ? 0 : (profit !== null && usPriceJpy ? (profit / usPriceJpy) * 100 : null);

        return {
            ...row,
            buyBoxUsd,
            jpCost,
            productCategory,
            priceDiffJpy,
            lastMonthSales,
            amazonSeller,
            fbaPickPackUsd,
            referralPercent,
            referralFeeJpy,
            pickPackFeeJpy,
            usPriceJpy,
            amazonFee,
            profit,
            profitRate,
        };
    });
};

const buildHistogram = (rows) => {
    const buckets = [
        { name: '<0', min: -999999, max: 0 },
        { name: '0-1k', min: 0, max: 1000 },
        { name: '1k-3k', min: 1000, max: 3000 },
        { name: '3k-5k', min: 3000, max: 5000 },
        { name: '>5k', min: 5000, max: 999999 },
    ];
    return buckets.map((bucket) => ({
        name: bucket.name,
        count: rows.filter((row) => row.profit !== null && row.profit >= bucket.min && row.profit < bucket.max).length,
    }));
};

const downloadCsv = (rows) => {
    const header = ['ASIN', 'Title', 'Buy Box USD', 'US Price (JPY)', 'JP Price (JPY)', 'Price Diff (JPY)', 'Referral (%)', 'Referral Fee (JPY)', 'Pick&Pack (USD)', 'Pick&Pack (JPY)', 'Total Fee (JPY)', 'Profit', 'Profit Rate (%)', 'Last Month Sales', 'Amazon Seller'];
    const body = rows.map((row) => [
        getAsin(row),
        getTitle(row),
        row.buyBoxUsd !== null ? row.buyBoxUsd.toFixed(2) : '',
        row.usPriceJpy !== null ? row.usPriceJpy.toFixed(0) : '',
        row.jpCost !== null ? row.jpCost.toFixed(0) : '',
        row.priceDiffJpy !== null ? row.priceDiffJpy.toFixed(0) : '',
        row.referralPercent !== null ? row.referralPercent.toFixed(2) : '',
        row.referralFeeJpy !== null ? row.referralFeeJpy.toFixed(0) : '',
        row.fbaPickPackUsd !== null ? row.fbaPickPackUsd.toFixed(2) : '',
        row.pickPackFeeJpy !== null ? row.pickPackFeeJpy.toFixed(0) : '',
        row.amazonFee !== null ? row.amazonFee.toFixed(0) : '',
        row.profit !== null ? row.profit.toFixed(0) : '',
        row.profitRate !== null ? row.profitRate.toFixed(1) : '',
        row.lastMonthSales !== null ? row.lastMonthSales.toString() : '',
        row.amazonSeller ? 'Yes' : 'No',
    ]);
    const csv = [header, ...body].map((row) => row.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'keepa_research_export.csv';
    link.click();
    URL.revokeObjectURL(url);
};

const downloadAsinCsv = (rows) => {
    const asins = rows
        .map((row) => getAsin(row).trim())
        .filter((asin) => asin.length > 0);
    const uniqueAsins = [...new Set(asins)];

    const header = ['ASIN'];
    const body = uniqueAsins.map((asin) => [asin]);
    const csv = [header, ...body].map((row) => row.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(',')).join('\n');

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'asin_list.csv';
    link.click();
    URL.revokeObjectURL(url);
};

export default function App() {
    const [csvText, setCsvText] = useState(SAMPLE_CSV);
    const [usRows, setUsRows] = useState(parseCsv(SAMPLE_CSV).map((row) => ({ ...row, csvMarket: 'US' })));
    const [jpRows, setJpRows] = useState(parseCsv(SAMPLE_JP_CSV).map((row) => ({ ...row, csvMarket: 'JP' })));
    const [marketLabel, setMarketLabel] = useState('US');
    const [lastImportedAt, setLastImportedAt] = useState(null);
    const [dbStatus, setDbStatus] = useState('');
    const [dbStats, setDbStats] = useState(null);
    const [exchangeRate, setExchangeRate] = useState(() => {
        const storedRate = Number(window.localStorage.getItem('keepaExchangeRate'));
        return Number.isFinite(storedRate) && storedRate > 0 ? storedRate : DEFAULT_RATE;
    });
    const [shippingCost, setShippingCost] = useState(DEFAULT_FEE);
    const [profitOnly, setProfitOnly] = useState(DEFAULT_PROFIT_FILTER);
    const [excludeAmazonSeller, setExcludeAmazonSeller] = useState(false);
    const [excludeMissingPrices, setExcludeMissingPrices] = useState(false);
    const [excludeZeroSales, setExcludeZeroSales] = useState(false);
    const [sortKey, setSortKey] = useState('lastMonthSales');
    const [sortOrder, setSortOrder] = useState('desc');
    const [categoryVisibility, setCategoryVisibility] = useState({});

    useEffect(() => {
        window.localStorage.setItem('keepaExchangeRate', String(exchangeRate));
    }, [exchangeRate]);

    const mergedRows = useMemo(() => mergeRowsByAsin(usRows, jpRows), [usRows, jpRows]);
    const parsedRows = useMemo(() => calculateRows(mergedRows, exchangeRate, shippingCost), [mergedRows, exchangeRate, shippingCost]);
    const availableCategories = useMemo(() => {
        const categories = new Set();
        parsedRows.forEach((row) => {
            categories.add(getProductCategory(row) || '未分類');
        });
        return Array.from(categories).sort((left, right) => left.localeCompare(right, 'ja'));
    }, [parsedRows]);

    useEffect(() => {
        setCategoryVisibility((previous) => {
            const next = {};
            availableCategories.forEach((category) => {
                next[category] = Object.prototype.hasOwnProperty.call(previous, category) ? previous[category] : true;
            });
            return next;
        });
    }, [availableCategories]);

    const visibleRows = useMemo(() => {
        return parsedRows.filter((row) => {
            const category = getProductCategory(row) || '未分類';
            return categoryVisibility[category] !== false;
        });
    }, [parsedRows, categoryVisibility]);

    const filteredRows = useMemo(() => {
        return visibleRows
            .filter((row) => !profitOnly || (row.profit !== null && row.profit >= 1000))
            .filter((row) => !excludeAmazonSeller || !row.amazonSeller)
            .filter((row) => !excludeMissingPrices || ((row.buyBoxUsd ?? 0) > 0 && (row.jpCost ?? 0) > 0))
            .filter((row) => !excludeZeroSales || (row.lastMonthSales ?? 0) > 0)
            .sort((a, b) => {
                const aValue = a[sortKey];
                const bValue = b[sortKey];
                if (aValue === bValue) return 0;
                if (typeof aValue === 'string' || typeof bValue === 'string') {
                    const difference = String(aValue ?? '').localeCompare(String(bValue ?? ''), 'ja');
                    return sortOrder === 'asc' ? difference : -difference;
                }
                const numericA = Number.isFinite(Number(aValue)) ? Number(aValue) : -Infinity;
                const numericB = Number.isFinite(Number(bValue)) ? Number(bValue) : -Infinity;
                const difference = numericA - numericB;
                return sortOrder === 'asc' ? difference : -difference;
            });
    }, [visibleRows, profitOnly, excludeAmazonSeller, excludeMissingPrices, excludeZeroSales, sortKey, sortOrder]);

    const selectSortKey = (nextSortKey) => {
        if (sortKey === nextSortKey) {
            setSortOrder((current) => current === 'desc' ? 'asc' : 'desc');
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

    const summary = useMemo(() => {
        const validRows = filteredRows.filter((row) => row.profit !== null);
        const profitableRows = validRows.filter((row) => row.profit >= 0);
        const averageProfit = validRows.length ? validRows.reduce((sum, row) => sum + row.profit, 0) / validRows.length : 0;
        const best = validRows.reduce((bestRow, row) => (row.profit !== null && row.profit > (bestRow?.profit ?? -Infinity) ? row : bestRow), null);

        return {
            total: validRows.length,
            profitable: profitableRows.length,
            averageProfit: averageProfit || 0,
            bestItem: best,
        };
    }, [filteredRows]);

    const histogramData = useMemo(() => buildHistogram(filteredRows), [filteredRows]);

    useEffect(() => {
        let mounted = true;

        const loadInitialRows = async () => {
            const [latestResult, statsResult] = await Promise.allSettled([loadRowsFromDb(), loadDbStatsFromDb()]);
            if (!mounted) return;

            if (statsResult.status === 'fulfilled') {
                setDbStats(statsResult.value);
            }

            if (latestResult.status === 'rejected') {
                setDbStatus(`起動時ロード失敗: ${latestResult.reason?.message || 'unknown error'}`);
                return;
            }

            const latest = latestResult.value;
            const dbUsRows = Array.isArray(latest?.US?.rows) ? latest.US.rows : [];
            const dbJpRows = Array.isArray(latest?.JP?.rows) ? latest.JP.rows : [];

            if (dbUsRows.length > 0) {
                setUsRows(dbUsRows.map((row) => ({ ...row, csvMarket: 'US' })));
            }
            if (dbJpRows.length > 0) {
                setJpRows(dbJpRows.map((row) => ({ ...row, csvMarket: 'JP' })));
            }

            const loadedMarkets = [dbUsRows.length > 0 ? 'US' : null, dbJpRows.length > 0 ? 'JP' : null].filter(Boolean);
            if (loadedMarkets.length > 0) {
                setMarketLabel(loadedMarkets.join('/'));
                setLastImportedAt(latest?.US?.importedAt || latest?.JP?.importedAt || null);
                setDbStatus(`起動時ロード: ${loadedMarkets.join('/')} をSQLiteから復元`);
            }
        };

        loadInitialRows();

        return () => {
            mounted = false;
        };
    }, []);

    const handleFile = async (content, sourceFileName = 'uploaded.csv', marketOverride = null) => {
        setCsvText(content);
        const data = parseCsv(content);
        const market = marketOverride || detectMarketFromRows(data);
        const importedAt = new Date().toISOString();
        const batchId = `${importedAt}-${Math.random().toString(36).slice(2, 10)}`;

        const rowsWithMetadata = data.map((row) => ({
            ...row,
            csvMarket: market,
            importedAt,
            sourceFileName,
        }));

        if (market === 'JP') {
            setJpRows(rowsWithMetadata);
        } else {
            setUsRows(rowsWithMetadata);
        }
        setMarketLabel(market);
        setLastImportedAt(importedAt);

        try {
            const result = await saveRowsToDb(rowsWithMetadata, {
                market,
                importedAt,
                batchId,
                sourceFileName,
            });
            try {
                const stats = await loadDbStatsFromDb();
                setDbStats(stats);
            } catch {
                // Ignore stats refresh errors so a successful import still reports success.
            }
            setDbStatus(`SQL保存完了: ${result.insertedCount}件 (${market})`);
        } catch (error) {
            setDbStatus(`SQL保存失敗: ${error?.message || 'unknown error'}`);
        }
    };

    const handleDrop = (event) => {
        event.preventDefault();
        const file = event.dataTransfer.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
            handleFile(reader.result, file.name);
        };
        reader.readAsText(file);
    };

    const handleUsFileSelect = (event) => {
        const file = event.target.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
            handleFile(reader.result, file.name, 'US');
        };
        reader.readAsText(file);
    };

    const handleJpFileSelect = (event) => {
        const file = event.target.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
            handleFile(reader.result, file.name, 'JP');
        };
        reader.readAsText(file);
    };

    const handleFileSelect = (event) => {
        const file = event.target.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
            handleFile(reader.result, file.name);
        };
        reader.readAsText(file);
    };

    return (
        <div className="min-h-screen bg-slate-950 text-slate-100">
            <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
                <header className="mb-6 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                    <div>
                        <p className="text-sm uppercase tracking-[0.2em] text-cyan-300">Keepa CSV Research Dashboard</p>
                        <h1 className="text-3xl font-semibold text-white">CSVアップロードで即時利益分析</h1>
                        <p className="mt-2 text-slate-400">CSVをドラッグ＆ドロップして、利益計算、ソート、グラフ化までブラウザだけで完結します。</p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                        <button
                            type="button"
                            onClick={() => downloadCsv(filteredRows)}
                            className="inline-flex items-center gap-2 rounded-xl bg-cyan-500 px-4 py-3 text-sm font-semibold text-slate-950 transition hover:bg-cyan-400"
                        >
                            <Download className="h-4 w-4" />
                            CSVダウンロード
                        </button>
                        <button
                            type="button"
                            onClick={() => downloadAsinCsv(filteredRows)}
                            className="inline-flex items-center gap-2 rounded-xl bg-emerald-500 px-4 py-3 text-sm font-semibold text-slate-950 transition hover:bg-emerald-400"
                        >
                            <Download className="h-4 w-4" />
                            ASINリストCSV
                        </button>
                    </div>
                </header>

                <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
                    <aside className="rounded-3xl border border-slate-800 bg-slate-900/80 p-5 shadow-xl shadow-slate-950/20">
                        <div className="mb-5 flex items-center justify-between">
                            <div>
                                <p className="text-sm text-slate-400">設定パネル</p>
                                <h2 className="text-xl font-semibold text-white">分析パラメータ</h2>
                            </div>
                            <FileText className="h-5 w-5 text-cyan-400" />
                        </div>

                        <div className="space-y-4">
                            <label className="block text-sm text-slate-400">為替レート</label>
                            <input
                                type="number"
                                value={exchangeRate}
                                onChange={(event) => setExchangeRate(Number(event.target.value))}
                                className="w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-3 text-white outline-none transition focus:border-cyan-400"
                            />

                            <label className="block text-sm text-slate-400">FBA手数料＋送料</label>
                            <input
                                type="number"
                                value={shippingCost}
                                onChange={(event) => setShippingCost(Number(event.target.value))}
                                className="w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-3 text-white outline-none transition focus:border-cyan-400"
                            />

                            <label className="flex items-center gap-3 text-sm text-slate-400">
                                <input
                                    type="checkbox"
                                    checked={profitOnly}
                                    onChange={(event) => setProfitOnly(event.target.checked)}
                                    className="h-5 w-5 rounded border-slate-700 bg-slate-900 text-cyan-500"
                                />
                                利益1,000円以上のみ表示
                            </label>

                            <label className="flex items-center gap-3 text-sm text-slate-400">
                                <input
                                    type="checkbox"
                                    checked={excludeAmazonSeller}
                                    onChange={(event) => setExcludeAmazonSeller(event.target.checked)}
                                    className="h-5 w-5 rounded border-slate-700 bg-slate-900 text-cyan-500"
                                />
                                Amazonがセラーの場合を除外
                            </label>

                            <label className="flex items-center gap-3 text-sm text-slate-400">
                                <input
                                    type="checkbox"
                                    checked={excludeMissingPrices}
                                    onChange={(event) => setExcludeMissingPrices(event.target.checked)}
                                    className="h-5 w-5 rounded border-slate-700 bg-slate-900 text-cyan-500"
                                />
                                JP/US価格が揃っていない商品を除外
                            </label>

                            <label className="flex items-center gap-3 text-sm text-slate-400">
                                <input
                                    type="checkbox"
                                    checked={excludeZeroSales}
                                    onChange={(event) => setExcludeZeroSales(event.target.checked)}
                                    className="h-5 w-5 rounded border-slate-700 bg-slate-900 text-cyan-500"
                                />
                                販売個数0の商品を除外
                            </label>

                            <div className="grid gap-2 sm:grid-cols-2">
                                <button
                                    type="button"
                                    onClick={() => setSortOrder(sortOrder === 'desc' ? 'asc' : 'desc')}
                                    className="rounded-2xl bg-slate-800 px-4 py-3 text-sm font-semibold text-slate-200"
                                >
                                    並び替え: {sortOrder === 'desc' ? '高い順' : '低い順'}
                                </button>
                                <button
                                    type="button"
                                    onClick={() => {
                                        handleFile(SAMPLE_CSV, 'sample.us.csv', 'US');
                                        handleFile(SAMPLE_JP_CSV, 'sample.jp.csv', 'JP');
                                    }}
                                    className="rounded-2xl bg-slate-800 px-4 py-3 text-sm font-semibold text-slate-200"
                                >
                                    US/JPサンプルを読み込み
                                </button>
                            </div>

                            <div className="rounded-3xl border border-slate-800 bg-slate-950/60 p-4">
                                <div className="mb-3 flex items-center justify-between gap-3">
                                    <div>
                                        <p className="text-sm text-slate-400">商品カテゴリ</p>
                                        <p className="text-sm font-semibold text-white">表示 / 非表示</p>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            const next = {};
                                            availableCategories.forEach((category) => {
                                                next[category] = true;
                                            });
                                            setCategoryVisibility(next);
                                        }}
                                        className="rounded-xl bg-slate-800 px-3 py-2 text-xs font-semibold text-slate-200"
                                    >
                                        全表示
                                    </button>
                                </div>
                                <div className="max-h-56 space-y-2 overflow-auto pr-1">
                                    {availableCategories.length > 0 ? (
                                        availableCategories.map((category) => (
                                            <label key={category} className="flex items-start gap-3 rounded-2xl bg-slate-900/80 px-3 py-2 text-sm text-slate-300">
                                                <input
                                                    type="checkbox"
                                                    checked={categoryVisibility[category] !== false}
                                                    onChange={(event) => {
                                                        const checked = event.target.checked;
                                                        setCategoryVisibility((previous) => ({
                                                            ...previous,
                                                            [category]: checked,
                                                        }));
                                                    }}
                                                    className="mt-1 h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500"
                                                />
                                                <span className="break-words">{category}</span>
                                            </label>
                                        ))
                                    ) : (
                                        <p className="text-sm text-slate-500">カテゴリを読み込み中です。</p>
                                    )}
                                </div>
                            </div>
                        </div>
                    </aside>

                    <main className="space-y-6">
                        <section
                            className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10"
                            onDragOver={(event) => event.preventDefault()}
                            onDrop={handleDrop}
                        >
                            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                                <div>
                                    <h2 className="text-xl font-semibold text-white">US/JP CSVを読み込む</h2>
                                    <p className="text-sm text-slate-400">US版とJP版のCSVを読み込むと、ASINで突合して価格差を表示します。</p>
                                </div>
                                <div className="flex flex-wrap gap-2">
                                    <label className="inline-flex cursor-pointer items-center gap-2 rounded-2xl bg-cyan-500 px-4 py-3 text-sm font-semibold text-slate-950 transition hover:bg-cyan-400">
                                        <UploadCloud className="h-4 w-4" />
                                        US CSV
                                        <input type="file" accept=".csv" className="hidden" onChange={handleUsFileSelect} />
                                    </label>
                                    <label className="inline-flex cursor-pointer items-center gap-2 rounded-2xl bg-emerald-500 px-4 py-3 text-sm font-semibold text-slate-950 transition hover:bg-emerald-400">
                                        <UploadCloud className="h-4 w-4" />
                                        JP CSV
                                        <input type="file" accept=".csv" className="hidden" onChange={handleJpFileSelect} />
                                    </label>
                                    <label className="inline-flex cursor-pointer items-center gap-2 rounded-2xl bg-slate-700 px-4 py-3 text-sm font-semibold text-slate-100 transition hover:bg-slate-600">
                                        <UploadCloud className="h-4 w-4" />
                                        自動判定で1ファイル
                                        <input type="file" accept=".csv" className="hidden" onChange={handleFileSelect} />
                                    </label>
                                </div>
                            </div>
                            <div className="mt-5 rounded-3xl border border-dashed border-slate-700 bg-slate-950/50 px-5 py-14 text-center text-slate-400">
                                ドラッグ＆ドロップでCSVをここに読み込みます。
                            </div>
                            <div className="mt-4 flex flex-col gap-2 text-sm text-slate-400 md:flex-row md:items-center md:justify-between">
                                <p>判定リージョン: <span className="font-semibold text-cyan-300">{marketLabel}</span></p>
                                <p>保存日時: <span className="font-semibold text-slate-200">{lastImportedAt ? new Date(lastImportedAt).toLocaleString() : '-'}</span></p>
                            </div>
                            <div className="mt-2 flex flex-col gap-1 text-sm text-slate-400 md:flex-row md:items-center md:justify-between">
                                <p>US件数: <span className="font-semibold text-slate-200">{usRows.length}</span></p>
                                <p>JP件数: <span className="font-semibold text-slate-200">{jpRows.length}</span></p>
                                <p>ASIN突合件数: <span className="font-semibold text-cyan-300">{mergedRows.length}</span></p>
                            </div>
                            <div className="mt-3 grid gap-2 rounded-2xl border border-slate-800 bg-slate-950/60 p-4 text-sm text-slate-300 md:grid-cols-2">
                                <p>DBファイル: <span className="font-semibold text-white">{dbStats?.dbFileName || '-'}</span></p>
                                <p>格納件数: <span className="font-semibold text-white">{dbStats?.rowCount ?? 0}</span></p>
                            </div>
                            <p className="mt-2 text-sm text-slate-400">{dbStatus || 'CSV読込時にSQLiteへ自動保存されます。'}</p>
                        </section>

                        <section className="grid gap-6 lg:grid-cols-2">
                            <div className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                                <p className="text-sm uppercase tracking-[0.2em] text-slate-400">KPI</p>
                                <div className="mt-5 grid gap-4 sm:grid-cols-2">
                                    <div className="rounded-3xl bg-slate-950/80 p-4">
                                        <p className="text-sm text-slate-400">分析件数（突合済み）</p>
                                        <p className="mt-2 text-3xl font-semibold text-white">{summary.total}</p>
                                    </div>
                                    <div className="rounded-3xl bg-slate-950/80 p-4">
                                        <p className="text-sm text-slate-400">黒字商品数</p>
                                        <p className="mt-2 text-3xl font-semibold text-white">{summary.profitable}</p>
                                    </div>
                                    <div className="rounded-3xl bg-slate-950/80 p-4">
                                        <p className="text-sm text-slate-400">平均想定純利益</p>
                                        <p className="mt-2 text-3xl font-semibold text-white">¥{summary.averageProfit.toFixed(0)}</p>
                                    </div>
                                    <div className="rounded-3xl bg-slate-950/80 p-4">
                                        <p className="text-sm text-slate-400">最高利益商品</p>
                                        <p className="mt-2 text-lg font-semibold text-white">{summary.bestItem?.Title || summary.bestItem?.title || 'なし'}</p>
                                    </div>
                                </div>
                            </div>

                            <div className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                                <p className="text-sm uppercase tracking-[0.2em] text-slate-400">利益分布</p>
                                <div className="mt-5 h-72">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <BarChart data={histogramData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                                            <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                                            <XAxis dataKey="name" stroke="#94a3b8" />
                                            <YAxis stroke="#94a3b8" />
                                            <Tooltip formatter={(value) => [`${value}`, '件数']} />
                                            <Legend />
                                            <Bar dataKey="count" fill="#22c55e">
                                                {histogramData.map((entry, index) => (
                                                    <Cell key={`cell-${index}`} fill={entry.count > 0 ? '#22c55e' : '#334155'} />
                                                ))}
                                            </Bar>
                                        </BarChart>
                                    </ResponsiveContainer>
                                </div>
                            </div>
                        </section>

                        <section className="rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-xl shadow-slate-950/10">
                            <div className="mb-5 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                                <div>
                                    <h2 className="text-xl font-semibold text-white">商品一覧</h2>
                                    <p className="text-sm text-slate-400">ASIN突合済みのUS価格、JP価格、差額、純利益、利益率を確認できます。</p>
                                </div>
                                <p className="text-sm text-slate-400">表示中: <span className="font-semibold text-cyan-300">{filteredRows.length}</span> 件</p>
                            </div>

                            <div className="overflow-hidden rounded-3xl border border-slate-800">
                                <div className="hidden md:block">
                                    <table className="min-w-full border-collapse text-left text-sm">
                                        <thead className="bg-slate-950/90">
                                            <tr>
                                                <th className="px-4 py-3 font-medium text-slate-400">画像</th>
                                                {renderSortHeader('ASIN', 'asin')}
                                                {renderSortHeader('タイトル', 'title')}
                                                {renderSortHeader('商品カテゴリ', 'productCategory')}
                                                {renderSortHeader('US価格($)', 'buyBoxUsd')}
                                                {renderSortHeader('US価格(円)', 'usPriceJpy')}
                                                {renderSortHeader('JP価格(円)', 'jpCost')}
                                                {renderSortHeader('差額(円)', 'priceDiffJpy')}
                                                {renderSortHeader('手数料合計(円)', 'amazonFee')}
                                                {renderSortHeader('先月売上', 'lastMonthSales')}
                                                {renderSortHeader('Amazonセラー', 'amazonSeller')}
                                                {renderSortHeader('純利益(円)', 'profit')}
                                                {renderSortHeader('利益率(%)', 'profitRate')}
                                                {renderSortHeader('更新日時', 'importedAt')}
                                                <th className="px-4 py-3 font-medium text-slate-400">リンク</th>
                                                <th className="px-4 py-3 font-medium text-slate-400">お気に入り</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {filteredRows.map((row, index) => (
                                                <tr key={`${getAsin(row) || index}-${index}`} className="border-t border-slate-800 bg-slate-950/80 hover:bg-slate-900">
                                                    <td className="px-4 py-3 text-slate-100">
                                                        {getImageUrl(row) ? (
                                                            <img
                                                                src={getImageUrl(row)}
                                                                alt={getTitle(row) || 'thumbnail'}
                                                                className="h-12 w-12 rounded-md border border-slate-700 object-cover"
                                                                loading="lazy"
                                                            />
                                                        ) : (
                                                            <span className="text-slate-500">-</span>
                                                        )}
                                                    </td>
                                                    <td className="px-4 py-3 text-slate-100">{getAsin(row)}</td>
                                                    <td className="px-4 py-3 text-slate-100">{getTitle(row)}</td>
                                                    <td className="px-4 py-3 text-slate-100">{row.productCategory || '未分類'}</td>
                                                    <td className="px-4 py-3 text-slate-100">{row.buyBoxUsd !== null ? row.buyBoxUsd.toFixed(2) : '-'}</td>
                                                    <td className="px-4 py-3 text-slate-100">{row.usPriceJpy !== null ? row.usPriceJpy.toFixed(0) : '-'}</td>
                                                    <td className="px-4 py-3 text-slate-100">{row.jpCost !== null ? row.jpCost.toFixed(0) : '-'}</td>
                                                    <td className={`px-4 py-3 font-semibold ${row.priceDiffJpy >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{row.priceDiffJpy !== null ? row.priceDiffJpy.toFixed(0) : '-'}</td>
                                                    <td className="px-4 py-3 text-slate-100">
                                                        <details className="group">
                                                            <summary className="cursor-pointer list-none font-semibold text-cyan-300">
                                                                {row.amazonFee !== null ? `¥${row.amazonFee.toFixed(0)}` : '-'}
                                                            </summary>
                                                            <div className="mt-2 space-y-1 text-xs text-slate-400">
                                                                <p>紹介料(%): {row.referralPercent !== null ? row.referralPercent.toFixed(2) : '-'}</p>
                                                                <p>紹介料(円): ¥{row.referralFeeJpy !== null ? row.referralFeeJpy.toFixed(0) : '-'}</p>
                                                                <p>Pick&Pack($): {row.fbaPickPackUsd !== null ? row.fbaPickPackUsd.toFixed(2) : '-'}</p>
                                                                <p>Pick&Pack(円): ¥{row.pickPackFeeJpy !== null ? row.pickPackFeeJpy.toFixed(0) : '-'}</p>
                                                            </div>
                                                        </details>
                                                    </td>
                                                    <td className="px-4 py-3 text-slate-100">{row.lastMonthSales !== null ? row.lastMonthSales : '-'}</td>
                                                    <td className="px-4 py-3 text-slate-100">{row.amazonSeller ? 'Yes' : 'No'}</td>
                                                    <td className={`px-4 py-3 font-semibold ${row.profit >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{row.profit !== null ? row.profit.toFixed(0) : '-'}</td>
                                                    <td className={`px-4 py-3 font-semibold ${row.profitRate >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{row.profitRate !== null ? row.profitRate.toFixed(1) : '-'}</td>
                                                    <td className="px-4 py-3 text-slate-400">{formatDateTime(row.importedAt)}</td>
                                                    <td className="px-4 py-3">
                                                        <div className="flex flex-wrap gap-2">
                                                            <a
                                                                href={`https://www.amazon.com/dp/${getAsin(row)}`}
                                                                target="_blank"
                                                                rel="noreferrer"
                                                                className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-700"
                                                            >
                                                                US
                                                            </a>
                                                            <a
                                                                href={`https://www.amazon.co.jp/dp/${getAsin(row)}`}
                                                                target="_blank"
                                                                rel="noreferrer"
                                                                className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-700"
                                                            >
                                                                JP
                                                            </a>
                                                        </div>
                                                    </td>
                                                    <td className="px-4 py-3">
                                                        <FavoriteButton asin={getAsin(row)} title={getTitle(row)} source="csv" data={row} />
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>

                                <div className="space-y-4 md:hidden">
                                    {filteredRows.map((row, index) => (
                                        <div key={`${getAsin(row) || index}-${index}`} className="rounded-3xl border border-slate-800 bg-slate-950/90 p-4">
                                            {getImageUrl(row) ? (
                                                <img
                                                    src={getImageUrl(row)}
                                                    alt={getTitle(row) || 'thumbnail'}
                                                    className="mb-3 h-16 w-16 rounded-lg border border-slate-700 object-cover"
                                                    loading="lazy"
                                                />
                                            ) : null}
                                            <div className="flex flex-wrap gap-2 text-sm text-slate-400">
                                                <span>ASIN: {getAsin(row)}</span>
                                                <span>カテゴリ: {row.productCategory || '未分類'}</span>
                                                <span>US: {row.buyBoxUsd !== null ? `$${row.buyBoxUsd.toFixed(2)}` : '-'}</span>
                                            </div>
                                            <div className="mt-3 flex flex-wrap items-center gap-2">
                                                <FavoriteButton asin={getAsin(row)} title={getTitle(row)} source="csv" data={row} />
                                                <a
                                                    href={`https://www.amazon.com/dp/${getAsin(row)}`}
                                                    target="_blank"
                                                    rel="noreferrer"
                                                    className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-700"
                                                >
                                                    US
                                                </a>
                                                <a
                                                    href={`https://www.amazon.co.jp/dp/${getAsin(row)}`}
                                                    target="_blank"
                                                    rel="noreferrer"
                                                    className="rounded-lg bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-700"
                                                >
                                                    JP
                                                </a>
                                            </div>
                                            <div className="mt-3 space-y-2">
                                                <p className="text-base font-semibold text-white">{getTitle(row)}</p>
                                                <p className="text-sm text-slate-400">US価格(円): ¥{row.usPriceJpy !== null ? row.usPriceJpy.toFixed(0) : '-'}</p>
                                                <p className="text-sm text-slate-400">JP価格: ¥{row.jpCost !== null ? row.jpCost.toFixed(0) : '-'}</p>
                                                <p className={`text-sm font-semibold ${row.priceDiffJpy >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>差額: ¥{row.priceDiffJpy !== null ? row.priceDiffJpy.toFixed(0) : '-'}</p>
                                                <details className="text-sm text-slate-400">
                                                    <summary className="cursor-pointer font-semibold text-cyan-300">
                                                        手数料合計: {row.amazonFee !== null ? `¥${row.amazonFee.toFixed(0)}` : '-'}
                                                    </summary>
                                                    <div className="mt-2 space-y-1 text-xs text-slate-400">
                                                        <p>紹介料(%): {row.referralPercent !== null ? row.referralPercent.toFixed(2) : '-'}</p>
                                                        <p>紹介料(円): ¥{row.referralFeeJpy !== null ? row.referralFeeJpy.toFixed(0) : '-'}</p>
                                                        <p>Pick&Pack($): {row.fbaPickPackUsd !== null ? row.fbaPickPackUsd.toFixed(2) : '-'}</p>
                                                        <p>Pick&Pack(円): ¥{row.pickPackFeeJpy !== null ? row.pickPackFeeJpy.toFixed(0) : '-'}</p>
                                                    </div>
                                                </details>
                                                <p className="text-sm text-slate-400">先月売上: {row.lastMonthSales !== null ? row.lastMonthSales : '-'}</p>
                                                <p className="text-sm text-slate-400">Amazonセラー: {row.amazonSeller ? 'Yes' : 'No'}</p>
                                                <p className={`text-sm font-semibold ${row.profit >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>純利益: ¥{row.profit !== null ? row.profit.toFixed(0) : '-'}</p>
                                                <p className="text-sm text-slate-400">利益率: {row.profitRate !== null ? `${row.profitRate.toFixed(1)}%` : '-'}</p>
                                                <p className="text-sm text-slate-500">更新日時: {formatDateTime(row.importedAt)}</p>
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        </section>
                    </main>
                </div>
            </div>
        </div>
    );
}
