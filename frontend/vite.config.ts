/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api/v1 (and the legacy HTML routes, for click-through
// fallback) to the FastAPI backend, so the SPA can use relative URLs and no
// CORS is needed in dev. The backend's CORS config still lists the Vite origin
// as a belt-and-suspenders convenience; see frontend/README.md for the
// production caveat.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    css: false,
  },
});
