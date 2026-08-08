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
