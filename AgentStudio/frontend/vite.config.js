// SPDX-License-Identifier: MIT
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import prefixSelector from 'postcss-prefix-selector'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = (env.VITE_API_PROXY_TARGET || 'http://localhost:8000').replace(/\/+$/, '')
  const absPrefix = '/ainxt/v1/api/abs'

  return {
    plugins: [react()],
    css: {
      postcss: {
        plugins: [
          prefixSelector({
            prefix: '[data-ac]',
            // Only scope our own CSS files, not node_modules (ReactFlow, etc.)
            transform(prefix, selector, prefixedSelector, filePath, rule) {
              // Skip third-party styles entirely
              if (filePath && filePath.includes('node_modules')) return selector;

              // Skip content inside @keyframes — animation step selectors (0%, from, to) must stay bare
              const parent = rule?.parent;
              if (parent?.type === 'atrule' && parent.name && /keyframes/i.test(parent.name)) {
                return selector;
              }

              // :root, html, body, * → scope to our [data-ac] subtree ONLY.
              // We intentionally DROP the bare global selector so these rules
              // (background, font, color-scheme, box-sizing resets, CSS vars)
              // never leak out and restyle the host app this UI embeds into.
              const trimmedSelector = selector.trim();

              if ([':root', 'html', 'body', 'html body'].includes(trimmedSelector)) {
                return prefix;
              }

              if (trimmedSelector === '*') {
                return `${prefix} *`;
              }

              // Match both descendants inside the scoped root and the scoped root
              // itself, since several top-level React views carry data-ac directly.
              if (/^[.#]/.test(trimmedSelector)) {
                return `${prefix}${selector}, ${prefixedSelector}`;
              }

              return prefixedSelector;
            },
          }),
        ],
      },
    },
    server: {
      port: 5174,
      host: '0.0.0.0',
      proxy: {
        '^/ainxt/v1/api': {
          target: proxyTarget,
          changeOrigin: true,
          secure: false,
        },
        '/api/run-stream': {
          target: proxyTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, absPrefix),
          configure: (proxy, _options) => {
            proxy.on('proxyRes', (proxyRes, req, res) => {
              proxyRes.headers['cache-control'] = 'no-cache';
              proxyRes.headers['x-accel-buffering'] = 'no';
            });
          }
        },
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, absPrefix)
        }
      }
    }
  }
})
