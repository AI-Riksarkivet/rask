import tailwindcss from "@tailwindcss/vite";
import { sveltekit } from "@sveltejs/kit/vite";
import { defineConfig } from "vite";
import { readFileSync } from "node:fs";

const pkg = JSON.parse(readFileSync("package.json", "utf-8"));

export default defineConfig({
  envDir: "..",
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  plugins: [tailwindcss(), sveltekit()],
  ssr: {
    noExternal: [
      "svelte-sonner",
      "mode-watcher",
      "svelte-toolbelt",
      "layerchart",
    ],
  },
  server: {
    host: "0.0.0.0",
    port: 5174,
    strictPort: true,
    allowedHosts: true,
    hmr: {
      // code-server proxy uses port 443 over wss
      clientPort: 443,
      protocol: "wss",
    },
  },
});
