#!/usr/bin/env bash
# Single-container aw-workspace app: Postgres+pgvector (this image's base,
# pgvector/pgvector:pg17) and the FastAPI backend run as two processes in
# the same container — the aw-workspace Tier-2 container model is one
# container per app (no sidecar/companion mechanism for marketplace apps),
# so bundling is the pragmatic port of the monolith's separate aw-pgvector
# container (see kb_app/kb_pg.py's module docstring).
set -euo pipefail

export POSTGRES_DB="${POSTGRES_DB:-knowledge_base}"
export POSTGRES_USER="${POSTGRES_USER:-postgres}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
export KB_PG_URL="${KB_PG_URL:-postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}}"
# aw-app.json sets PGDATA under the $AW_APP_DATA mount (/app/persist) so the
# database survives an uninstall/reinstall — see runtime.py's $AW_APP_DATA
# docstring. Falls back to the base image's own default for a bare
# `docker run` with no aw-workspace volume wired up.
export PGDATA="${PGDATA:-/var/lib/postgresql/data}"
export KB_DATA_DIR="${KB_DATA_DIR:-/app/data}"
mkdir -p "$PGDATA" "$KB_DATA_DIR"

# Start Postgres via the base image's own entrypoint — handles first-run
# initdb + creates $POSTGRES_DB from env, identical to a standalone
# postgres container. Backgrounded so this script can also start the app.
docker-entrypoint.sh postgres &
PG_PID=$!

until pg_isready -h 127.0.0.1 -p 5432 -U "$POSTGRES_USER" >/dev/null 2>&1; do
  sleep 1
done

python3 -m kb_app.main &
APP_PID=$!

# Either process dying takes the whole container down (podman/docker
# restarts it) rather than limping along with only half the stack up.
wait -n "$PG_PID" "$APP_PID"
exit $?
