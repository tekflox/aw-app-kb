# syntax=docker/dockerfile:1.7
# aw-app-kb — Knowledge Base app. Single container: Postgres+pgvector (this
# image's base) + FastAPI backend + the built React UI, started together by
# entrypoint.sh. See kb_app/kb_pg.py and entrypoint.sh for why everything is
# bundled into one image rather than a separate sidecar container.

# ---- UI build stage ---------------------------------------------------
FROM node:20-slim AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# ---- final image --------------------------------------------------------
FROM pgvector/pgvector:pg17

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY kb_app/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Bake the embedding model into the image (520 MB, nomic-embed-text-v1.5)
# so the first boot doesn't need a network round-trip before search works.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed_cache
RUN python3 -c "from fastembed import TextEmbedding; TextEmbedding('nomic-ai/nomic-embed-text-v1.5')"

COPY kb_app /app/kb_app
COPY skills /app/skills
COPY --from=ui-build /ui/dist /app/ui/dist
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
