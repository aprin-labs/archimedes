import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

// Deliberately separate from production routes, styles, environment and API proxy.
export default defineConfig({
	root: fileURLToPath(new URL(".", import.meta.url)),
	plugins: [react(), tailwindcss()],
	resolve: { alias: { "@": fileURLToPath(new URL(".", import.meta.url)) } },
	build: { outDir: "../dist/concept", emptyOutDir: false },
	server: { host: "127.0.0.1", port: 5180, strictPort: true },
	preview: { host: "127.0.0.1", port: 5181, strictPort: true },
});
