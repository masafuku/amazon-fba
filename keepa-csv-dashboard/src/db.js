const API_BASE = 'http://127.0.0.1:8001';

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

export const loadKeepaFinderRun = async (runId) => {
    const params = new URLSearchParams({ runId });
    const response = await fetch(`${API_BASE}/api/keepa/finder-run?${params.toString()}`);
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `HTTP ${response.status}`);
    }
    return response.json();
};
