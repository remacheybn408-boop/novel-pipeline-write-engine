import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        ws: false,
        // The backend's require_same_origin check compares the Origin header
        // against settings.public_url, so spoof it to the API origin while proxying.
        headers: { origin: "http://localhost:8000" },
      },
    },
  },
});
