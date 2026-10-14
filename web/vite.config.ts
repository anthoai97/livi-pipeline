import { timingSafeEqual } from "node:crypto";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { type Connect, defineConfig, loadEnv, type Plugin } from "vite";

// Basic auth on every request the dev and preview servers answer, the /api and /s3 proxies included.
function basicAuth(username: string, password: string): Plugin {
  const expected = Buffer.from(`Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`);
  const check: Connect.NextHandleFunction = (req, res, next) => {
    const given = Buffer.from(req.headers.authorization ?? "");
    if (given.length === expected.length && timingSafeEqual(given, expected)) return next();
    res.statusCode = 401;
    res.setHeader("WWW-Authenticate", 'Basic realm="Livinit", charset="UTF-8"');
    res.end("Authentication required");
  };
  return {
    name: "basic-auth",
    configureServer: (server) => void server.middlewares.use(check),
    configurePreviewServer: (server) => void server.middlewares.use(check),
  };
}

export default defineConfig(({ mode }) => {
  // The repo-root .env the pipeline reads; auth is on only when BASIC_AUTH_PASSWORD is set.
  const env = loadEnv(mode, fileURLToPath(new URL("..", import.meta.url)), "BASIC_AUTH_");
  return {
    plugins: [
      react(),
      tailwindcss(),
      ...(env.BASIC_AUTH_PASSWORD ? [basicAuth(env.BASIC_AUTH_USERNAME || "livinit", env.BASIC_AUTH_PASSWORD)] : []),
    ],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: "http://localhost:8000",
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
        // The asset bucket sends no CORS headers, so the browser loads GLBs through this proxy.
        "/s3": {
          target: "https://livinit-storage-prod.s3.us-east-2.amazonaws.com",
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/s3/, ""),
        },
      },
    },
  };
});
