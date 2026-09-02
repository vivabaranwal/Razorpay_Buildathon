import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Bind explicitly to IPv4. Vite's default resolves to IPv6-only on
    // Windows, which leaves http://127.0.0.1:5173 refusing connections even
    // though localhost works - a confusing failure for anyone following the
    // README, and for any tooling that resolves to IPv4.
    host: "127.0.0.1",
  },
});
