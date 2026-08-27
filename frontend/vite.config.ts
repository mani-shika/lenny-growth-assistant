import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the SPA runs on :5173 and proxies /api to the FastAPI server
// on :8000. In the container there is no proxy: FastAPI serves the built
// bundle itself, so the app is same-origin and this config is irrelevant.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
