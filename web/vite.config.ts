import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Avoid common busy ports (5173/8000/4000/…). Defaults:
//   UI  http://127.0.0.1:18766
//   API http://127.0.0.1:18765
const API_PORT = Number(process.env.VDL_API_PORT || 18765)
const WEB_PORT = Number(process.env.VDL_WEB_PORT || 18766)

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // allow .env in web/ to override without shell exports
  const env = loadEnv(mode, process.cwd(), '')
  const apiPort = Number(env.VDL_API_PORT || API_PORT)
  const webPort = Number(env.VDL_WEB_PORT || WEB_PORT)

  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: webPort,
      strictPort: true,
      proxy: {
        '/api': {
          target: `http://127.0.0.1:${apiPort}`,
          changeOrigin: true,
        },
      },
    },
    preview: {
      host: '127.0.0.1',
      port: webPort + 1,
      strictPort: true,
    },
  }
})
