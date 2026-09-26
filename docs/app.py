#!/usr/bin/env python
"""Plexar-Framework local server: the /app page, the /api, and its docs.

    /app        the page: queue, approve, watch runs, give verdicts (docs/client.html)
    /api/...    everything the page does, one route per action (plexar_agents/api.py)
    /api-docs   every route, interactive (OpenAPI; /openapi.json is the machine form)
    /healthz    liveness

    python docs/app.py                       # http://127.0.0.1:8430
    uvicorn docs.app:app --reload             # dev, from the repo root

Localhost only by default. The API has no auth, and a submitted prompt causes code to be
written, so binding wider (PLEXAR_FRAMEWORK_HOST) is a deliberate act, never a default.
"""
from __future__ import annotations

import os
import pathlib
import sys

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plexar_agents import api as api_mod  # noqa: E402

app = FastAPI(title="Plexar-Framework", docs_url="/api-docs", redoc_url=None,
              description="The local control plane: queue prompts, select and approve a batch, "
                          "and watch the daemon run each task on its own branch. Every action in "
                          "/app is one of these routes.")
app.include_router(api_mod.router)
# Plexar's shared look: plexar-tokens.css is a byte-for-byte copy of the brand source, never edited here.
app.mount("/brand", StaticFiles(directory=ROOT / "docs" / "brand"), name="brand")


@app.middleware("http")
async def same_origin_writes(request, call_next):
    """A page on any other site can POST to localhost. Refuse cross-origin writes.

    Browsers send Origin on cross-site requests; curl and scripts send none and are
    allowed, since they are already on this machine. This is not auth; it only stops a web
    page from queueing prompts that write code.
    """
    from fastapi.responses import JSONResponse
    from urllib.parse import urlparse
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        host = urlparse(origin).hostname
        if host not in ("127.0.0.1", "localhost", request.url.hostname):
            return JSONResponse({"detail": "cross-origin write refused"}, status_code=403)
    return await call_next(request)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/app")


@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
def client_app() -> str:
    """The reference client. Everything it does is a call to /api."""
    return (ROOT / "docs" / "client.html").read_text(encoding="utf-8")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("PLEXAR_FRAMEWORK_HOST", "127.0.0.1"),
                port=int(os.environ.get("PLEXAR_FRAMEWORK_PORT", "8430")))
