import { defineConfig } from 'vite'
import { fileURLToPath } from 'node:url'

const stub = (f: string): string => fileURLToPath(new URL('./' + f, import.meta.url))

// Builds the Midum Workbench (CodinGame monaco-vscode-api) into a static bundle served by Midum's
// localhost server under /wb/. Config follows the official monaco-vscode-api demo.
export default defineConfig({
  base: '/wb/',
  // Served from localhost only, so minifying buys little but costs several GB of RAM during the build.
  // Set MIDUM_MINIFY=1 to enable it on a machine with plenty of memory.
  build: { target: 'esnext', chunkSizeWarningLimit: 100000, sourcemap: false, minify: process.env.MIDUM_MINIFY === '1' ? 'esbuild' : false, reportCompressedSize: false, rollupOptions: { maxParallelFileOps: 2 } },
  worker: { format: 'es' },
  plugins: [
    {
      // Vite's build-import-analysis rewrites `const {a, b} = await import('x').then(n => n.main)` into a destructuring
      // of the module namespace itself, so `main` is never unwrapped and the TextMate worker gets `undefined`.
      // Rewriting to `= (await import('x')).main` keeps the semantics and sidesteps that optimisation.
      name: 'fix-destructured-dynamic-import',
      enforce: 'pre',
      transform(code, id) {
        if (!id.includes('@codingame') || !code.includes('.then(function (n) { return n.')) return null
        const out = code.replace(/=\s*await import\(([^)]+)\)\.then\(function \(n\) \{ return n\.(\w+); \}\)/g, '= (await import($1)).$2')
        return out === code ? null : { code: out, map: null }
      }
    },
    {
      // VS Code CSS must be exposed as strings (registerCss adopts them as constructable stylesheets)
      name: 'load-vscode-css-as-string',
      enforce: 'pre',
      async resolveId(source, importer, options) {
        const resolved = await this.resolve(source, importer, options)
        if (resolved == null) return undefined
        if (/node_modules\/(@codingame\/monaco-vscode|vscode|monaco-editor).*\.css$/.test(resolved.id)) {
          return { ...resolved, id: resolved.id + '?inline' }
        }
        return undefined
      }
    }
  ],
  esbuild: { minifySyntax: false },
  optimizeDeps: { include: ['@codingame/monaco-vscode-api', '@codingame/monaco-vscode-api/extensions', '@codingame/monaco-vscode-api/monaco', 'vscode/localExtensionHost'] },
  resolve: {
    dedupe: ['vscode'],
    alias: [
      { find: /^node:diagnostics_channel$/, replacement: stub('stub-diagnostics-channel.js') },
      { find: /^node:fs\/promises$/, replacement: stub('stub-empty.js') }
    ]
  },
  server: { port: 5173, host: '127.0.0.1' }
})
