import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  base: './',  // works from any folder or static host
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
  },
})
