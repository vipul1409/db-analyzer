import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The UI calls the API at /api on its own origin; in development Vite proxies it to `dbx serve`
// (DBX_API_URL to point elsewhere). No CORS: serving the built UI from the API is release work.
export default defineConfig({
  plugins: [react()],
  // One local bundle, loaded once from the engineer's own machine: no need to split it.
  build: { chunkSizeWarningLimit: 800 },
  server: {
    proxy: {
      "/api": { target: process.env.DBX_API_URL ?? "http://127.0.0.1:8765" },
    },
  },
});
