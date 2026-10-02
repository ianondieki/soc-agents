import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

/** Path -> page module, as App.tsx routes them (a key ending in "/" matches by prefix). Only used
 *  to fetch the opened page's chunk early; a route missing here still works, it just fetches its
 *  chunk after the shell has run instead of beside it. */
const ROUTE_PAGES: Record<string, string> = {
  "/": "MissionControl",
  "/showcase": "Showcase",
  "/incidents": "IncidentBoard",
  "/incidents/": "IncidentWorkspace",
  "/hitl": "HitlInbox",
  "/shift": "ShiftDesk",
  "/wallboard": "Wallboard",
  "/agents": "Agents",
  "/workflow": "WorkflowMap",
  "/problems": "Problems",
  "/regions": "Regions",
  "/maintenance": "Maintenance",
  "/audit": "Audit",
  "/contracts": "Contracts",
  "/pirs": "Pirs",
  "/scorecards": "Scorecards",
  "/settings": "Settings",
};

/**
 * Every page is a lazy chunk, so on a cold load the opened page's code would only be requested
 * once the shell (index + vendor) had downloaded and run: one more round trip before the first
 * real paint. This writes a few lines into index.html that, while the HTML is still parsing,
 * modulepreload the chunks (and stylesheet) of the page the URL names, so they download beside
 * the shell. Vite's own loader finds the links already there and does not fetch twice.
 */
function preloadRouteChunk(): Plugin {
  return {
    name: "noc-preload-route-chunk",
    apply: "build",
    transformIndexHtml: {
      order: "post",
      handler(html, ctx) {
        const bundle = ctx.bundle;
        if (!bundle) return html;
        const chunks = Object.values(bundle).filter((c) => c.type === "chunk");
        const entry = chunks.find((c) => c.type === "chunk" && c.isEntry);
        const shell = new Set<string>();
        const walk = (file: string, into: Set<string>) => {
          const c = bundle[file];
          if (!c || c.type !== "chunk" || into.has(file)) return;
          into.add(file);
          c.imports.forEach((i) => walk(i, into));
        };
        if (entry) walk(entry.fileName, shell);
        const map: Record<string, string[]> = {};
        for (const [path, page] of Object.entries(ROUTE_PAGES)) {
          const chunk = chunks.find(
            (c) => c.type === "chunk" && c.isDynamicEntry && /[\\/]src[\\/]pages[\\/]([^\\/]+)\.tsx$/.exec(c.facadeModuleId ?? "")?.[1] === page
          );
          if (!chunk || chunk.type !== "chunk") continue;
          const files = new Set<string>();
          walk(chunk.fileName, files);
          const own = [...files].filter((f) => !shell.has(f));
          const css = new Set<string>();
          for (const f of own) {
            const c = bundle[f];
            if (c?.type === "chunk") c.viteMetadata?.importedCss.forEach((x) => css.add(x));
          }
          map[path] = [...css, ...own].map((f) => f.replace(/^assets\//, ""));
        }
        const script =
          `<script>(function(){var m=${JSON.stringify(map)},p=location.pathname.replace(/\\/+$/,"")||"/",f=m[p];` +
          `if(!f)for(var k in m)if(k.length>1&&k.slice(-1)==="/"&&p.indexOf(k)===0)f=m[k];` +
          `(f||[]).forEach(function(h){var l=document.createElement("link"),css=/\\.css$/.test(h);` +
          `l.rel=css?"preload":"modulepreload";if(css)l.as="style";l.crossOrigin="";l.href="/assets/"+h;` +
          `document.head.appendChild(l)})})();</script>`;
        // Before the stylesheet link: an inline script after it would wait for the stylesheet.
        const marker = "<!-- route-chunk-preload -->";
        return html.includes(marker) ? html.replace(marker, script) : html.replace(/<script type="module"/, script + "\n    $&");
      },
    },
  };
}

export default defineConfig({
  plugins: [react(), preloadRouteChunk()],
  build: {
    // Every browser the floor runs (Chromium, Firefox 115+, Safari 17+) supports modulepreload
    // natively; an older one only loses the early fetch, so the polyfill is not shipped.
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        // React, ReactDOM and the router change far less often than the app: one long-cached
        // vendor chunk, so a new build re-downloads only the app's own chunks.
        manualChunks(id) {
          if (/[\\/]node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler|@remix-run[\\/]router)[\\/]/.test(id)) {
            return "vendor";
          }
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/health": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
