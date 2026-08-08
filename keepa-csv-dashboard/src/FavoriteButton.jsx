import { useState } from 'react';
import { Star } from 'lucide-react';
import { saveFavorite } from './db';

export default function FavoriteButton({ asin, title, source, data, className = '' }) {
    const [saved, setSaved] = useState(false);
    const [saving, setSaving] = useState(false);

    const handleClick = async () => {
        if (!asin || saving) return;
        setSaving(true);
        try {
            await saveFavorite({ asin, title, source, data });
            setSaved(true);
        } finally {
            setSaving(false);
        }
    };

    return (
        <button
            type="button"
            onClick={handleClick}
            disabled={saving}
            title={saved ? 'お気に入りに登録済み' : 'お気に入りに登録'}
            className={`inline-flex min-h-10 items-center justify-center gap-1 rounded-lg px-3 py-2 text-xs font-semibold ${saved ? 'bg-amber-400 text-slate-950' : 'bg-slate-800 text-slate-200 hover:bg-slate-700'} disabled:cursor-not-allowed disabled:opacity-60 ${className}`}
        >
            <Star className="h-3.5 w-3.5" fill={saved ? 'currentColor' : 'none'} />
            {saved ? '登録済み' : 'お気に入り'}
        </button>
    );
}
