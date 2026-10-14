FROM node:24-slim
RUN corepack enable && corepack prepare pnpm@10.17.0 --activate
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build
CMD ["pnpm", "preview", "--host", "--port", "4173"]
