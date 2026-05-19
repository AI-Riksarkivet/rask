# syntax=docker/dockerfile:1
FROM denoland/deno:2.7.1@sha256:ee49ef20aec2e0c2967e5563161d32f8a54a282aa08112c8e6e719e02d216abc AS builder

WORKDIR /app

# Install dependencies first (cache-friendly)
COPY frontend/deno.json frontend/deno.lock frontend/package.json ./
RUN deno install

# Copy source and build
COPY frontend/ .
RUN deno task build && \
    chmod -R a+rX node_modules

# ── Production stage ─────────────────────────────────────────────────
FROM denoland/deno:2.7.1@sha256:ee49ef20aec2e0c2967e5563161d32f8a54a282aa08112c8e6e719e02d216abc

RUN groupadd -g 1000 app 2>/dev/null || true && \
    useradd -u 1000 -g 1000 -s /bin/sh app 2>/dev/null || true

WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/.deno-deploy /app/.deno-deploy
COPY --from=builder --chown=1000:1000 /app/node_modules /app/node_modules
COPY --from=builder --chown=1000:1000 /app/deno.json /app/deno.lock /app/package.json /app/
COPY --from=builder --chown=1000:1000 /app/server.ts /app/server.ts

ENV DENO_DIR=/deno-dir

USER 1000

EXPOSE 8000

CMD ["deno", "run", "-A", "server.ts"]
