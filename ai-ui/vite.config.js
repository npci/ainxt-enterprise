// SPDX-License-Identifier: MIT
import path from 'path';
import { fileURLToPath } from 'url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import prefixSelector from 'postcss-prefix-selector';

// All API traffic flows through a single versioned prefix: /ainxt/v1/api
// Regex key preserves the full path so FastAPI receives the complete URL unchanged.
// VITE_API_URL env var overrides the proxy target for production deployments.

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig( ( { command } ) => ( {
  // Production builds are served behind the /portal/ prefix (Caddy/Nginx).
  // The dev server uses "/" so root-absolute references in index.html and
  // manifest.json (/icons/*, /manifest.json) resolve correctly.
  base: command === 'build' ? '/portal/' : '/',

  // Load .env from the repository root, not from ai-ui/.
  //
  // Vite defaults envDir to the project root (this directory), so it only ever
  // reads ai-ui/.env — but this repo keeps a single .env at the top level,
  // shared with the FastAPI backend. That mismatch meant VITE_PII_ENCRYPTION_KEY
  // was never inlined into the bundle even though it was set alongside the
  // backend's PII_ENCRYPTION_KEY. With PII_PAYLOAD_ENCRYPTION_ENABLED=true the
  // server encrypts name/email, the browser had no key to decrypt them, and
  // every PII field rendered as the "—" placeholder with profile editing
  // disabled. Pointing envDir at the root keeps one .env as the single source
  // of truth for both halves of the key pair.
  envDir: path.resolve( __dirname, '..' ),
  plugins: [
    react(),
    tailwindcss(),
  ],

  resolve: {
    alias: {
      // @abs → AgentStudio/frontend/src
      // Allows ai-ui to import AgentStudio components without moving files:
      //   import BuildStudio from '@abs/BuildStudio.jsx'
      '@abs': path.resolve( __dirname, '../AgentStudio/frontend/src' ),
    },
    // Deduplicate shared packages so AgentStudio components (resolved from
    // AgentStudio/frontend/src/) use the SAME React/ReactDOM/Zustand instances
    // as ai-ui. Without this, Rollup tries to resolve 'react' relative to
    // AgentStudio/frontend/ where there is no node_modules, causing build errors.
    dedupe: [
      'react',
      'react-dom',
      'react/jsx-runtime',
      'react-router-dom',
      'zustand',
      'zundo',
      '@xyflow/react',
      '@dagrejs/dagre',
      'framer-motion',
      'axios',
      'react-markdown',
      'remark-gfm',
      'remark-math',
      'rehype-highlight',
      'rehype-katex',
      'uuid',
    ],
    // Tell Vite/Rollup to look in ai-ui/node_modules when resolving bare
    // specifiers from AgentStudio source files (which have no local node_modules).
    modules: [
      path.resolve( __dirname, 'node_modules' ),
      'node_modules',
    ],
  },

  css: {
    postcss: {
      plugins: [
        prefixSelector( {
          prefix: '[data-ac]',
          // Only scope CSS that belongs to AgentStudio — skip ai-ui's own files
          // and all node_modules (ReactFlow, Tailwind, etc.).
          // Without this guard, Tailwind utilities in ai-ui would be prefixed
          // with [data-ac] and stop applying to the host shell.
          transform( prefix, selector, prefixedSelector, filePath ) {
            if ( !filePath ) return selector;
            const norm = filePath.replace( /\\/g, '/' );
            if ( norm.includes( 'node_modules' ) ) return selector;
            if ( !norm.includes( 'AgentStudio/frontend/src' ) ) return selector;
            return prefixedSelector;
          },
        } ),
      ],
    },
  },

  build: {
    // Ensure all output filenames are content-hashed so browsers can
    // cache them indefinitely — a changed file always gets a new hash.
    rollupOptions: {
      output: {
        // Main entry chunk: app.[hash].js
        entryFileNames: 'assets/app.[hash].js',
        // Code-split chunks: [name].[hash].js
        chunkFileNames: 'assets/[name].[hash].js',
        // CSS, images, fonts: [name].[hash][ext]
        assetFileNames: 'assets/[name].[hash][extname]',
        // Split vendor (React, ReactDOM) into a separate chunk so it can
        // be cached independently from application code.
        manualChunks: {
          vendor: [ 'react', 'react-dom' ],
        },
      },
    },
  },

  server: {
    host: true,
    port: 5173,
    allowedHosts: [ '.trycloudflare.com' ],
    // Serve index.html for all non-asset routes so React Router handles navigation
    historyApiFallback: true,
    proxy: {
      '/ainxt/v1/api': {
        target: process.env.VITE_API_URL || 'http://localhost:8000',
        changeOrigin: true,
        secure: false,
        configure: ( proxy ) =>
        {
          proxy.on( 'proxyReq', ( proxyReq ) =>
          {
            // Disable compression so SSE tokens are not buffered
            proxyReq.setHeader( 'Accept-Encoding', 'identity' );
          } );
        },
      },
      // /health only exists under /ainxt/v1/api (see gateway.py's `_v1`
      // router mount) — mirrors nginx.conf's production `/health` rule.
      '/health': {
        target: process.env.VITE_API_URL || 'http://localhost:8000',
        changeOrigin: true,
        secure: false,
        rewrite: () => '/ainxt/v1/api/health',
      },
    },
  },

  test: {
    // sanitizeSvg.js relies on browser-global DOMParser/XMLSerializer.
    environment: 'jsdom',
  },
} ) );
