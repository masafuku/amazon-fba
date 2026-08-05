import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import KeepaFinderPage from './KeepaFinderPage.jsx';
import './index.css';

function Root() {
    const [hash, setHash] = React.useState(window.location.hash || '#dashboard');

    React.useEffect(() => {
        const onHashChange = () => setHash(window.location.hash || '#dashboard');
        window.addEventListener('hashchange', onHashChange);
        return () => window.removeEventListener('hashchange', onHashChange);
    }, []);

    const isFinder = hash === '#finder';

    return (
        <div className="min-h-screen bg-slate-950 text-slate-100">
            <div className="mx-auto max-w-7xl px-4 py-4 sm:px-6 lg:px-8">
                <div className="mb-4 flex flex-wrap items-center gap-2">
                    <a
                        href="#dashboard"
                        className={`rounded-xl px-4 py-2 text-sm font-semibold ${!isFinder ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        CSV分析
                    </a>
                    <a
                        href="#finder"
                        className={`rounded-xl px-4 py-2 text-sm font-semibold ${isFinder ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-100'}`}
                    >
                        Keepa Finder
                    </a>
                </div>
                {isFinder ? <KeepaFinderPage /> : <App />}
            </div>
        </div>
    );
}

ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        <Root />
    </React.StrictMode>
);
