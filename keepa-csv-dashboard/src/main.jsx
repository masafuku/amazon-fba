import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import KeepaFinderPage from './KeepaFinderPage.jsx';
import FavoritesPage from './FavoritesPage.jsx';
import AgentPage from './AgentPage.jsx';
import KeywordPoolPage from './KeywordPoolPage.jsx';
import SellerMiningPage from './SellerMiningPage.jsx';
import CandidateDetailPage from './CandidateDetailPage.jsx';
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
    const isAgent = hash === '#agent';
    const isKeywords = hash === '#keywords';
    const isSellerMining = hash === '#seller-mining';
    // #candidate/<ASIN>: このコードベース初のパラメータ付きハッシュルート。
    // ナビゲーションバーには追加しない(AgentPage一覧からのクリック遷移のみが導線)。
    const candidateAsin = hash.startsWith('#candidate/') ? decodeURIComponent(hash.slice('#candidate/'.length)) : null;
    const isDashboard = !isFinder && !isFavorites && !isAgent && !isKeywords && !isSellerMining && !candidateAsin;

    return (
        <div className="min-h-screen bg-slate-950 text-slate-100">
            <div className="mx-auto max-w-7xl px-3 py-3 sm:px-6 sm:py-4 lg:px-8">
                <nav className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-6" aria-label="メインメニュー">
                    <a
                        href="#dashboard"
                        className={`min-h-11 rounded-xl px-2 py-2 text-center text-xs font-semibold sm:px-4 sm:text-sm ${isDashboard ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
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
                        href="#agent"
                        className={`min-h-11 rounded-xl px-2 py-2 text-center text-xs font-semibold sm:px-4 sm:text-sm ${isAgent ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        🤖 エージェント
                    </a>
                    <a
                        href="#seller-mining"
                        className={`min-h-11 rounded-xl px-2 py-2 text-center text-xs font-semibold sm:px-4 sm:text-sm ${isSellerMining ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        🕵️ セラーマイニング
                    </a>
                    <a
                        href="#keywords"
                        className={`min-h-11 rounded-xl px-2 py-2 text-center text-xs font-semibold sm:px-4 sm:text-sm ${isKeywords ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        🔑 キーワード
                    </a>
                    <a
                        href="#favorites"
                        className={`min-h-11 rounded-xl px-2 py-2 text-center text-xs font-semibold sm:px-4 sm:text-sm ${isFavorites ? 'bg-amber-400 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        お気に入り
                    </a>
                </nav>
                {candidateAsin ? (
                    <CandidateDetailPage asin={candidateAsin} onBack={() => { window.location.hash = '#agent'; }} />
                ) : isFinder ? (
                    <KeepaFinderPage />
                ) : isFavorites ? (
                    <FavoritesPage />
                ) : isAgent ? (
                    <AgentPage />
                ) : isKeywords ? (
                    <KeywordPoolPage />
                ) : isSellerMining ? (
                    <SellerMiningPage />
                ) : (
                    <App />
                )}
            </div>
        </div>
    );
}

ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        <Root />
    </React.StrictMode>
);
