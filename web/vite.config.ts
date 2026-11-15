import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { resolve } from 'node:path'

// https://vite.dev/config/
export default defineConfig({
  base: './',  // works from any folder or static host
  plugins: [
    react(),
    {
      // /ruraldemo (no slash) -> the Sakleshpura rural demo page in ruraldemo/index.html
      name: 'ruraldemo-route',
      configureServer(server) {
        server.middlewares.use((req, _res, next) => {
          if (req.url === '/ruraldemo' || req.url?.startsWith('/ruraldemo?')) req.url = req.url.replace('/ruraldemo', '/ruraldemo/')
          next()
        })
      },
    },
  ],
  build: { rollupOptions: { input: { main: resolve(__dirname, 'index.html'), crew: resolve(__dirname, 'crew.html'), ruraldemo: resolve(__dirname, 'ruraldemo/index.html') } } },
  server: {
    port: 5173,
    host: true,
    proxy: { '/api': 'http://127.0.0.1:8787' },  // assistant backend (make assistant)
  },
})
