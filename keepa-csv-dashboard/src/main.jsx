import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import KeepaFinderPage from './KeepaFinderPage.jsx';
import FavoritesPage from './FavoritesPage.jsx';
import './index.css';

function Root() {
    const [hash, setHash] = React.useState(window.location.hash || '#dashboard');

    React.useEffect(() => {
        const onHashChange = () => setHash(window.location.hash || '#dashboard');
        window.addEventListener('hashchange', onHashChange);
        return () => window.removeEventListener('hashchange', onHashChange);
    }, []);

    const isFinder = hash === '#finder';
    const isFavorites = hash === '#favorites';

    return (
        <div className="min-h-screen bg-slate-950 text-slate-100">
            <div className="mx-auto max-w-7xl px-3 py-3 sm:px-6 sm:py-4 lg:px-8">
                <nav className="mb-4 grid grid-cols-3 gap-2" aria-label="メインメニュー">
                    <a
                        href="#dashboard"
                        className={`min-h-11 rounded-xl px-2 py-2 text-center text-xs font-semibold sm:px-4 sm:text-sm ${!isFinder ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        CSV分析
                    </a>
                    <a
                        href="#finder"
                        className={`min-h-11 rounded-xl px-2 py-2 text-center text-xs font-semibold sm:px-4 sm:text-sm ${isFinder ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        Keepa Finder
                    </a>
                    <a
                        href="#favorites"
                        className={`min-h-11 rounded-xl px-2 py-2 text-center text-xs font-semibold sm:px-4 sm:text-sm ${isFavorites ? 'bg-amber-400 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        お気に入り
                    </a>
                </nav>
                {isFinder ? <KeepaFinderPage /> : isFavorites ? <FavoritesPage /> : <App />}
            </div>
        </div>
    );
}

ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        <Root />
    </React.StrictMode>
);
