"""Amtsgraph — resolution API (matter-aware).

Endpoints (see docs/ARCHITECTURE.md):

  GET /places?q=neustadt
      place candidates with Land/Kreis context (disambiguation picker)
  GET /matters
      legal-matter taxonomy for the court flow
  GET /resolve/court?plz=10115&ort=Berlin&matter=mahn
      exact instance chain for place x matter, with caveats + provenance
  GET /resolve/authority?ags=10041100&kind=auslaenderbehoerde
      competent non-court authority for a Gemeinde
  GET /authorities/{id}
      full card + related edges

Every answer carries `caveats` and `provenance`; clients must display both —
for legal use an honest warning beats a confident wrong answer.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import unicodedata
from functools import wraps
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

# Deployment override: AMTSGRAPH_DB=/path/to/atlas.db (default: repo layout)
DB_PATH = Path(os.environ.get(
    "AMTSGRAPH_DB",
    Path(__file__).resolve().parent.parent / "data" / "atlas.db"))
app = FastAPI(title="Amtsgraph", version="2.2")

COURT_KINDS = {"amtsgericht", "landgericht", "oberlandesgericht",
               "sozialgericht", "verwaltungsgericht", "arbeitsgericht",
               "finanzgericht"}


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


def caveats_for(conn, scopes: list[tuple[str, str]], matter: str | None = None):
    rows = []
    for level, key in scopes:
        rows += conn.execute(
            """SELECT severity, text_de, source FROM caveat
               WHERE scope_level = ? AND scope_key = ?
                 AND (matter IS NULL OR matter = ?)""",
            (level, key, matter)).fetchall()
    return [dict(r) for r in rows]


def authority_card(conn, authority_id: int) -> dict:
    a = conn.execute("SELECT * FROM authority WHERE id = ?",
                     (authority_id,)).fetchone()
    ext = conn.execute(
        "SELECT scheme, value FROM authority_external_id WHERE authority_id = ?",
        (authority_id,)).fetchall()
    card = {k: a[k] for k in a.keys() if not k.startswith("source")}
    card["external_ids"] = {r["scheme"]: r["value"] for r in ext}
    card["provenance"] = {"source": a["source"], "url": a["source_url"],
                          "fetched_at": a["fetched_at"]}
    return card


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
    conn = db()
    info = {r["key"]: r["value"] for r in
            conn.execute("SELECT key, value FROM build_info")}
    import json as _json
    return {"dataset": "Amtsgraph",
            "built_at": info.get("built_at"),
            "source_snapshots": _json.loads(info.get("snapshots", "{}"))}


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
        return {"query": q, "matches": [dict(r) for r in rows],
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
    return {"query": q, "matches": matches,
            "court_register_only": [dict(r) for r in extra]}


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
def graph():
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


# ---------------------------------------------------------------- courts

@app.get("/resolve/court")
def resolve_court(plz: str, matter: str, ort: str | None = None):
    conn = db()
    places = conn.execute(
        "SELECT * FROM jz_place WHERE plz = ?" +
        (" AND ort_norm = ?" if ort else ""),
        (plz, normalize(ort)) if ort else (plz,)).fetchall()
    if not places:
        raise HTTPException(404, f"unknown PLZ {plz}")
    if len({p["ortk"] for p in places}) > 1 and not ort:
        # one PLZ, several Orte in different court districts: must pick Ort
        return {"needs_ort": True,
                "options": [{"ort": p["ort"], "ortk": p["ortk"]} for p in places]}

    place = places[0]
    chain = conn.execute(
        """SELECT cc.position, cc.role, cc.note, cc.authority_id
           FROM court_chain cc
           WHERE cc.plz = ? AND cc.ortk = ? AND cc.matter = ?
           ORDER BY cc.role DESC, cc.position""",
        (place["plz"], place["ortk"], matter)).fetchall()
    if not chain:
        raise HTTPException(404, f"no chain for matter '{matter}' at this place")

    return {
        "place": {"plz": place["plz"], "ort": place["ort"]},
        "matter": matter,
        "chain": [{"position": c["position"], "role": c["role"],
                   "note": c["note"],
                   **authority_card(conn, c["authority_id"])} for c in chain],
        "caveats": caveats_for(conn, [("plz", plz),
                                      ("jz_place", f"{plz}|{place['ortk']}")],
                               matter),
    }


# ----------------------------------------------------------- non-courts

@app.get("/resolve/authority")
def resolve_authority(ags: str, kind: str):
    conn = db()
    if kind in COURT_KINDS:
        raise HTTPException(400, "court kinds resolve via /resolve/court "
                            "(competence is matter-specific)")
    g = conn.execute("SELECT * FROM gemeinde WHERE ags = ?", (ags,)).fetchone()
    if not g:
        raise HTTPException(404, "unknown AGS")
    hits = conn.execute(
        """SELECT a.id, MIN(c.rank) AS rank FROM authority a
           JOIN competence c ON c.authority_id = a.id
           WHERE c.kind = ? AND a.valid_to IS NULL AND (
                 (c.level='gemeinde' AND c.area = ?)
              OR (c.level='kreis'    AND c.area = ?)
              OR (c.level='land'     AND c.area = ?)
              OR (c.level='plz' AND c.area IN
                    (SELECT plz FROM gemeinde_plz WHERE ags = ?)))
           GROUP BY a.id ORDER BY rank, a.id""",
        (kind, ags, g["kreis_ags"], ags[:2], ags)).fetchall()
    if not hits:
        raise HTTPException(404, f"no {kind} known for {ags}")
    primary = [h for h in hits if h["rank"] == 0]
    supervisory = [h for h in hits if h["rank"] > 0]
    cards = [authority_card(conn, h["id"]) for h in primary]
    return {"gemeinde": g["name_simple"], "kind": kind,
            "resolved": cards[0] if len(cards) == 1 else None,
            "candidates": cards if len(cards) > 1 else [],
            "supervisory": [authority_card(conn, h["id"])
                            for h in supervisory],
            "caveats": caveats_for(conn, [("gemeinde", ags)])}


@app.get("/authorities/{authority_id}")
def authority_detail(authority_id: int):
    conn = db()
    if not conn.execute("SELECT 1 FROM authority WHERE id = ?",
                        (authority_id,)).fetchone():
        raise HTTPException(404)
    card = authority_card(conn, authority_id)
    edges = conn.execute(
        """SELECT e.relation, e.matter, e.note, e.source, e.source_url,
                  a2.id, a2.name, a2.kind
           FROM authority_edge e JOIN authority a2 ON a2.id = e.to_authority
           WHERE e.from_authority = ?""", (authority_id,)).fetchall()
    card["related"] = [dict(e) for e in edges]
    incoming = conn.execute(
        """SELECT e.relation, e.matter, e.note, e.source, e.source_url,
                  a2.id, a2.name, a2.kind
           FROM authority_edge e
           JOIN authority a2 ON a2.id = e.from_authority
           WHERE e.to_authority = ?""", (authority_id,)).fetchall()
    card["related_incoming"] = [dict(e) for e in incoming]
    card["caveats"] = caveats_for(conn, [("authority", str(authority_id))])
    return card
