"""aw-app-kb — Knowledge Base app, FastAPI entrypoint.

Serves three things from one container/process (see entrypoint.sh for how
Postgres+pgvector, bundled into the same image, boots alongside this):
  * ``/api/kb/*``  — file browser/editor + build/map job control (routes.py)
  * ``/mcp``       — Streamable HTTP MCP endpoint (mcp_http.py), auto-
                      discovered by aw-mcp-gateway via self_register.py
  * ``/``          — the built React UI (ui/dist), static files

No aw-workspace ``ctx.routes``/IdentityGuard integration here — Tier-2
container apps are reverse-proxied 1:1 by aw-workspace onto their own
subdomain (see aw-app.json's ``app_iframe`` window), not mounted under
``/api/apps/<id>`` like Tier-1 apps are.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import kb_pg
from . import mcp_http
from . import self_register
from .routes import build_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aw-app-kb")

PORT = int(os.environ.get("PORT", "8000"))
APP_ROOT = Path(__file__).resolve().parent.parent
UI_DIST = APP_ROOT / "ui" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    kb_pg.ensure_kb_schema(retries=12, delay=1.0)
    self_register.register_self(PORT)
    yield


def build_app() -> FastAPI:
    app = FastAPI(title="aw-app-kb", lifespan=lifespan)
    app.include_router(build_routes())

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "docs": kb_pg.count()}

    @app.post("/mcp")
    async def mcp_post(request: Request):
        body = await request.json()
        messages = body if isinstance(body, list) else [body]
        responses = [r for r in (mcp_http.handle_request(m) for m in messages) if r is not None]
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses if isinstance(body, list) else responses[0])

    @app.get("/mcp")
    async def mcp_get():
        return Response(status_code=405)

    if UI_DIST.is_dir():
        # html=True: unknown paths fall back to index.html (client-side
        # router-friendly), same pattern as aw-app-template's __main__.py.
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
    else:
        log.warning("%s not built yet — run `npm run build` in ui/ (API routes still work)", UI_DIST)

    return app


app = build_app()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
