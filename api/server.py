"""SNTIQ Platform API — the api.sntiq.com entry point.

Composition root for every backend service the platform ships:

    /                   service index (JSON; HTML landing page for browsers)
    /health             liveness + database check (used by uptime monitors)
    /v1/...             Amtsgraph resolver (mounted sub-application)
    /v1/pdf/letter      Briefcraft — stateless DIN-5008 letter rendering

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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pathlib import Path

from api.main import DB_PATH, app as amtsgraph
from api.pdf import pdf_letter, router as pdf_router

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
                    "/v1/places", "/v1/graph", "/v1/matters",
                    "/v1/resolve/court", "/v1/resolve/authority",
                    "/v1/authorities/{id}",
                ],
                "dataset": "https://github.com/SNTIQ-Team/amtsgraph",
            },
            "pdf": {
                "status": "live",
                "endpoints": ["/v1/pdf/letter"],
                "note": "stateless DIN-5008 rendering; nothing stored",
            },
        },
    },
    # Canonical, service-scoped paths. The /v1/* block above stays as a
    # permanent backward-compatibility alias — both prefixes serve the same
    # app instances.
    "services": {
        "amtsgraph": {
            "status": "live",
            "docs": "/amtsgraph/docs",
            "endpoints": [
                "/amtsgraph/health", "/amtsgraph/version", "/amtsgraph/stats",
                "/amtsgraph/sources", "/amtsgraph/places", "/amtsgraph/graph",
                "/amtsgraph/matters", "/amtsgraph/resolve/court",
                "/amtsgraph/resolve/authority", "/amtsgraph/authorities/{id}",
            ],
            "legacy_prefix": "/v1",
            "dataset": "https://github.com/SNTIQ-Team/amtsgraph",
        },
        "lexgraph": {
            "status": "live",
            "docs": "/lex/docs",
            "endpoints": [
                "/lex/acts", "/lex/acts/{id}", "/lex/git", "/lex/feed",
                "/lex/hierarchy", "/lex/graph", "/lex/search",
                "/lex/decisions", "/lex/decisions/{id}", "/lex/digest",
                "/lex/stats", "/lex/health",
            ],
            "dataset": "https://github.com/SNTIQ-Team/lexgraph",
        },
        "pdf": {
            "status": "live",
            "endpoints": ["/pdf/letter"],
            "legacy_prefix": "/v1/pdf",
            "note": "stateless DIN-5008 rendering; nothing stored",
        },
    },
    "dataset": "https://github.com/SNTIQ-Team/amtsgraph",
}


# ---- HTML landing page (browsers only; scripts keep getting JSON) --------
# Single self-contained document: inline CSS + inline SVG, no JS, no
# external requests. Keep the numbers in sync with the dataset READMEs.

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SNTIQ API — api.sntiq.com</title>
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #030304;
    color: #FFFFFF;
    font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }
  .accent-bar {
    height: 3px;
    background: linear-gradient(90deg, #0033A0, #0052FF, #0072CE);
  }
  main { max-width: 960px; margin: 0 auto; padding: 56px 24px 40px; }
  a { color: #7FA6FF; text-decoration: none; transition: color .15s ease; }
  a:hover { color: #FFFFFF; }
  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
                 "Liberation Mono", monospace;
  }
  header { display: flex; align-items: center; gap: 16px; margin-bottom: 28px; }
  header h1 { font-size: 1.7rem; font-weight: 700; letter-spacing: -0.02em; }
  header .host { color: #94A3B8; font-size: 0.9rem; }
  .intro { color: #94A3B8; max-width: 640px; margin-bottom: 40px; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(270px, 100%), 1fr));
    gap: 20px;
  }
  .card {
    background: rgba(20, 21, 26, 0.6);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 14px;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    transition: border-color .15s ease;
  }
  .card:hover { border-color: rgba(255, 255, 255, 0.18); }
  .card h2 { font-size: 1.15rem; font-weight: 600; }
  .status {
    display: flex; align-items: center; gap: 7px;
    font-size: 0.72rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.09em;
    color: #4ADE80;
  }
  .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: #4ADE80;
    box-shadow: 0 0 6px rgba(74, 222, 128, 0.55);
  }
  .tagline { color: #94A3B8; font-size: 0.9rem; margin-top: -6px; }
  .desc { color: #94A3B8; font-size: 0.85rem; }
  .endpoints {
    list-style: none;
    font-size: 0.78rem;
    color: #CBD5E1;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    padding: 10px 14px;
    overflow-x: auto;
  }
  .endpoints li { white-space: nowrap; }
  .links {
    display: flex; flex-wrap: wrap; gap: 14px;
    margin-top: auto; font-size: 0.85rem;
  }
  footer {
    margin-top: 48px;
    padding-top: 22px;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    color: #94A3B8;
    font-size: 0.85rem;
    display: flex;
    flex-wrap: wrap;
    gap: 6px 18px;
  }
  @media (max-width: 480px) {
    main { padding: 40px 18px 32px; }
    header h1 { font-size: 1.45rem; }
  }
</style>
</head>
<body>
<div class="accent-bar"></div>
<main>
  <header>
    <img src="favicon.svg" width="44" height="44" alt="SNTIQ mark">
    <div>
      <h1>SNTIQ API</h1>
      <p class="host mono">api.sntiq.com</p>
    </div>
  </header>

  <p class="intro">Public, read-only APIs behind
    <a href="https://sntiq.com">sntiq.com</a> — infrastructure for navigating
    large bureaucratic systems.</p>

  <section class="grid">
    <article class="card">
      <div class="status"><span class="dot"></span>live</div>
      <h2>Amtsgraph</h2>
      <p class="tagline">German public authorities &amp; court competences</p>
      <p class="desc">19,227 authorities, 338,873 court-chain links,
        matter-aware competence resolution.</p>
      <ul class="endpoints mono">
        <li>/amtsgraph/resolve/court</li>
        <li>/amtsgraph/resolve/authority</li>
        <li>/amtsgraph/places</li>
        <li>/amtsgraph/stats</li>
      </ul>
      <div class="links">
        <a href="/amtsgraph/docs">Interactive docs</a>
        <a href="https://github.com/SNTIQ-Team/amtsgraph">Dataset</a>
      </div>
    </article>

    <article class="card">
      <div class="status"><span class="dot"></span>live</div>
      <h2>Lexgraph</h2>
      <p class="tagline">German &amp; EU legislation as event-sourced git</p>
      <p class="desc">Laws as repositories, amendments as commits, court
        decisions linked to the norms they affect. WP21: 358 procedures,
        1,467 patch instructions.</p>
      <ul class="endpoints mono">
        <li>/lex/acts</li>
        <li>/lex/git</li>
        <li>/lex/feed</li>
        <li>/lex/decisions</li>
        <li>/lex/search</li>
      </ul>
      <div class="links">
        <a href="/lex/docs">Interactive docs</a>
        <a href="https://github.com/SNTIQ-Team/lexgraph">Dataset</a>
      </div>
    </article>

    <article class="card">
      <div class="status"><span class="dot"></span>live</div>
      <h2>Briefcraft PDF</h2>
      <p class="tagline">Stateless DIN-5008 letter rendering</p>
      <p class="desc">Render a formal German letter from JSON — nothing is
        stored.</p>
      <ul class="endpoints mono">
        <li>POST /pdf/letter</li>
      </ul>
      <div class="links">
        <a href="/docs">Interactive docs</a>
      </div>
    </article>
  </section>

  <footer>
    <span><a href="https://sntiq.com">SNTIQ n.e.V.</a></span>
    <span>Free for non-commercial use · rate-limited</span>
    <span>Legacy <span class="mono">/v1/*</span> paths remain supported</span>
    <span><a href="mailto:contact@sntiq.com">contact@sntiq.com</a></span>
  </footer>
</main>
</body>
</html>
"""

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
def index(request: Request):
    # Content negotiation: browsers get the landing page, everything else
    # (curl, scripts, monitors) keeps getting the JSON service index.
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(INDEX_HTML)
    return SERVICE_INDEX


# The real SNTIQ mark (same file the website uses), served for the landing
# page header and the browser tab icon.
_MARK = Path(__file__).resolve().parent / "sntiq-mark.svg"


@server.get("/favicon.svg", include_in_schema=False)
@server.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(_MARK, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=604800"})


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

# Canonical service-scoped alias for the PDF renderer: POST /pdf/letter.
# The /v1/pdf/letter route above stays for backward compatibility.
server.post("/pdf/letter", include_in_schema=False)(pdf_letter)


# ---- mounted services ----------------------------------------------------
# Canonical, service-scoped path first; the historical /v1 mount stays so
# that existing clients (and published docs) never break. Both serve the
# same app instance — /amtsgraph/docs and /v1/docs are equally live.

server.mount("/amtsgraph", amtsgraph)
server.mount("/v1", amtsgraph)
