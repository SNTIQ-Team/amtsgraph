"""Amtsgraph — resolution API (matter-aware, evidence-carrying).

Identity and competence operations (see docs/API.md, "Evidence envelope"):

  GET /resolve/place?purpose=court&q=54636
      stable place ids (place:jz:<PLZ>:<ORTK> / place:ags:<AGS>) or explicit
      ambiguity — never the first database row
  GET /resolve/competence?place_id=place:jz:10115:BERLIN&matter=zivil
  GET /resolve/competence?place_id=place:ags:09162000&kind=jobcenter
      the competent court/office with relation-level evidence and every
      applicable caveat (place, municipality, authority, matter, assignment)
  GET /resolve/court?place_id=...&matter=sozial     (or plz[&ort][&ortk])
      the register's instance chain for place x matter
  GET /resolve/authority?place_id=place:ags:...&kind=...   (or ags)
  GET /authorities/{id}
      identity/contact card + graph edges (edges are not competence)
  GET /courts, /courts/{court_id}, /courts/lookup, /courts/mapping
      courts as first-class nodes: court id <-> name <-> XJustiz <-> ECLI

Every competence answer is ``current_only``. Unknown, repeated or temporal
query arguments are rejected (400) with their names, never ignored.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import unicodedata
from functools import wraps
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

from api import competence as domain
from api import courts as court_registry
from api.contract import (HTTP_STATUS, STATUSES, TEMPORAL_SCOPE,
                          check_snapshot, install, request_scope, respond,
                          snapshot_info, strict_query)

# Deployment override: AMTSGRAPH_DB=/path/to/atlas.db (default: repo layout)
DB_PATH = Path(os.environ.get(
    "AMTSGRAPH_DB",
    Path(__file__).resolve().parent.parent / "data" / "atlas.db"))
app = FastAPI(title="Amtsgraph", version="3.0",
              dependencies=[Depends(strict_query)])
install(app)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize(name: str) -> str:
    s = name.lower().strip()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.split(",")[0].split("(")[0].strip()



# Backwards-compatible alias: the card now lives with the domain operations.
authority_card = domain.authority_card
COURT_KINDS = domain.COURT_KINDS


@app.get("/capabilities")
def capabilities():
    return {
        "schema_version": 2, "service": "amtsgraph",
        "temporal": "current_only",
        "temporal_scope": TEMPORAL_SCOPE,
        "historical_arguments": "rejected with unsupported_temporal_request",
        "unknown_arguments": "rejected with invalid_argument (HTTP 400)",
        "snapshot_pinning": "snapshot_id argument; mismatch -> stale_snapshot",
        "statuses": sorted(STATUSES),
        "http_status": HTTP_STATUS,
        "place_identity": {
            "court_register_place": "place:jz:<PLZ>:<ORTK>",
            "municipality": "place:ags:<AGS>",
            "legacy_selectors": ["plz", "ort", "ortk", "ags"]},
        "court_identity": {"de": "court:de:<XJustiz-ID>",
                           "eu": "court:eu:<ECLI court code>"},
        "ambiguity": "explicit candidates; nothing is selected implicitly",
        "assignment_evidence": ["register_observation",
                                "curated_source_review",
                                "statutory_assignment",
                                "official_publication"],
        "caveat_scopes": ["place", "municipality", "district", "land",
                          "matter", "authority", "court", "assignment",
                          "global"],
        "operations": {
            "place_resolve": "/resolve/place",
            "competence_resolve": "/resolve/competence",
            "court_chain": "/resolve/court",
            "authority_resolution": "/resolve/authority",
            "authority_detail": "/authorities/{authority_id}",
            "court_detail": "/courts/{court_id}",
            "court_lookup": "/courts/lookup",
            "court_mapping": "/courts/mapping",
            "court_search": "/courts",
            "research": "/research"},
    }


# --------------------------------------------------------------- service

@app.get("/health")
def health():
    try:
        db().execute("SELECT 1 FROM build_info LIMIT 1")
        return {"status": "ok"}
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(503, f"database unavailable: {exc}")


@app.get("/version")
def version():
    info = snapshot_info(db())
    return {"dataset": "Amtsgraph",
            "built_at": info["built_at"],
            "snapshot_id": info["snapshot_id"],
            "schema_version": info["schema_version"],
            "source_snapshots": info["source_snapshots"]}


@app.get("/stats")
def stats():
    conn = db()
    one = lambda sql: conn.execute(sql).fetchone()[0]          # noqa: E731
    return {
        "authorities_active": one("SELECT COUNT(*) FROM authority "
                                  "WHERE valid_to IS NULL"),
        "court_chain_links": one("SELECT COUNT(*) FROM court_chain"),
        "places": one("SELECT COUNT(*) FROM jz_place"),
        "competences": one("SELECT COUNT(*) FROM competence"),
        "parent_edges": one("SELECT COUNT(*) FROM authority_edge "
                            "WHERE relation='parent'"),
        "eu_authorities": one("SELECT COUNT(*) FROM authority "
                              "WHERE valid_to IS NULL AND kind LIKE 'eu_%'"),
        "eu_edges": one("SELECT COUNT(*) FROM authority_edge "
                        "WHERE source='eu_curated'"),
        "caveats": one("SELECT COUNT(*) FROM caveat"),
        # courts as nodes (codelist entries, incl. dissolved courts and
        # departments) — distinct from the register cards counted above
        **({"courts": one("SELECT COUNT(*) FROM court"),
            "courts_active": one("SELECT COUNT(*) FROM court WHERE "
                                 "status='active' AND unit_type='court'")}
           if domain.has_table(conn, "court") else {}),
        "counting_note": ("authorities_active counts every active node: "
                          "offices, court register cards and organigram "
                          "units (departments, sections)"),
    }


@app.get("/sources")
def sources():
    """Provenance overview: which source contributed how many records."""
    conn = db()
    return [{"source": r["source"], "authorities": r["n"]}
            for r in conn.execute(
                """SELECT source, COUNT(*) AS n FROM authority
                   WHERE valid_to IS NULL GROUP BY source ORDER BY n DESC""")]


# ---------------------------------------------------------------- places

_PLACE_COLS = """g.ags, g.name_simple AS name, g.kind,
                  k.name AS kreis, l.name AS land,
                  (SELECT COUNT(*) FROM gemeinde_plz p WHERE p.ags=g.ags) AS plz_count,
                  (SELECT MIN(plz) FROM gemeinde_plz p WHERE p.ags=g.ags) AS plz"""
_PLACE_FROM = """FROM gemeinde g
           JOIN kreis k ON k.ags = g.kreis_ags
           JOIN land  l ON l.code = k.land_code"""


@app.get("/places")
def search_places(q: str, limit: int = 10):
    """Place lookup: city name (prefix → substring → fuzzy) or PLZ.

    A digit query searches gemeinde_plz directly and returns the PLZ on
    each match, so clients can skip the separate PLZ prompt. Fuzzy
    matching (difflib on normalized names) catches typos like
    'erdnig' → Erding; results are flagged with fuzzy=True.
    """
    conn = db()
    q = q.strip()

    # ---- postal-code search -------------------------------------
    if q.isdigit() and 3 <= len(q) <= 5:
        # the outer join alias must NOT be `p` — _PLACE_COLS' correlated
        # subqueries use `p` internally, and shadowing it silently
        # decorrelates them (MIN(plz) over ALL of Germany = 01067 Dresden).
        # Also: show the PLZ that actually matched the query, not MIN().
        cols = _PLACE_COLS.replace(
            "(SELECT MIN(plz) FROM gemeinde_plz p WHERE p.ags=g.ags) AS plz",
            "pq.plz AS plz")
        rows = conn.execute(
            f"""SELECT DISTINCT {cols}
                {_PLACE_FROM}
                JOIN gemeinde_plz pq ON pq.ags = g.ags
                WHERE pq.plz LIKE ? || '%'
                ORDER BY g.population IS NULL, g.population DESC, pq.plz
                LIMIT ?""",
            (q, limit)).fetchall()
        return {"query": q,
                "matches": [{**dict(r),
                             "place_id": domain.ags_place_id(r["ags"])}
                            for r in rows],
                "court_register_only": []}

    norm = normalize(q)
    rows = conn.execute(
        f"""SELECT {_PLACE_COLS} {_PLACE_FROM}
            WHERE g.name_norm LIKE ? || '%'
            ORDER BY g.population IS NULL, g.population DESC LIMIT ?""",
        (norm, limit)).fetchall()

    # ---- substring fallback (e.g. 'neustadt' inside 'bad neustadt')
    if not rows and len(norm) >= 3:
        rows = conn.execute(
            f"""SELECT {_PLACE_COLS} {_PLACE_FROM}
                WHERE g.name_norm LIKE '%' || ? || '%'
                ORDER BY g.population IS NULL, g.population DESC LIMIT ?""",
            (norm, limit)).fetchall()

    matches = [dict(r) for r in rows]

    # ---- fuzzy fallback for typos --------------------------------
    if not matches and len(norm) >= 4:
        import difflib
        pool = conn.execute(
            f"SELECT g.name_norm, {_PLACE_COLS} {_PLACE_FROM}").fetchall()
        by_norm = {}
        for r in pool:
            by_norm.setdefault(r["name_norm"], r)
        close = difflib.get_close_matches(
            norm, by_norm.keys(), n=limit, cutoff=0.78)
        matches = [{**dict(by_norm[n]), "fuzzy": True} for n in close]
        for m in matches:
            m.pop("name_norm", None)

    extra = conn.execute(
        """SELECT plz, ort, ortk FROM jz_place
           WHERE ort_norm LIKE ? || '%' AND gemeinde_ags IS NULL LIMIT ?""",
        (norm, limit)).fetchall()
    for m in matches:
        m["place_id"] = domain.ags_place_id(m["ags"])
    return {"query": q, "matches": matches,
            "court_register_only": [
                {**dict(r), "place_id": domain.jz_place_id(r["plz"], r["ortk"])}
                for r in extra]}


_GRAPH_CACHE: dict | None = None
_GRAPH_JSON: bytes | None = None
_GRAPH_LOCK = threading.Lock()


def _serialized(lock: threading.Lock):
    """Serialize an expensive sync endpoint, preserving FastAPI's signature.

    Uvicorn runs sync handlers in a thread pool.  Without this guard, a burst
    of cold-cache ``/graph`` requests can make every worker construct and JSON
    encode the multi-megabyte graph simultaneously — enough to exhaust the
    1 GB production host during boot.  Returning pre-encoded bytes also avoids
    a fresh large allocation for every cached response.
    """
    def decorate(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            with lock:
                return func(*args, **kwargs)
        return wrapped
    return decorate


@app.get("/graph")
@_serialized(_GRAPH_LOCK)
def graph(v: str | None = Query(
        None, max_length=16,
        description="client cache-busting token (the sntiq.com viewer "
                    "sends v=5); it selects nothing")):
    """Full authority graph for visual exploration: every ACTIVE
    authority plus every edge whose endpoints are active.
    Compact arrays (nodes: [id, kind, name], edges: [a, b, relation]);
    sparse EU relation notes/provenance live in ``edge_meta``.
    the dataset is static per deploy, so the response is cached
    in-process and marked cacheable for clients.
    """
    global _GRAPH_CACHE, _GRAPH_JSON
    if _GRAPH_CACHE is None:
        conn = db()
        edges = conn.execute(
            """SELECT e.from_authority, e.to_authority, e.relation,
                      e.matter, e.note, e.source, e.source_url
               FROM authority_edge e
               JOIN authority a ON a.id = e.from_authority AND a.valid_to IS NULL
               JOIN authority b ON b.id = e.to_authority AND b.valid_to IS NULL
               WHERE e.from_authority <> e.to_authority
               """).fetchall()
        # ALL active authorities, not only edge-connected ones —
        # otherwise every Land without a harvested department web
        # (i.e. everything outside Bavaria) loses its Kreise/cities
        ids = {r["id"] for r in conn.execute(
            "SELECT id FROM authority WHERE valid_to IS NULL")}
        # land per authority: from its competence areas, then propagated
        # across edges so departments inherit their parent's Land
        land: dict[int, str] = {}
        for r in conn.execute(
                """SELECT authority_id,
                          CASE level WHEN 'land' THEN area
                               ELSE substr(area, 1, 2) END AS lc
                   FROM competence WHERE level IN
                        ('land', 'kreis', 'gemeinde')"""):
            land.setdefault(r["authority_id"], r["lc"])
        # courts/prosecutors have no competence rows — derive their
        # Land from the postal code; justizadressen rows keep the PLZ
        # inside the street/postal string (plz column is NULL), so
        # extract it with a regex first
        import re as _re
        plz_of: dict[int, str] = {}
        for r in conn.execute(
                """SELECT id, plz, street, postal_address
                   FROM authority WHERE valid_to IS NULL"""):
            p = r["plz"]
            if not p:
                m = _re.search(r"\b(\d{5})\b",
                               (r["street"] or "") + " " +
                               (r["postal_address"] or ""))
                p = m.group(1) if m else None
            if p:
                plz_of[r["id"]] = p
        plz2land: dict[str, str] = {}
        plz2kreis: dict[str, str] = {}
        for r in conn.execute(
                """SELECT gp.plz, MIN(g.ags) AS ags FROM gemeinde_plz gp
                   JOIN gemeinde g ON g.ags = gp.ags GROUP BY gp.plz"""):
            plz2land[r["plz"]] = r["ags"][:2]
            plz2kreis[r["plz"]] = r["ags"][:5]
        for aid, p in plz_of.items():
            if p in plz2land:
                land.setdefault(aid, plz2land[p])
        for _ in range(6):  # propagate over the shallow trees
            changed = False
            for edge in edges:
                a, b = edge[0], edge[1]
                if a in land and b not in land:
                    land[b] = land[a]; changed = True
                elif b in land and a not in land:
                    land[a] = land[b]; changed = True
            if not changed:
                break
        # kreis per authority (5-digit ARS prefix) — same recipe
        kreis: dict[int, str] = {}
        for r in conn.execute(
                """SELECT authority_id,
                          CASE level WHEN 'kreis' THEN area
                               WHEN 'gemeinde' THEN substr(area, 1, 5)
                          END AS kc
                   FROM competence WHERE level IN ('kreis', 'gemeinde')"""):
            if r["kc"]:
                kreis.setdefault(r["authority_id"], r["kc"])
        for aid, p in plz_of.items():
            if p in plz2kreis:
                kreis.setdefault(aid, plz2kreis[p])
        for _ in range(6):
            changed = False
            for edge in edges:
                a, b = edge[0], edge[1]
                if a in kreis and b not in kreis:
                    kreis[b] = kreis[a]; changed = True
                elif b in kreis and a not in kreis:
                    kreis[a] = kreis[b]; changed = True
            if not changed:
                break
        nodes = []
        if ids:
            marks = ",".join("?" * len(ids))
            for r in conn.execute(
                    f"""SELECT id, kind, name FROM authority
                        WHERE id IN ({marks})""", list(ids)):
                nodes.append([r["id"], r["kind"], r["name"],
                              land.get(r["id"]), kreis.get(r["id"])])
        _GRAPH_CACHE = {
            "nodes": nodes,
            # Keep the original tuple contract uniform: older clients often
            # destructure every row as exactly ``for a, b, relation in``.
            "edges": [[r[0], r[1], r[2]] for r in edges],
            # Rich legal scope is sparse (the reviewed EU overlay only), so
            # publish it separately instead of changing the tuple shape or
            # repeating nulls across thousands of German edges.
            "edge_meta": [
                {"from": r[0], "to": r[1], "relation": r[2],
                 "matter": r[3], "note": r[4], "source": r[5],
                 "source_url": r[6]}
                for r in edges if r[5] == "eu_curated"
            ],
            "kreise": {r["ags"]: r["name"] for r in
                       conn.execute("SELECT ags, name FROM kreis")},
            # paper §8.4: presentation heuristics stay presentation metadata
            "node_fields": ["id", "kind", "name", "land", "kreis"],
            "visualization_metadata": {
                "fields": ["land", "kreis"],
                "basis": ("display grouping inferred from competence areas, "
                          "postal codes and propagation along graph edges"),
                "legal_effect": False,
                "note": ("not a statement of territorial competence; use "
                         "/resolve/competence for assignments")},
            "edge_semantics": {
                "appeal": ("next instance in an observed register chain; "
                           "not proof that an appeal is available"),
                "parent": "organisational hierarchy, not competence",
                "supervision": ("higher-level body inferred from unit "
                                "titles (heuristic)"),
                "legal_effect_verified": False},
        }
        _GRAPH_JSON = json.dumps(
            _GRAPH_CACHE, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")
    return Response(_GRAPH_JSON, media_type="application/json", headers={
        "Cache-Control": "public, max-age=3600"})


@app.get("/matters")
def matters():
    rows = db().execute(
        "SELECT code, label_de, grp, core FROM matter ORDER BY core DESC, grp")
    return [dict(r) for r in rows]


# ------------------------------------------------------------ identity

SnapshotArg = Query(None, max_length=120,
                    description="pin a snapshot_id; mismatch -> 409 "
                                "stale_snapshot")


@app.get("/resolve/place", name="place_resolve")
def place_resolve(request: Request,
                  purpose: str = Query(..., description="court | authority"),
                  q: str | None = Query(None, max_length=200),
                  plz: str | None = None, ort: str | None = None,
                  ags: str | None = None,
                  limit: int = Query(20, ge=1, le=100),
                  snapshot_id: str | None = SnapshotArg):
    conn = db()
    snap = check_snapshot(conn, snapshot_id)
    return respond(domain.place_resolve(
        conn, purpose=purpose, q=q, plz=plz, ort=ort, ags=ags, limit=limit,
        request_scope=request_scope(request), snapshot=snap))


@app.get("/resolve/competence", name="competence_resolve")
def competence_resolve(request: Request, place_id: str,
                       matter: str | None = None, kind: str | None = None,
                       snapshot_id: str | None = SnapshotArg):
    conn = db()
    snap = check_snapshot(conn, snapshot_id)
    return respond(domain.competence_resolve(
        conn, place_id=place_id, matter=matter, kind=kind,
        request_scope=request_scope(request), snapshot=snap))


# ---------------------------------------------------------------- courts

def court_chain_result(*, matter: str, plz: str | None = None,
                       ort: str | None = None, ortk: str | None = None,
                       place_id: str | None = None,
                       scope: dict | None = None,
                       snapshot_id: str | None = None) -> dict:
    """Domain call used by the REST route and by /research."""
    conn = db()
    snap = check_snapshot(conn, snapshot_id)
    return domain.court_chain(conn, matter=matter, plz=plz, ort=ort,
                              ortk=ortk, place_id=place_id,
                              request_scope=scope or {}, snapshot=snap)


@app.get("/resolve/court", name="court_chain")
def resolve_court(request: Request, matter: str, plz: str | None = None,
                  ort: str | None = None, ortk: str | None = None,
                  place_id: str | None = None,
                  snapshot_id: str | None = SnapshotArg):
    return respond(court_chain_result(
        matter=matter, plz=plz, ort=ort, ortk=ortk, place_id=place_id,
        scope=request_scope(request), snapshot_id=snapshot_id))


# ----------------------------------------------------------- non-courts

def authority_result(*, kind: str, ags: str | None = None,
                     place_id: str | None = None, scope: dict | None = None,
                     snapshot_id: str | None = None) -> dict:
    conn = db()
    snap = check_snapshot(conn, snapshot_id)
    return domain.authority_resolution(
        conn, kind=kind, ags=ags, place_id=place_id,
        request_scope=scope or {}, snapshot=snap,
        operation="authority_resolution")


@app.get("/resolve/authority", name="authority_resolution")
def resolve_authority(request: Request, kind: str, ags: str | None = None,
                      place_id: str | None = None,
                      snapshot_id: str | None = SnapshotArg):
    return respond(authority_result(
        kind=kind, ags=ags, place_id=place_id, scope=request_scope(request),
        snapshot_id=snapshot_id))


@app.get("/authorities/{authority_id}", name="authority_detail")
def authority_detail(request: Request, authority_id: int,
                     snapshot_id: str | None = SnapshotArg):
    conn = db()
    snap = check_snapshot(conn, snapshot_id)
    return respond(domain.authority_detail(
        conn, authority_id, request_scope=request_scope(request),
        snapshot=snap))


# ------------------------------------------------------- court registry

@app.get("/courts", name="court_search")
def courts(request: Request, q: str | None = Query(None, max_length=200),
           kind: str | None = None, land: str | None = None,
           level: str | None = None, status: str | None = None,
           limit: int = Query(50, ge=1, le=200),
           offset: int = Query(0, ge=0)):
    return respond(court_registry.court_search(
        db(), q=q, kind=kind, land=land, level=level, status=status,
        limit=limit, offset=offset, request_scope=request_scope(request)))


@app.get("/courts/mapping", name="court_mapping")
def courts_mapping(request: Request, limit: int = Query(5000, ge=1, le=5000),
                   offset: int = Query(0, ge=0)):
    return respond(court_registry.court_mapping(
        db(), limit=limit, offset=offset,
        request_scope=request_scope(request)))


@app.get("/courts/lookup", name="court_lookup")
def courts_lookup(request: Request,
                  name: str | None = Query(None, max_length=200),
                  ecli: str | None = Query(None, max_length=120),
                  xjustiz: str | None = Query(None, max_length=12),
                  authority_id: int | None = None, land: str | None = None):
    return respond(court_registry.court_lookup(
        db(), name=name, ecli=ecli, xjustiz=xjustiz,
        authority_id=authority_id, land=land,
        request_scope=request_scope(request)))


@app.get("/courts/{court_id}", name="court_detail")
def court_detail(request: Request, court_id: str):
    return respond(court_registry.court_detail(
        db(), court_id, request_scope=request_scope(request)))


# One read-only domain interface over the existing institutional and law stores.
from api.research import create_research_router  # noqa: E402
app.include_router(create_research_router(court_chain_result, authority_result))
