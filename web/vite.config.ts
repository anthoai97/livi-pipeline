import { createHash, timingSafeEqual } from "node:crypto";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { type Connect, defineConfig, loadEnv, type Plugin } from "vite";

const COOKIE = "livi_auth";

const loginPage = (failed: boolean) => `<!doctype html><meta name="viewport" content="width=device-width">
<title>Sign in</title><body style="font-family:sans-serif;display:grid;place-items:center;height:90vh">
<form method="post" action="/__login" style="display:grid;gap:8px;width:240px">
<input name="username" placeholder="Username" autocomplete="username" required>
<input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
<button>Sign in</button>${failed ? "<p style=\"color:#b00\">Wrong username or password.</p>" : ""}</form></body>`;

// Password login on every request the dev and preview servers answer, the /api and /s3 proxies included.
// A correct login sets a cookie holding a hash of the credentials, kept for 30 days.
function passwordAuth(username: string, password: string): Plugin {
  const token = Buffer.from(createHash("sha256").update(`${username}:${password}`).digest("hex"));
  const check: Connect.NextHandleFunction = (req, res, next) => {
    if (req.method === "POST" && req.url === "/__login") {
      let body = "";
      req.on("data", (chunk) => (body += chunk));
      req.on("end", () => {
        const form = new URLSearchParams(body);
        if (form.get("username") === username && form.get("password") === password) {
          res.statusCode = 303;
          res.setHeader("Set-Cookie", `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000`);
          res.setHeader("Location", "/");
          res.end();
        } else {
          res.statusCode = 401;
          res.setHeader("Content-Type", "text/html");
          res.end(loginPage(true));
        }
      });
      return;
    }
    const given = Buffer.from(req.headers.cookie?.match(new RegExp(`(?:^|; )${COOKIE}=([^;]*)`))?.[1] ?? "");
    if (given.length === token.length && timingSafeEqual(given, token)) return next();
    res.statusCode = 401;
    res.setHeader("Content-Type", "text/html");
    res.end(loginPage(false));
  };
  return {
    name: "password-auth",
    configureServer: (server) => void server.middlewares.use(check),
    configurePreviewServer: (server) => void server.middlewares.use(check),
  };
}

export default defineConfig(({ mode }) => {
  // The repo-root .env the pipeline reads; auth is on only when AUTH_PASSWORD is set.
  const env = loadEnv(mode, fileURLToPath(new URL("..", import.meta.url)), "AUTH_");
  return {
    plugins: [
      react(),
      tailwindcss(),
      ...(env.AUTH_PASSWORD ? [passwordAuth(env.AUTH_USERNAME || "livinit", env.AUTH_PASSWORD)] : []),
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
