import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import KeepaFinderPage from './KeepaFinderPage.jsx';
import FavoritesPage from './FavoritesPage.jsx';
import AgentPage from './AgentPage.jsx';
import KeywordPoolPage from './KeywordPoolPage.jsx';
import SellerMiningPage from './SellerMiningPage.jsx';
import CandidateDetailPage from './CandidateDetailPage.jsx';
import SellerDetailPage from './SellerDetailPage.jsx';
import FinancePage from './FinancePage.jsx';
import PurchaseDetailPage from './PurchaseDetailPage.jsx';
import ShipmentDetailPage from './ShipmentDetailPage.jsx';
import SupplierDetailPage from './SupplierDetailPage.jsx';
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
    const isFinance = hash === '#finance';
    // #candidate/<ASIN>: このコードベース初のパラメータ付きハッシュルート。
    // ナビゲーションバーには追加しない(AgentPage一覧からのクリック遷移のみが導線)。
    const candidateAsin = hash.startsWith('#candidate/') ? decodeURIComponent(hash.slice('#candidate/'.length)) : null;
    // #seller/<sellerId>: #candidate/<ASIN>と同じパラメータ付きハッシュルートパターン。
    // ナビゲーションバーには追加しない(セラーマイニングページの一覧からのクリック遷移のみが導線)。
    // CEO: 「セラーサーチで見つけたセラーの結果をもう少しみやすくしたい。少なくとも、
    // そのセラーのページを一枚作ること。」
    const sellerId = hash.startsWith('#seller/') ? decodeURIComponent(hash.slice('#seller/'.length)) : null;
    // #purchase/<sdReceptionNo>, #shipment/<shipmentId>, #supplier/<名前>: 収支ページ
    // (FinancePage)の仕入れ一覧・納品便一覧からのクリック遷移のみが導線の詳細ページ
    // (CEO フィードバック: 「各仕入れや納品の詳細ページが欲しい」「仕入れ先の詳細ページが欲しい」)。
    const purchaseReceptionNo = hash.startsWith('#purchase/') ? decodeURIComponent(hash.slice('#purchase/'.length)) : null;
    const shipmentId = hash.startsWith('#shipment/') ? decodeURIComponent(hash.slice('#shipment/'.length)) : null;
    const supplierName = hash.startsWith('#supplier/') ? decodeURIComponent(hash.slice('#supplier/'.length)) : null;
    const isDashboard = !isFinder && !isFavorites && !isAgent && !isKeywords && !isSellerMining && !isFinance
        && !candidateAsin && !sellerId && !purchaseReceptionNo && !shipmentId && !supplierName;

    return (
        <div className="min-h-screen bg-slate-950 text-slate-100">
            <div className="mx-auto max-w-7xl px-3 py-3 sm:px-6 sm:py-4 lg:px-8">
                <nav className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-7" aria-label="メインメニュー">
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
                    <a
                        href="#finance"
                        className={`min-h-11 rounded-xl px-2 py-2 text-center text-xs font-semibold sm:px-4 sm:text-sm ${isFinance ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        💰 収支
                    </a>
                </nav>
                {candidateAsin ? (
                    <CandidateDetailPage asin={candidateAsin} onBack={() => { window.location.hash = '#agent'; }} />
                ) : sellerId ? (
                    <SellerDetailPage sellerId={sellerId} onBack={() => { window.location.hash = '#seller-mining'; }} />
                ) : purchaseReceptionNo ? (
                    <PurchaseDetailPage sdReceptionNo={purchaseReceptionNo} onBack={() => { window.location.hash = '#finance'; }} />
                ) : shipmentId ? (
                    <ShipmentDetailPage shipmentId={shipmentId} onBack={() => { window.location.hash = '#finance'; }} />
                ) : supplierName ? (
                    <SupplierDetailPage supplierName={supplierName} onBack={() => { window.location.hash = '#finance'; }} />
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
                ) : isFinance ? (
                    <FinancePage />
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
