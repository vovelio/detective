import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const target = process.env.DETECTIVE_API_TARGET ?? "http://127.0.0.1:8000";
const proxy = {
  "/api": { target, changeOrigin: false },
  "/ws": { target, ws: true, changeOrigin: false, rewriteWsOrigin: false },
};
export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
  preview: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
});
