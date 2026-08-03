# aw-app-kb

Knowledge Base app for [Agentic Workspace](https://github.com/tekflox/aw-marketplace) — semantic
search over project docs and skills, backed by Postgres/pgvector.

Ported from the `agentic-workspace` monolith's Knowledge Base (`src/api/routes/knowledge_base.py`,
`src/libs/kb_pg.py`, `src/mcp/knowledge_base.py`, `src/commands/knowledge_base.py`) into a
standalone, self-contained aw-workspace app (Tier-2 / container), per the
[`aw-create-app`](https://github.com/tekflox/aw-app-template/blob/master/skills/aw-create-app/SKILL.md)
skill.

## What you get

- **File browser + editor** — browse, search (grep-style and semantic), edit, and delete
  Markdown documents under the knowledge base.
- **Semantic search** — Postgres + [pgvector](https://github.com/pgvector/pgvector), embeddings via
  [`nomic-embed-text-v1.5`](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) (`fastembed`,
  ONNX runtime — no PyTorch).
- **Code-map build jobs** — extract classes/functions/docstrings from a source tree into indexed
  Markdown (`--map-path`), plus HTML→Markdown conversion.
- **MCP tools**, auto-discovered by `aw-mcp-gateway` (no manual wiring — see below):
  `search_knowledge_base`, `update_knowledge_base`, `delete_knowledge_base`, `search_skills`,
  `load_skill`.
- **`aw-kb-curator` skill**, bundled and registered via `contributes.skills` — periodic KB/memory/
  skill-health audit.

## Architecture

One container, two processes, started together by `entrypoint.sh`:

```
┌─────────────────────────────────────────┐
│  aw-app-kb container                     │
│                                           │
│  Postgres 17 + pgvector  (127.0.0.1:5432)│
│              ▲                           │
│              │ kb_app/kb_pg.py           │
│              │                           │
│  FastAPI (kb_app/main.py)  :8000         │
│    /              → built React UI       │
│    /api/kb/*       → routes.py           │
│    /mcp            → mcp_http.py         │
└─────────────────────────────────────────┘
```

aw-workspace's Tier-2 (container) model is **one container per marketplace app** — there is no
sidecar/companion-container mechanism, so Postgres+pgvector is bundled into this app's own image
rather than run as a separate container (the monolith's `aw-pgvector` was a sibling container;
here it's `pgvector/pgvector:pg17` as the base image, with Python added on top).

MCP is served over **Streamable HTTP** (`/mcp`), not stdio — `aw-mcp-gateway` runs in a sibling
container and can't spawn a process inside this one. On boot, `kb_app/self_register.py` writes this
app's own `mcp.json` (`contributes.mcp.reload_on_save`) with an `http` entry pointing at
`http://$AW_APP_SELF_HOST:8000/mcp` — `AW_APP_SELF_HOST` is aw-workspace's own env (the
`aw-app-kb` container name, resolvable via aardvark-dns on the shared podman network), never
`127.0.0.1` (that only resolves inside this container's own netns).

The UI window (`windows/main.json`) is a `declarative` iframe pointing at `/api/apps/kb/` —
aw-workspace reverse-proxies that straight into this container (stripping the `/api/apps/kb`
prefix first), so `kb_app`'s own routes never need to know about it. `ui/src/client.js` uses
relative fetch paths for the same reason — they resolve correctly whether the page is loaded at
`/` (standalone) or `/api/apps/kb/` (proxied).

### Data persistence

The indexed documents and Postgres data directory are mounted via `$AW_APP_DATA`
(`aw-app.json`'s `runtime.volumes`, gated by the `fs:workspace-data` permission) at
`/app/persist` — resolved by aw-workspace to `~/.aw-workspace/data/kb/` on the host, **outside**
this app's installed package directory. That distinction matters: `aw-workspace` deletes the
entire package directory on uninstall (`fetch.remove_app_repo`), so a package-relative volume
(e.g. `{"source": "data", ...}`) looks persistent but is wiped on every uninstall/reinstall —
found live shipping this app's first version. `$AW_APP_DATA` survives that; `PGDATA` and
`KB_DATA_DIR` both point inside it (see `entrypoint.sh` / `aw-app.json`'s `runtime.env`).

The one exception is the `.` (package-root) volume mounted at `/app/pkg`, used only so
`self_register.py` can write this app's own `mcp.json` — that one is *supposed* to reset on
every install, since it's regenerated fresh on every boot anyway.

## Local development

```sh
# Backend (needs a local Postgres+pgvector — or just run the full image, see below)
cd kb_app && pip install -r requirements-dev.txt
KB_DATA_DIR=/tmp/kb-data python3 -m kb_app.main   # http://127.0.0.1:8000

# Frontend (proxies /api/kb + /mcp to 127.0.0.1:8000, see ui/vite.config.js)
cd ui && npm install && npm run dev
```

Or build and run the whole thing as one container:

```sh
docker build -t aw-app-kb:dev .
docker run -p 8000:8000 aw-app-kb:dev
```

## Testing

```sh
python3 tests/validate_manifest.py       # aw-app.json against schemas/aw-app.schema.json
PYTHONPATH=. pytest tests/ -q            # routes.py + mcp_http.py, kb_pg mocked out
```

## CI

- `.github/workflows/build.yml` — manual (`workflow_dispatch`): backend tests, frontend build,
  Docker image build.
- `.github/workflows/release.yml` — auto on push to `main`: bumps `aw-app.json`'s version via the
  shared `tekflox/aw-marketplace` release workflow.
- `.github/workflows/publish.yml` — manual: builds and pushes `ghcr.io/tekflox/aw-app-kb`.

## License

MIT — see [LICENSE](LICENSE).
