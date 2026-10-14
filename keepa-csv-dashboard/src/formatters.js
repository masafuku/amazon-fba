// 各ページで共通して使う「更新日時」の表示フォーマット。
// ISO文字列やUnix秒(Keepaのlastupdate等)を受け取り、日本語ロケールの
// 短い日時表記に変換する。不正な値やnullは '-' を返す。

export const formatDateTime = (value) => {
    if (value === null || value === undefined || value === '') return '-';

    let date;
    if (typeof value === 'number') {
        // Keepaのタイムスタンプはミリ秒/秒どちらの場合もあるため桁数で判定
        date = new Date(value < 1e12 ? value * 1000 : value);
    } else {
        date = new Date(value);
    }

    if (Number.isNaN(date.getTime())) return '-';

    return date.toLocaleString('ja-JP', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    });
};

// エージェント/セラーマイニングの実行履歴テーブルで共通して使う「所要時間」表示。
export const formatDuration = (seconds) => {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '-';
    if (seconds < 60) return `${seconds.toFixed(0)}秒`;
    return `${Math.floor(seconds / 60)}分${Math.round(seconds % 60)}秒`;
};

// 実行中の検索は経過時間を秒精度で表示したいので、durationSecondsではなく
// startedAtから現在時刻までを都度計算する。
export const formatElapsedSince = (startedAt) => {
    const startedMs = Date.parse(startedAt);
    if (Number.isNaN(startedMs)) return '-';
    return formatDuration((Date.now() - startedMs) / 1000);
};
