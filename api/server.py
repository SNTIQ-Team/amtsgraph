"""SNTIQ Platform API — the api.sntiq.com entry point.

Composition root for every backend service the platform ships:

    /                   service index (what lives here, where the docs are)
    /health             liveness + database check (used by uptime monitors)
    /v1/...             Amtsgraph resolver (mounted sub-application)
    /v1/pdf/letter      RESERVED — PDF letter rendering (501 until live)

Adding a future service
-----------------------
1. Build it as its own module with an APIRouter (or FastAPI sub-app).
2. Register it below — parent routes are matched in registration order,
   so explicit routes (e.g. /v1/pdf/*) MUST be added before the
   catch-all mount of the Amtsgraph app on /v1.
3. List it in SERVICE_INDEX so GET / stays an honest map of the API.

Run (systemd unit sntiq-api.service):
    AMTSGRAPH_DB=/srv/sntiq-api/data/atlas.db \
    uvicorn api.server:server --host 127.0.0.1 --port 8001
"""
from __future__ import annotations

import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from api.main import DB_PATH, app as amtsgraph
from api.pdf import router as pdf_router

SERVICE_INDEX = {
    "service": "sntiq-api",
    "operator": "SNTIQ n.e.V. — https://sntiq.com",
    "versions": {
        "v1": {
            "amtsgraph": {
                "status": "live",
                "docs": "/v1/docs",
                "endpoints": [
                    "/v1/health", "/v1/version", "/v1/stats", "/v1/sources",
                    "/v1/places", "/v1/matters",
                    "/v1/resolve/court", "/v1/resolve/authority",
                    "/v1/authorities/{id}",
                ],
            },
            "pdf": {
                "status": "live",
                "endpoints": ["/v1/pdf/letter"],
                "note": "stateless DIN-5008 rendering; nothing stored",
            },
        },
    },
    "dataset": "https://github.com/SNTIQ-Team/amtsgraph",
}

server = FastAPI(
    title="SNTIQ Platform API",
    version="1.0",
    # platform-level docs describe only the composition root;
    # each mounted service keeps its own /v1/docs
    docs_url="/docs",
    redoc_url=None,
)

server.add_middleware(GZipMiddleware, minimum_size=2048)

# Browser clients: the platform web app and local development.
server.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sntiq.com",
        "https://www.sntiq.com",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    max_age=86400,
)


@server.get("/")
def index():
    return SERVICE_INDEX


@server.get("/health")
def health():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1 FROM build_info LIMIT 1")
        conn.close()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"database unavailable: {exc}")
    return {"status": "ok", "services": {"amtsgraph": "ok", "pdf": "ok"}}


# ---- service routers (explicit routes BEFORE the /v1 mount) -------------

server.include_router(pdf_router)


# ---- mounted services ----------------------------------------------------

server.mount("/v1", amtsgraph)
