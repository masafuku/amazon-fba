const API_BASE = import.meta.env.VITE_API_BASE || '';

export const saveRowsToDb = async (rows, metadata) => {
    const response = await fetch(`${API_BASE}/api/import`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            rows,
            metadata,
        }),
    });

    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }

    const json = await response.json();

    return {
        insertedCount: json.insertedCount ?? rows.length,
        batchId: json.batchId ?? metadata.batchId,
    };
};

export const loadRowsFromDb = async () => {
    const response = await fetch(`${API_BASE}/api/latest`);
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }
    return response.json();
};

export const loadFavorites = async () => {
    const response = await fetch(`${API_BASE}/api/favorites`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const json = await response.json();
    return json.favorites || [];
};

export const saveFavorite = async (favorite) => {
    const response = await fetch(`${API_BASE}/api/favorites`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(favorite),
    });
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    return response.json();
};

export const deleteFavorite = async (asin) => {
    const response = await fetch(`${API_BASE}/api/favorites?asin=${encodeURIComponent(asin)}`, { method: 'DELETE' });
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    return response.json();
};

export const loadAgentCandidates = async (days = 7) => {
    const params = new URLSearchParams({ days: String(days) });
    const response = await fetch(`${API_BASE}/api/agent/candidates?${params.toString()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const json = await response.json();
    return json.candidates || [];
};

export const loadAgentRuns = async (days = 30) => {
    const params = new URLSearchParams({ days: String(days) });
    const response = await fetch(`${API_BASE}/api/agent/runs?${params.toString()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const json = await response.json();
    return json.runs || [];
};

export const loadKeepaTokenStatus = async () => {
    const response = await fetch(`${API_BASE}/api/keepa/token`);
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }
    return response.json();
};

export const loadScanLoopStatus = async () => {
    const response = await fetch(`${API_BASE}/api/agent/scan-loop`);
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }
    return response.json();
};

export const controlScanLoop = async (action) => {
    const response = await fetch(`${API_BASE}/api/agent/scan-loop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
    });
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }
    return response.json();
};

export const discoverSellersForAsin = async ({ asin, maxSellers }) => {
    const response = await fetch(`${API_BASE}/api/seller-mining/discover-sellers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ asin, maxSellers }),
    });
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    return response.json();
};

export const expandFromSeller = async ({ sellerId, maxCandidates, seedAsin }) => {
    const response = await fetch(`${API_BASE}/api/seller-mining/expand`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sellerId, maxCandidates, seedAsin }),
    });
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    return response.json();
};

export const loadDbStatsFromDb = async () => {
    const response = await fetch(`${API_BASE}/api/health`);
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }
    return response.json();
};

export const listBatchesFromDb = async () => {
    const response = await fetch(`${API_BASE}/api/batches`);
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }
    return response.json();
};

export const loadBatchRowsFromDb = async (market, batchId) => {
    const params = new URLSearchParams({ market, batchId });
    const response = await fetch(`${API_BASE}/api/batch?${params.toString()}`);
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }
    return response.json();
};

export const searchKeepaProductFinder = async (payload) => {
    const response = await fetch(`${API_BASE}/api/keepa/product-finder`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
    });

    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }

    return response.json();
};

export const fetchKeepaProduct = async (payload) => {
    const requestBody = JSON.stringify(payload);
    const response = await fetch(`${API_BASE}/api/keepa/product`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: requestBody,
    });
    const responseText = await response.text();

    if (!response.ok) {
        const error = new Error(responseText || `HTTP ${response.status}`);
        error.debug = {
            command: `curl -i -X POST '${API_BASE}/api/keepa/product' -H 'Content-Type: application/json' --data '${requestBody.replace(/'/g, "'\\''")}'`,
            status: response.status,
            responseText,
        };
        throw error;
    }

    let data;
    try {
        data = JSON.parse(responseText);
    } catch {
        const error = new Error('APIの戻り値がJSONではありません。');
        error.debug = {
            command: `curl -i -X POST '${API_BASE}/api/keepa/product' -H 'Content-Type: application/json' --data '${requestBody.replace(/'/g, "'\\''")}'`,
            status: response.status,
            responseText,
        };
        throw error;
    }

    return {
        ...data,
        debug: {
            command: `curl -i -X POST '${API_BASE}/api/keepa/product' -H 'Content-Type: application/json' --data '${requestBody.replace(/'/g, "'\\''")}'`,
            status: response.status,
            responseText,
        },
    };
};

export const loadKeywordPool = async () => {
    const response = await fetch(`${API_BASE}/api/keyword-pool`);
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    const json = await response.json();
    return json.keywords || [];
};

export const addKeywordToPool = async (keyword) => {
    const response = await fetch(`${API_BASE}/api/keyword-pool`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keyword }),
    });
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    return response.json();
};

export const deleteKeywordFromPool = async (keyword) => {
    const response = await fetch(`${API_BASE}/api/keyword-pool?keyword=${encodeURIComponent(keyword)}`, { method: 'DELETE' });
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    return response.json();
};

export const loadSellerPool = async () => {
    const response = await fetch(`${API_BASE}/api/seller-pool`);
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    const json = await response.json();
    return json.sellers || [];
};

export const addSellerToPool = async (sellerId) => {
    const response = await fetch(`${API_BASE}/api/seller-pool`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sellerId }),
    });
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    return response.json();
};

export const deleteSellerFromPool = async (sellerId) => {
    const response = await fetch(`${API_BASE}/api/seller-pool?sellerId=${encodeURIComponent(sellerId)}`, { method: 'DELETE' });
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    return response.json();
};

export const loadKeepaFinderRun = async (runId) => {
    const params = new URLSearchParams({ runId });
    const response = await fetch(`${API_BASE}/api/keepa/finder-run?${params.toString()}`);
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }
    return response.json();
};
