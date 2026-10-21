// DEV ONLY: bundle the real DemoBank frontend (unmodified source) with esbuild when Vite can't be installed.
// Usage: node devtools/build_frontend.mjs <frontend-dir> <out-dir> [api-base]
import { build } from "/opt/npm-tools/node_modules/esbuild/lib/main.js";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const [frontend, out, api = "http://localhost:8000"] = process.argv.slice(2);
mkdirSync(out, { recursive: true });
await build({
  entryPoints: [join(frontend, "src/main.tsx")],
  bundle: true, outfile: join(out, "assets/app.js"), format: "esm", jsx: "automatic",
  loader: { ".css": "css", ".svg": "file" }, sourcemap: true,
  nodePaths: ["/opt/npm-tools/node_modules"],
  alias: { "react-router-dom": resolve(here, "router-shim/react-router-dom.tsx") },
  define: { "import.meta.env.VITE_API_BASE_URL": JSON.stringify(api), "process.env.NODE_ENV": '"development"' },
});
const html = readFileSync(join(frontend, "index.html"), "utf8")
  .replace('<script type="module" src="/src/main.tsx"></script>',
           '<link rel="stylesheet" href="/assets/app.css" /><script type="module" src="/assets/app.js"></script>');
writeFileSync(join(out, "index.html"), html);
copyFileSync(join(frontend, "public/favicon.svg"), join(out, "favicon.svg"));
console.log("built", out);
