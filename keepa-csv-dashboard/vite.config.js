import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
    base: './',
    server: {
        host: true,
        port: 5173,
        strictPort: true,
        proxy: {
            '/api': {
                target: 'http://127.0.0.1:8001',
                changeOrigin: true,
            },
        },
        watch: {
            // Dropbox/iCloud sync can trigger change events before writes are fully complete.
            // Wait briefly so Vite does not parse half-written config/package files.
            awaitWriteFinish: {
                stabilityThreshold: 500,
                pollInterval: 100,
            },
            ignored: [
                '**/.dropbox/**',
                '**/.dropbox.cache/**',
                '**/.dropboxignore',
                '**/.dropbox.attr',
                '**/.DS_Store',
                '**/.~*',
                '**/~$*',
            ],
        },
    },
    plugins: [react()],
});
