/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Porta do backend para o proxy de desenvolvimento (start.ps1 -Port define MEDIA_TOOL_PORT).
function backendPort(): number {
  const raw = process.env.MEDIA_TOOL_PORT;
  const port = raw ? Number(raw) : 8765;
  return Number.isInteger(port) && port > 0 && port < 65536 ? port : 8765;
}

export default defineConfig({
  base: "/",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${backendPort()}`,
        // O backend valida o header Host (127.0.0.1:<porta> / localhost:<porta>).
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
