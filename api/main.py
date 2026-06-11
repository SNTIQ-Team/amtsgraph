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

import sqlite3
import unicodedata
from pathlib import Path

from fastapi import FastAPI, HTTPException

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "atlas.db"
app = FastAPI(title="Amtsgraph", version="2.1")

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


# ---------------------------------------------------------------- places

@app.get("/places")
def search_places(q: str, limit: int = 10):
    conn = db()
    norm = normalize(q)
    rows = conn.execute(
        """SELECT g.ags, g.name_simple AS name, g.kind,
                  k.name AS kreis, l.name AS land,
                  (SELECT COUNT(*) FROM gemeinde_plz p WHERE p.ags=g.ags) AS plz_count
           FROM gemeinde g
           JOIN kreis k ON k.ags = g.kreis_ags
           JOIN land  l ON l.code = k.land_code
           WHERE g.name_norm LIKE ? || '%'
           ORDER BY g.population IS NULL, g.population DESC LIMIT ?""",
        (norm, limit)).fetchall()
    # court-register places that didn't map to a Gemeinde (rare but real)
    extra = conn.execute(
        """SELECT plz, ort, ortk FROM jz_place
           WHERE ort_norm LIKE ? || '%' AND gemeinde_ags IS NULL LIMIT ?""",
        (norm, limit)).fetchall()
    return {"query": q,
            "matches": [dict(r) for r in rows],
            "court_register_only": [dict(r) for r in extra]}


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
        """SELECT e.relation, e.matter, a2.id, a2.name, a2.kind
           FROM authority_edge e JOIN authority a2 ON a2.id = e.to_authority
           WHERE e.from_authority = ?""", (authority_id,)).fetchall()
    card["related"] = [dict(e) for e in edges]
    card["caveats"] = caveats_for(conn, [("authority", str(authority_id))])
    return card
