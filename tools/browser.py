#!/usr/bin/env python3
"""Amtsgraph — hierarchical data browser (test tool, stdlib only).

Serves data/atlas.db read-only: Land → Kreis → Gemeinde tree, instant
search, authority cards per kind, and the court resolver (PLZ × matter →
instance chain) with caveats.

    python3 tools/browser.py [--port 8400]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB = Path(__file__).resolve().parent.parent / "data" / "atlas.db"


def conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def normalize(name: str) -> str:
    s = name.lower().strip()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.split(",")[0].split("(")[0].strip()


def rows(sql, *args):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


# ------------------------------------------------------------------- API

def api_lands():
    return rows("""SELECT l.code, l.name,
                          (SELECT COUNT(*) FROM kreis k WHERE k.land_code=l.code) AS kreise
                   FROM land l ORDER BY l.name""")


def api_kreise(land):
    return rows("""SELECT k.ags, k.name, k.kind, k.regierungsbezirk,
                          (SELECT COUNT(*) FROM gemeinde g WHERE g.kreis_ags=k.ags) AS gemeinden
                   FROM kreis k WHERE k.land_code=? ORDER BY k.name""", land)


def api_gemeinden(kreis):
    return rows("""SELECT ags, ars, name_simple AS name, kind
                   FROM gemeinde WHERE kreis_ags=? ORDER BY name_simple""", kreis)


def api_search(q):
    n = normalize(q)
    gem = rows("""SELECT g.ags, g.name_simple AS name, g.kind,
                         k.name AS kreis, l.name AS land
                  FROM gemeinde g JOIN kreis k ON k.ags=g.kreis_ags
                  JOIN land l ON l.code=k.land_code
                  WHERE g.name_norm LIKE ?||'%' ORDER BY length(g.name_norm)
                  LIMIT 25""", n)
    auth = rows("""SELECT id, kind, name, city FROM authority
                   WHERE name_norm LIKE '%'||?||'%' AND valid_to IS NULL
                   ORDER BY length(name) LIMIT 15""", n)
    plz = rows("""SELECT DISTINCT jp.plz, jp.ort, jp.ortk FROM jz_place jp
                  WHERE jp.plz LIKE ?||'%' LIMIT 15""", q.strip()) \
        if q.strip()[:1].isdigit() else []
    return {"gemeinden": gem, "authorities": auth, "plz": plz}


def api_gemeinde(ags):
    g = rows("""SELECT g.*, k.name AS kreis_name, k.regierungsbezirk,
                       l.name AS land_name
                FROM gemeinde g JOIN kreis k ON k.ags=g.kreis_ags
                JOIN land l ON l.code=k.land_code WHERE g.ags=?""", ags)
    if not g:
        return {"error": "unknown ags"}
    g = g[0]
    plz = [r["plz"] for r in rows(
        "SELECT plz FROM gemeinde_plz WHERE ags=? ORDER BY plz", ags)]
    auth = rows("""
        SELECT DISTINCT a.id, a.kind, ak.label_de AS kind_label, a.name,
               a.legal_form, a.street, a.plz, a.city, a.phone, a.fax,
               a.email, a.web, c.level
        FROM authority a
        JOIN authority_kind ak ON ak.kind = a.kind OR ak.kind = (
             SELECT c2.kind FROM competence c2
             WHERE c2.authority_id=a.id LIMIT 1)
        JOIN competence c ON c.authority_id = a.id
        WHERE a.valid_to IS NULL AND (
              (c.level='gemeinde' AND c.area=?)
           OR (c.level='kreis'   AND c.area=?)
           OR (c.level='land'    AND c.area=?))
        ORDER BY c.kind, c.level""", ags, g["kreis_ags"], ags[:2])
    # competence kind per row (an office wears many hats)
    comp = rows("""
        SELECT c.kind, c.level, c.authority_id, c.rank FROM competence c
        WHERE (c.level='gemeinde' AND c.area=?)
           OR (c.level='kreis'   AND c.area=?)
           OR (c.level='land'    AND c.area=?)""", ags, g["kreis_ags"], ags[:2])
    kinds_by_auth = {}
    rank_by_auth = {}
    for c in comp:
        kinds_by_auth.setdefault(c["authority_id"], set()).add(c["kind"])
        rank_by_auth[c["authority_id"]] = min(
            rank_by_auth.get(c["authority_id"], 9), c["rank"])
    seen, out_auth = set(), []
    for a in auth:
        if a["id"] in seen:
            continue
        seen.add(a["id"])
        a["competence_kinds"] = sorted(kinds_by_auth.get(a["id"], []))
        a["rank"] = rank_by_auth.get(a["id"], 0)
        out_auth.append(a)
    out_auth.sort(key=lambda a: (a["rank"], a["name"]))
    caveats = rows("""SELECT severity, matter, text_de, source FROM caveat
                      WHERE (scope_level='gemeinde' AND scope_key=?)
                         OR (scope_level='kreis' AND scope_key=?)""",
                   ags, g["kreis_ags"])
    jz = rows("""SELECT DISTINCT jp.plz, jp.ort, jp.ortk FROM jz_place jp
                 WHERE jp.gemeinde_ags=? OR jp.plz IN (
                   SELECT plz FROM gemeinde_plz WHERE ags=?)
                 ORDER BY jp.plz""", ags, ags)
    return {"gemeinde": dict(g), "plz": plz, "authorities": out_auth,
            "caveats": caveats, "jz_places": jz}


def api_matters():
    return rows("SELECT code, label_de, grp, core FROM matter "
                "ORDER BY core DESC, label_de")


def api_court(plz, ortk, matter):
    chain = rows("""
        SELECT cc.position, cc.role, cc.note, a.id, a.kind, a.name,
               a.street AS address, a.postal_address, a.phone, a.fax,
               a.email, a.web, a.erv_note,
               (SELECT value FROM authority_external_id e
                WHERE e.authority_id=a.id AND e.scheme='xjustiz') AS xjustiz_id
        FROM court_chain cc JOIN authority a ON a.id=cc.authority_id
        WHERE cc.plz=? AND cc.ortk=? AND cc.matter=?
        ORDER BY cc.role DESC, cc.position""", plz, ortk, matter)
    caveats = rows("""SELECT severity, text_de FROM caveat
                      WHERE (scope_level='plz' AND scope_key=?)
                         OR (scope_level='jz_place' AND scope_key=?)
                      AND (matter IS NULL OR matter=?)""",
                   plz, f"{plz}|{ortk}", matter)
    return {"chain": chain, "caveats": caveats}


def api_stats():
    one = lambda sql: rows(sql)[0]["n"]                      # noqa: E731
    return {
        "authorities": one("SELECT COUNT(*) n FROM authority"),
        "chain_links": one("SELECT COUNT(*) n FROM court_chain"),
        "places": one("SELECT COUNT(*) n FROM jz_place"),
        "competences": one("SELECT COUNT(*) n FROM competence"),
        "caveats": one("SELECT COUNT(*) n FROM caveat"),
        "gemeinden": one("SELECT COUNT(*) n FROM gemeinde"),
        "built_at": rows("SELECT value FROM build_info WHERE key='built_at'")
        [0]["value"],
    }


# -------------------------------------------------------- graph (QFS-style)

REL_STYLE = {"appeal": 1.0, "supervision": 0.7, "parent": 0.45,
             "successor": 1.0}


def api_graph_node(aid):
    """One authority + its edges, for fractal expansion in the graph view."""
    a = rows("""SELECT a.id, a.kind, a.name, a.legal_form, a.source,
                       COALESCE(st.trust, 0.6) AS trust
                FROM authority a
                LEFT JOIN source_trust st ON st.source = a.source
                WHERE a.id=?""", int(aid))
    if not a:
        return {"error": "unknown id"}
    edges = rows("""
        SELECT e.from_authority AS src, e.to_authority AS dst, e.relation,
               e.matter, e.delta, e.trust,
               a2.id, a2.kind, a2.name, a2.source,
               COALESCE(st.trust, 0.6) AS node_trust
        FROM authority_edge e
        JOIN authority a2 ON a2.id = CASE WHEN e.from_authority=?
                                          THEN e.to_authority
                                          ELSE e.from_authority END
        LEFT JOIN source_trust st ON st.source = a2.source
        WHERE e.from_authority=? OR e.to_authority=?""",
        int(aid), int(aid), int(aid))
    comp = rows("""SELECT kind, level, area, rank FROM competence
                   WHERE authority_id=? LIMIT 40""", int(aid))
    return {"node": a[0], "edges": edges, "competences": comp}


def api_graph_traverse(src, dst):
    """Least-cost path where cost = -log(conductance * trust).

    Conductance of hop along an edge (QFS): 0.5 + 0.5*delta*(role_a-role_b)
    with role +1 at the edge's from-side, -1 at its to-side. Traversing WITH
    the edge direction => conductance = 0.5+delta; against => 0.5-delta*?
    Simplified to: forward = (1+delta)/2 mapped onto [0.05..1],
    backward = (1-delta)/2 floored at 0.05 (hard but possible, per QFS).
    """
    import heapq, math
    src, dst = int(src), int(dst)
    adj = {}
    for e in rows("SELECT from_authority f, to_authority t, delta, trust, "
                  "relation FROM authority_edge"):
        fwd = max(0.05, (1 + e["delta"]) / 2) * e["trust"]
        bwd = max(0.05, (1 - e["delta"]) / 2) * e["trust"]
        adj.setdefault(e["f"], []).append((e["t"], -math.log(fwd), e["relation"], "->"))
        adj.setdefault(e["t"], []).append((e["f"], -math.log(bwd), e["relation"], "<-"))
    dist = {src: 0.0}
    prev = {}
    pq = [(0.0, src)]
    seen = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u == dst:
            break
        if len(seen) > 20000:
            break
        for v, w, rel, direction in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = (u, rel, direction, w)
                heapq.heappush(pq, (nd, v))
    if dst not in dist:
        return {"found": False}
    path = []
    cur = dst
    while cur != src:
        u, rel, direction, w = prev[cur]
        path.append({"to": cur, "relation": rel, "dir": direction,
                     "cost": round(w, 3)})
        cur = u
    path.reverse()
    names = {r["id"]: r["name"] for r in rows(
        f"SELECT id, name FROM authority WHERE id IN "
        f"({','.join(str(p['to']) for p in path)}, {src})")}
    return {"found": True, "total_cost": round(dist[dst], 3),
            "start": names.get(src),
            "hops": [{**p, "name": names.get(p["to"])} for p in path]}


# --------------------------------------------------- provenance & hierarchy

def api_provenance():
    """Build/ingestion log: every fact's origin. Each (source, kind) pair
    is a 'commit' that brought N authorities into the graph, tagged with
    the source snapshot date and its trust. Newest first."""
    import json as _json
    snaps = _json.loads(rows(
        "SELECT value FROM build_info WHERE key='snapshots'")[0]["value"])
    trust = {r["source"]: r["trust"] for r in rows(
        "SELECT source, trust FROM source_trust")}
    commits = rows("""SELECT source, kind, COUNT(*) n,
                             MIN(fetched_at) fetched
                      FROM authority WHERE valid_to IS NULL
                      GROUP BY source, kind ORDER BY source, n DESC""")
    for c in commits:
        c["date"] = snaps.get(c["source"], (c["fetched"] or "")[:10])
        c["trust"] = trust.get(c["source"], 0.6)
    superseded = rows("SELECT COUNT(*) n FROM authority "
                      "WHERE valid_to IS NOT NULL")[0]["n"]
    return {"commits": commits, "snapshots": snaps, "trust": trust,
            "superseded": superseded,
            "built_at": rows("SELECT value FROM build_info "
                             "WHERE key='built_at'")[0]["value"],
            "sources": sorted(trust, key=lambda s: -trust[s])}


def api_hierarchy():
    """Administrative tier tree: Bund → 16 Länder → Kreise, with live
    authority counts per node (the federal administrative skeleton)."""
    lands = rows("""SELECT l.code, l.name,
        (SELECT COUNT(*) FROM kreis k WHERE k.land_code=l.code) kreise,
        (SELECT COUNT(*) FROM gemeinde g JOIN kreis k ON k.ags=g.kreis_ags
         WHERE k.land_code=l.code) gemeinden,
        (SELECT COUNT(DISTINCT a.id) FROM authority a
         JOIN competence c ON c.authority_id=a.id
         WHERE a.valid_to IS NULL AND substr(c.area,1,2)=l.code) authorities
        FROM land l ORDER BY l.name""")
    return {"lands": lands,
            "total_authorities": rows("SELECT COUNT(*) n FROM authority "
                                      "WHERE valid_to IS NULL")[0]["n"]}


def api_seed(land=None):
    """Graph seed: the most-connected authorities (highest edge degree).
    With ?land=<code> restricts to that Land — used when a Land node in
    the geographic overview is clicked to load its organisational web."""
    if land:
        return rows("""SELECT a.id, a.name, a.kind,
            (SELECT COUNT(*) FROM authority_edge e
             WHERE e.from_authority=a.id OR e.to_authority=a.id) deg
            FROM authority a
            JOIN competence c ON c.authority_id=a.id
            WHERE a.valid_to IS NULL AND substr(c.area,1,2)=?
            GROUP BY a.id ORDER BY deg DESC LIMIT 12""", land)
    return rows("""SELECT a.id, a.name, a.kind,
        (SELECT COUNT(*) FROM authority_edge e
         WHERE e.from_authority=a.id OR e.to_authority=a.id) deg
        FROM authority a WHERE a.valid_to IS NULL
        ORDER BY deg DESC LIMIT 12""")


# ------------------------------------------------------------------ HTML

# SNTIQ brand mark, served at /sntiq.svg (favicon + header logo)
try:
    SNTIQ_SVG = (Path(__file__).resolve().parent / "sntiq.svg").read_bytes()
except OSError:
    SNTIQ_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"/>'

PAGE = r"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amtsgraph — SNTIQ</title>
<link rel="icon" href="/sntiq.svg">
<style>
:root{
  --bg:#0d1017;--bg2:#141922;--bg3:#1b2230;--line:#242c3a;
  --fg:#e6ebf2;--dim:#8b97a8;--dim2:#5a6577;
  --acc:#6ea8fe;--ok:#4ec9a5;--warn:#e0b341;--bad:#e0645a;--eu:#c58fff;
  --by:#5fd0e0;--land:#9aa7ff;
  /* back-compat aliases used across the JS templates */
  --panel:#141922;--grn:#4ec9a5;--yel:#e0b341;--red:#e0645a;
  --mono:"SF Mono",ui-monospace,Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:5px}
::-webkit-scrollbar-track{background:transparent}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 var(--sans);display:flex;flex-direction:column;height:100vh}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
code,.mono{font-family:var(--mono)}
header{padding:10px 18px;background:var(--bg2);display:flex;gap:16px;align-items:center;
border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
.brand{font-weight:700;font-size:16px;letter-spacing:.3px;white-space:nowrap;
display:flex;align-items:center;gap:8px}
.brand .logo{width:22px;height:22px;border-radius:5px}
.brand small{color:var(--dim);font-weight:400;font-size:12px;margin-left:2px}
#stats{color:var(--dim2);font-size:12px;margin-left:auto;white-space:nowrap}
#q{flex:1;max-width:520px;background:var(--bg3);border:1px solid var(--line);color:var(--fg);
padding:8px 12px;border-radius:7px;font:inherit;font-size:13px;outline:none}
#q:focus{border-color:var(--acc)}
main{flex:1;display:flex;min-height:0}
#tree{width:390px;overflow:auto;padding:12px;border-right:1px solid var(--line);
background:var(--bg2)}
#detail{flex:1;overflow:auto;padding:18px 26px}
.node{cursor:pointer;padding:3px 7px;border-radius:5px;white-space:nowrap;font-size:13px}
.node:hover{background:var(--bg3)}.node .n{color:var(--dim2);font-size:11px}
.kids{margin-left:18px;border-left:1px solid var(--line);padding-left:8px}
.chip{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;
background:#1b2740;color:var(--acc);margin:1px 4px 1px 0;border:1px solid var(--line)}
.chip.lf{background:#123a2a;color:var(--ok);border-color:#1c4a37}
.card{background:var(--bg2);border:1px solid var(--line);border-radius:9px;
padding:11px 15px;margin:9px 0}
.card h3{margin:0 0 5px;font-size:14px;color:var(--fg)}
.card .meta{color:var(--dim);font-size:12px}
.caveat{border-left:3px solid var(--warn);background:#221d10;padding:9px 13px;
margin:9px 0;font-size:12px;border-radius:0 7px 7px 0;color:var(--fg)}
.role-pros{opacity:.72}
.pos{color:var(--warn);font-weight:700}
h2{font-size:16px;border-bottom:1px solid var(--line);padding-bottom:6px;margin:20px 0 10px}
select{background:var(--bg3);color:var(--fg);border:1px solid var(--line);
border-radius:7px;padding:6px 9px;font:inherit;font-size:13px}
button{background:var(--bg3);color:var(--fg);border:1px solid var(--line);
border-radius:7px;padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}
button:hover{border-color:var(--acc)}
.hint{color:var(--dim);font-size:12px}
.sr{padding:5px 9px;cursor:pointer;border-radius:5px}.sr:hover{background:var(--bg2)}
.sr .ctx{color:var(--dim2);font-size:11px}
.kbadge{color:var(--dim);font-weight:400}
/* nav + view switching (shared chrome with Lexgraph) */
nav{display:flex;gap:4px;margin-left:8px}
nav button{background:none;border:1px solid transparent;color:var(--dim);
padding:7px 14px;border-radius:7px;cursor:pointer;font-size:13.5px}
nav button:hover{color:var(--fg);background:var(--bg3)}
nav button.on{color:var(--fg);background:var(--bg3);border-color:var(--line)}
.right{margin-left:auto;display:flex;align-items:center;gap:14px}
.lang{display:flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
.lang button{background:var(--bg3);border:none;color:var(--dim);
padding:6px 11px;cursor:pointer;font-size:12px;font-weight:600}
.lang button.on{background:var(--acc);color:#08121f}
.built{color:var(--dim2);font-size:12px;white-space:nowrap}
.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;
background:var(--ok);margin-right:6px;vertical-align:middle}
main{flex:1;display:flex;min-height:0;position:relative;overflow:hidden}
.view{display:none;width:100%;min-height:0}
#overview.on{display:flex;flex-direction:column}
#git.on{display:flex;flex-direction:column}
#hier.on{display:block;overflow:auto;padding:18px 22px}
#graph.on{display:block;position:relative}
.stats{display:flex;gap:10px;padding:14px 18px;flex-wrap:wrap;
border-bottom:1px solid var(--line);background:var(--bg2)}
.stat{background:var(--bg3);border:1px solid var(--line);border-radius:9px;
padding:9px 14px;min-width:96px}
.stat .n{font-size:21px;font-weight:700}
.stat.acc .n{color:var(--acc)}.stat.by .n{color:var(--by)}.stat.eu .n{color:var(--eu)}
.stat .l{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.4px}
.split{display:flex;flex:1;overflow:hidden}
.col{display:flex;flex-direction:column;overflow:hidden}
.col.treecol{width:400px;border-right:1px solid var(--line);background:var(--bg2)}
.col.detailcol{flex:1}
.colhead{padding:10px 16px;border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:10px;background:var(--bg2)}
.colhead h3{margin:0;font-size:13px;text-transform:uppercase;letter-spacing:.5px;
color:var(--dim);white-space:nowrap}
.scroll{overflow-y:auto;flex:1}
.search{background:var(--bg3);border:1px solid var(--line);color:var(--fg);
border-radius:7px;padding:7px 11px;font:inherit;font-size:13px;outline:none}
.search:focus{border-color:var(--acc)}
.chips{display:flex;gap:5px;flex-wrap:wrap}
.chips .chip{cursor:pointer}.chips .chip.on{border-color:var(--acc);background:#1b2740;color:var(--fg)}
/* git log */
#git .colhead{gap:14px}
.gitstat{margin-left:auto;color:var(--dim2);font-size:12px}
#gitlog{overflow-y:auto;flex:1;padding:6px 0 40px}
.commit{display:grid;grid-template-columns:56px 110px 1fr;align-items:stretch;
border-bottom:1px solid var(--bg2)}
.commit:hover{background:var(--bg2)}
.rail{position:relative}.rail canvas{position:absolute;inset:0;width:100%;height:100%}
.chash{font-family:var(--mono);font-size:11px;color:var(--dim2);padding:11px 8px 0 0;text-align:right}
.cbody{padding:9px 16px 9px 4px;min-width:0}
.cmsg{font-size:13px}
.mtag{font-size:9px;font-weight:700;letter-spacing:.4px;padding:1px 5px;border-radius:3px;
margin-right:7px;vertical-align:middle;text-transform:uppercase}
.cmeta{font-size:11px;color:var(--dim);margin-top:3px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.cref{font-size:10px;background:var(--bg3);border:1px solid var(--line);border-radius:10px;
padding:0 7px;color:var(--dim);font-family:var(--mono)}
/* hierarchy tree */
.tree details{margin:2px 0}
.tree summary{cursor:pointer;padding:5px 8px;border-radius:6px;list-style:none;
display:flex;align-items:center;gap:8px}
.tree summary::-webkit-details-marker{display:none}
.tree summary:hover{background:var(--bg2)}
.tree summary .tw{color:var(--dim2);width:12px;display:inline-block;transition:transform .15s}
.tree details[open]>summary .tw{transform:rotate(90deg)}
.tree .grp{border-left:1px solid var(--line);margin-left:13px;padding-left:10px}
.tree .cnt{color:var(--dim2);font-size:12px}
.tree .leaf{padding:3px 8px 3px 30px;font-size:12.5px;color:var(--dim);border-bottom:1px solid var(--bg2)}
.tree .leaf .d{color:var(--dim2);font-family:var(--mono);margin-right:8px}
.tree .lvl1>summary{font-size:15px;font-weight:600}
.tree h2{font-size:15px;border:0}
.bar{height:5px;border-radius:3px;background:var(--bg3);width:120px;display:inline-block;
overflow:hidden;vertical-align:middle;margin-left:6px}.bar>i{display:block;height:100%;background:var(--acc)}
/* graph tab — glass panels */
#gc{position:absolute;inset:0;width:100%;height:100%;cursor:grab;display:block}
.gpanel{position:absolute;top:14px;left:14px;background:rgba(20,25,34,.92);
border:1px solid var(--line);border-radius:10px;padding:12px 14px;backdrop-filter:blur(6px);
max-width:250px;font-size:12px}
.gpanel h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim)}
.gpanel label{display:block;margin:4px 0;cursor:pointer}
.gsearch{position:absolute;top:14px;right:14px;width:250px}
#ginfo{position:absolute;right:14px;top:56px;width:330px;max-height:72vh;overflow:auto}
#ginfo .card{background:rgba(20,25,34,.94);backdrop-filter:blur(6px)}
.gbar-status{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);
background:rgba(20,25,34,.92);border:1px solid var(--line);border-radius:8px;
padding:6px 14px;font-size:12px;color:var(--dim);backdrop-filter:blur(6px);max-width:70vw}
</style></head><body>
<header>
 <div class="brand"><img src="/sntiq.svg" class="logo" alt="SNTIQ">Amtsgraph
 <small>Kompetenzgraph der deutschen Behörden</small></div>
 <nav>
  <button data-v="overview" class="on" data-i="nav_overview"></button>
  <button data-v="git" data-i="nav_git"></button>
  <button data-v="hier" data-i="nav_hier"></button>
  <button data-v="graph" data-i="nav_graph"></button>
 </nav>
 <div class="right">
  <div class="lang"><button data-l="de" class="on">DE</button><button data-l="en">EN</button></div>
  <span class="built" id="built"><span class="pulse"></span>…</span></div>
</header>
<main>
 <section class="view on" id="overview">
  <div class="stats" id="stats2"></div>
  <div class="split">
   <div class="col treecol">
    <div class="colhead"><h3 data-i="tree_title"></h3>
     <input id="q" class="search" style="flex:1"></div>
    <div class="scroll" id="tree"></div>
   </div>
   <div class="col detailcol">
    <div class="scroll" id="detail" style="padding:18px 24px">
     <p class="hint" data-i="tree_hint"></p></div>
   </div>
  </div>
 </section>

 <section class="view" id="git">
  <div class="colhead"><h3 data-i="git_title"></h3>
   <div class="chips" id="gitlanes"></div><span class="gitstat" id="gitstat"></span></div>
  <div class="scroll" id="gitlog"></div>
 </section>

 <section class="view" id="hier"></section>

 <section class="view" id="graph">
  <canvas id="gc"></canvas>
  <div class="gpanel"><h4 data-i="g_web"></h4>
   <button id="geohome" style="width:100%;margin-bottom:8px" data-i="geo_home"></button>
   <label class="chip"><input type="checkbox" id="l-parent" checked> parent δ=0.45</label>
   <label class="chip"><input type="checkbox" id="l-supervision" checked> supervision δ=0.7</label>
   <label class="chip"><input type="checkbox" id="l-appeal" checked> appeal δ=1.0</label>
   <div class="hint" style="margin-top:8px" data-i="g_hint"></div>
  </div>
  <input id="gsearch" class="search gsearch">
  <div id="ginfo"></div>
  <div class="gbar-status" id="gstatus"></div>
 </section>
</main>
<script>
const $=s=>document.querySelector(s);
const api=async p=>(await fetch('/api/'+p)).json();
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let MATTERS=[];

/* i18n — UI chrome only (authority data stays German) */
let LANG=localStorage.getItem('amts_lang')||'de';
const T={de:{
  nav_overview:'Wiki & Realtime',nav_git:'Git-Log',nav_hier:'Hierarchie',nav_graph:'Graph',
  stand:'Stand',s_beh:'Behörden',s_orte:'Orte',s_links:'Instanz-Links',
  s_komp:'Kompetenzen',s_gem:'Gemeinden',s_hinw:'Hinweise',
  tree_title:'Verwaltungsbaum',tree_ph:'Stadt, PLZ, Behörde… (Nördlingen, 12555, Jobcenter Kiel)',
  tree_hint:'Wähle links Land → Kreis → Gemeinde, oder nutze die Suche.',
  git_title:'Aufbau-Log — Herkunft jeder Angabe',git_all:'alle',
  git_commits:'Ingest-Commits',git_facts:'Angaben',git_dedup:'ersetzt (Dedup)',
  git_merge:'Cross-Source-Dedup:',git_merged:'Datensätze zusammengeführt/ersetzt',
  hier_title:'Verwaltungshierarchie — Bund → Länder → Kreise → Gemeinden',
  hier_root:'🇩🇪 Bundesrepublik Deutschland',hier_sub:'Behörden · 16 Länder',
  hier_kreise:'Kreise',hier_gem:'Gemeinden',hier_beh:'Behörden',hier_load:'Kreise laden…',
  g_web:'⚛ Organisational web',g_hint:'Klick = expandieren · Ziehen = fixieren · Shift-Klick zweier Knoten = vertrauenswürdigster Pfad',
  g_ph:'Behörde suchen & ins Netz laden…',g_seed:'Startknoten',g_expand:'Knoten klicken zum Expandieren, Shift-Klick = Pfad',
  g_loaded:'geladen',g_none:'keine Behörde gefunden',
  geo_hint:'Deutschland-Übersicht · Klick auf ein Land lädt dessen Behördennetz · Klick auf „Bund" zurück',
  geo_home:'🗺 Deutschland-Übersicht',
},en:{
  nav_overview:'Wiki & Realtime',nav_git:'Git log',nav_hier:'Hierarchy',nav_graph:'Graph',
  stand:'as of',s_beh:'authorities',s_orte:'places',s_links:'instance links',
  s_komp:'competences',s_gem:'municipalities',s_hinw:'caveats',
  tree_title:'Administrative tree',tree_ph:'city, postal code, authority… (Nördlingen, 12555, Jobcenter Kiel)',
  tree_hint:'Pick Land → Kreis → Gemeinde on the left, or use search.',
  git_title:'Build log — provenance of every fact',git_all:'all',
  git_commits:'ingest commits',git_facts:'records',git_dedup:'superseded (dedup)',
  git_merge:'cross-source dedup:',git_merged:'records merged/superseded',
  hier_title:'Administrative hierarchy — Federation → States → Districts → Municipalities',
  hier_root:'🇩🇪 Federal Republic of Germany',hier_sub:'authorities · 16 states',
  hier_kreise:'districts',hier_gem:'municipalities',hier_beh:'authorities',hier_load:'loading districts…',
  g_web:'⚛ Organisational web',g_hint:'click = expand · drag = pin · shift-click two nodes = most-trusted path',
  g_ph:'search an authority & load into the web…',g_seed:'start node',g_expand:'click a node to expand, shift-click = path',
  g_loaded:'loaded',g_none:'no authority found',
  geo_hint:'Germany overview · click a state to load its authority web · click "Bund" to return',
  geo_home:'🗺 Germany overview',
}};
const tr=k=>(T[LANG][k]!==undefined?T[LANG][k]:k);
function applyLang(){
  document.documentElement.lang=LANG;
  document.querySelectorAll('[data-i]').forEach(el=>el.textContent=tr(el.dataset.i));
  const q=$('#q');if(q)q.placeholder=tr('tree_ph');
  const gs=$('#gsearch');if(gs)gs.placeholder=tr('g_ph');
  document.querySelectorAll('.lang button').forEach(b=>b.classList.toggle('on',b.dataset.l===LANG));
}
document.querySelectorAll('.lang button').forEach(b=>b.onclick=()=>{
  LANG=b.dataset.l;localStorage.setItem('amts_lang',LANG);applyLang();
  if(STATS)renderChrome();
});

const fmtDate=s=>{const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(s||'');return m?`${m[3]}.${m[2]}.${m[1]}`:(s||'');};
let STATS=null;
function renderStatsBar(){
  const st=STATS,loc=LANG==='de'?'de':'en';
  $('#built').innerHTML=`<span class="pulse"></span>${tr('stand')} ${fmtDate(st.built_at)}`;
  const cells=[['acc',st.authorities,'s_beh'],['',st.places,'s_orte'],
    ['acc',st.chain_links,'s_links'],['',st.competences,'s_komp'],
    ['by',st.gemeinden,'s_gem'],['',st.caveats,'s_hinw']];
  $('#stats2').innerHTML=cells.map(([c,n,l])=>
    `<div class="stat ${c}"><div class="n">${Number(n).toLocaleString(loc)}</div><div class="l">${tr(l)}</div></div>`).join('');
}
function renderChrome(){renderStatsBar();if(PROV)renderGit();if(HIER)renderHier();}
async function init(){
  applyLang();
  STATS=await api('stats');
  renderStatsBar();
  MATTERS=await api('matters');
  const lands=await api('lands');
  $('#tree').innerHTML=lands.map(l=>
    `<div class="node" data-land="${l.code}">▸ ${esc(l.name)} <span class="n">${l.kreise} Kreise</span><div class="kids" hidden></div></div>`).join('');
  await renderGit(); await renderHier(); geoOverview();
}

/* ---- nav / view switching ---- */
let gitLoaded=false, hierLoaded=false, graphSeeded=false;
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.view').forEach(v=>v.classList.toggle('on',v.id===b.dataset.v));
  if(b.dataset.v==='git')drawGit();
  if(b.dataset.v==='graph'){gResize();if(!graphSeeded)geoOverview();}
});
function showTab(v){const b=document.querySelector(`nav button[data-v="${v}"]`);if(b)b.click();}

/* ---- Git-Log: provenance / build log ---- */
const SRC_COL={destatis:'#4ec9a5',justizadressen:'#6ea8fe',ba:'#e0b341',
  bayernportal:'#5fd0e0',pvog:'#9aa7ff',override:'#c58fff'};
let PROV=null, provFilter=null;
async function renderGit(){
  PROV=await api('provenance');
  const bySrc={};PROV.commits.forEach(c=>bySrc[c.source]=(bySrc[c.source]||0)+c.n);
  const loc=LANG==='de'?'de':'en';
  $('#gitlanes').innerHTML=
    `<span class="chip${provFilter==null?' on':''}" data-s="all">${tr('git_all')}</span>`+
    PROV.sources.map(s=>`<span class="chip${provFilter===s?' on':''}" data-s="${s}">
      <span style="color:${SRC_COL[s]||'#8b97a8'}">●</span> ${s} <span style="color:var(--dim2)">${(bySrc[s]||0).toLocaleString(loc)} · trust ${PROV.trust[s]}</span></span>`).join('');
  document.querySelectorAll('#gitlanes .chip').forEach(c=>c.onclick=()=>{
    provFilter=c.dataset.s==='all'?null:c.dataset.s;drawGit();});
  const total=PROV.commits.reduce((a,c)=>a+c.n,0);
  $('#gitstat').textContent=`${PROV.commits.length} ${tr('git_commits')} · ${total.toLocaleString(loc)} ${tr('git_facts')} · ${PROV.superseded} ${tr('git_dedup')}`;
  gitLoaded=true;drawGit();
}
function drawGit(){
  if(!PROV)return;
  const rows=PROV.commits.filter(c=>provFilter==null||c.source===provFilter);
  let html=rows.map(c=>{
    const col=SRC_COL[c.source]||'#8b97a8';
    const hash=(c.source.slice(0,3)+c.kind.slice(0,3)+c.n).replace(/[^a-z0-9]/gi,'').slice(0,8).padEnd(8,'0');
    return `<div class="commit" data-col="${col}">
      <div class="rail"></div><div class="chash">${hash}</div>
      <div class="cbody"><div class="cmsg"><span class="mtag" style="background:${col}22;color:${col}">${esc(c.source)}</span>
        + ${c.n.toLocaleString('de')} × <b>${esc(c.kind)}</b></div>
        <div class="cmeta"><span>${fmtDate(c.date)}</span><span class="cref">trust ${c.trust}</span></div></div></div>`;
  }).join('');
  // a final dedup/merge commit
  html+=`<div class="commit" data-col="#c58fff" data-merge="1">
    <div class="rail"></div><div class="chash">dedupmrg</div>
    <div class="cbody"><div class="cmsg"><span class="mtag" style="background:#173a2a;color:#4ec9a5">⑃ merge</span>
      ${tr('git_merge')} ${PROV.superseded} ${tr('git_merged')}</div>
      <div class="cmeta"><span>${fmtDate(PROV.built_at)}</span><span class="cref">identity (XJustiz-ID, Name)</span></div></div></div>`;
  $('#gitlog').innerHTML=html;
  document.querySelectorAll('#gitlog .commit').forEach(row=>{
    const rail=row.querySelector('.rail'),col=row.dataset.col;
    const cv=document.createElement('canvas');rail.appendChild(cv);
    const w=rail.clientWidth,hh=row.clientHeight,dpr=devicePixelRatio||1;
    if(!w||!hh)return;cv.width=w*dpr;cv.height=hh*dpr;const x=cv.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);
    const cx=w/2,cy=15,merge=row.dataset.merge;
    x.strokeStyle=col;x.globalAlpha=.5;x.lineWidth=2;x.beginPath();x.moveTo(cx,0);x.lineTo(cx,hh);x.stroke();x.globalAlpha=1;
    x.beginPath();x.arc(cx,cy,merge?6:5,0,7);x.fillStyle=merge?'#4ec9a5':col;x.fill();
  });
}

/* ---- Hierarchie: Bund → Länder → Kreise ---- */
let HIER=null;
async function renderHier(){
  if(!HIER)HIER=await api('hierarchy');
  const h=HIER,loc=LANG==='de'?'de':'en';
  const mx=Math.max(...h.lands.map(l=>l.authorities));
  const g=(label,count,inner,open,cls='')=>`<details ${open?'open':''} class="${cls}">
    <summary><span class="tw">▸</span><b>${label}</b> <span class="cnt">${count}</span></summary>
    <div class="grp">${inner}</div></details>`;
  const landInner=h.lands.map(l=>{
    const w=Math.round(l.authorities/mx*100);
    return `<details data-land="${l.code}"><summary><span class="tw">▸</span>
      ${esc(l.name)} <span class="cnt">${l.kreise} ${tr('hier_kreise')} · ${l.gemeinden.toLocaleString(loc)} ${tr('hier_gem')} · ${l.authorities.toLocaleString(loc)} ${tr('hier_beh')}</span>
      <span class="bar"><i style="width:${w}%"></i></span></summary>
      <div class="grp"><div class="leaf hint" data-hland="${l.code}">${tr('hier_load')}</div></div></details>`;
  }).join('');
  $('#hier').innerHTML=`<div class="tree" style="max-width:1050px">
    <h2 style="margin:0 0 10px">${tr('hier_title')}</h2>
    ${g(tr('hier_root'),h.total_authorities.toLocaleString(loc)+' '+tr('hier_sub'),landInner,true,'lvl1')}</div>`;
  document.querySelectorAll('#hier details[data-land]').forEach(d=>d.addEventListener('toggle',async()=>{
    if(!d.open||d.dataset.loaded)return;d.dataset.loaded=1;
    const ks=await api('kreise?land='+d.dataset.land);
    d.querySelector('.grp').innerHTML=ks.map(k=>
      `<div class="leaf">${esc(k.name)} <span class="d">${esc(k.kind)}${k.regierungsbezirk?' · '+esc(k.regierungsbezirk):''} · ${k.gemeinden} ${tr('hier_gem')}</span></div>`).join('');
  }));
  hierLoaded=true;
}
document.addEventListener('click',async e=>{
  const n=e.target.closest('.node, .sr');if(!n)return;
  e.stopPropagation();
  if(n.dataset.land&&!n.dataset.loaded){
    const kids=n.querySelector('.kids');
    const ks=await api('kreise?land='+n.dataset.land);
    kids.innerHTML=ks.map(k=>`<div class="node" data-kreis="${k.ags}">▸ ${esc(k.name)} <span class="n">${esc(k.kind)}${k.regierungsbezirk?' · '+esc(k.regierungsbezirk):''} · ${k.gemeinden}</span><div class="kids" hidden></div></div>`).join('');
    n.dataset.loaded=1;kids.hidden=false;
  } else if(n.dataset.land){const k=n.querySelector('.kids');k.hidden=!k.hidden;}
  if(n.dataset.kreis&&!n.dataset.loaded){
    const kids=n.querySelector('.kids');
    const gs=await api('gemeinden?kreis='+n.dataset.kreis);
    kids.innerHTML=gs.map(g=>`<div class="node" data-ags="${g.ags}">· ${esc(g.name)} <span class="n">${esc(g.kind||'')}</span></div>`).join('');
    n.dataset.loaded=1;kids.hidden=false;
  } else if(n.dataset.kreis){const k=n.querySelector('.kids');k.hidden=!k.hidden;}
  if(n.dataset.ags) showGemeinde(n.dataset.ags);
  if(n.dataset.plz) showCourtPicker(n.dataset.plz,n.dataset.ortk,n.dataset.ort);
});
async function showGemeinde(ags){
  const d=await api('gemeinde?ags='+ags);
  const g=d.gemeinde;
  let h=`<h2>${esc(g.name_simple)} <span class="kbadge">· ${esc(d.gemeinde.kreis_name)} · ${esc(g.land_name)}</span></h2>
  <p class="meta hint">AGS ${g.ags} · ARS ${g.ars} · PLZ: ${d.plz.join(', ')||'—'}</p>`;
  for(const c of d.caveats) h+=`<div class="caveat">⚠ ${esc(c.text_de)}</div>`;
  if(d.jz_places.length){
    h+=`<h2>Zuständiges Gericht finden</h2><p>`;
    h+=d.jz_places.map(j=>`<span class="sr chip" data-plz="${j.plz}" data-ortk="${j.ortk}" data-ort="${esc(j.ort)}">${j.plz} ${esc(j.ort)}</span>`).join(' ');
    h+=`</p><div id="court"></div>`;
  }
  h+=`<h2>Behörden (${d.authorities.length})</h2>`;
  for(const a of d.authorities){
    h+=`<div class="card" ${a.rank>0?'style="opacity:.7"':''}><h3>${esc(a.name)} ${a.legal_form?`<span class="chip lf">${esc(a.legal_form)}</span>`:''}${a.rank>0?'<span class="chip" style="background:#3a2e12;color:var(--warn)">übergeordnet / Aufsicht</span>':''} <span class="chip sr" data-graph="${a.id}" style="background:#12313a;color:var(--by);cursor:pointer">⚛ web</span></h3>
    <div>${(a.competence_kinds||[]).map(k=>`<span class="chip">${esc(k)}</span>`).join('')}<span class="chip" style="background:#2c2447;color:var(--eu)">${esc(a.level)}</span></div>
    <div class="meta">${esc([a.street,a.plz,a.city].filter(Boolean).join(', '))}
    ${a.phone?` · ☎ ${esc(a.phone)}`:''}${a.email?` · ✉ <a href="mailto:${esc(a.email)}">${esc(a.email)}</a>`:''}
    ${a.web?` · <a href="${esc(a.web)}" target="_blank">web</a>`:''}</div></div>`;
  }
  $('#detail').innerHTML=h;
}
function showCourtPicker(plz,ortk,ort){
  const sel=`<select id="matter">${MATTERS.map(m=>`<option value="${m.code}" ${m.code==='zivil'?'selected':''}>${esc(m.label_de)}${m.core?'':' *'}</option>`).join('')}</select>`;
  const div=$('#court')||$('#detail');
  div.innerHTML=`<div class="card"><h3>⚖ ${plz} ${esc(ort)}</h3><p>Angelegenheit: ${sel} <button id="go">anzeigen</button></p><div id="chain"></div></div>`;
  $('#go').onclick=async()=>{
    const m=$('#matter').value;
    const r=await api(`court?plz=${plz}&ortk=${encodeURIComponent(ortk)}&matter=${m}`);
    let h='';
    for(const c of r.caveats) h+=`<div class="caveat">⚠ ${esc(c.text_de)}</div>`;
    if(!r.chain.length) h+=`<p class="hint">Kein Eintrag im amtlichen Verzeichnis für diese Angelegenheit.</p>`;
    for(const c of r.chain){
      h+=`<div class="card ${c.role==='prosecution'?'role-pros':''}">
      <h3><span class="pos">${c.role==='prosecution'?'StA':c.position+'.'}</span> ${esc(c.name)}
      ${c.xjustiz_id?`<span class="chip">XJustiz ${esc(c.xjustiz_id)}</span>`:''}${c.note?`<span class="chip" style="background:#3a2e12;color:var(--warn)">${esc(c.note)}</span>`:''}</h3>
      <div class="meta">📍 ${esc(c.address||'—')}${c.postal_address&&c.postal_address!==c.address?` · ✉ Post: ${esc(c.postal_address)}`:''}<br>
      ${c.phone?`☎ ${esc(c.phone)} `:''}${c.fax?` · Fax ${esc(c.fax)}`:''}${c.web?` · <a href="${esc(c.web)}" target="_blank">web</a>`:''}<br>
      ${c.erv_note?`<span style="color:var(--grn)">⇄ ${esc(c.erv_note)}</span>`:''}</div></div>`;
    }
    $('#chain').innerHTML=h;
  };
  $('#go').click();
}
let t;$('#q').addEventListener('input',e=>{clearTimeout(t);t=setTimeout(async()=>{
  const q=e.target.value.trim();if(q.length<2)return;
  const r=await api('search?q='+encodeURIComponent(q));
  let h=`<h2>Suche: „${esc(q)}“</h2>`;
  if(r.plz.length){h+='<h3>Orte nach PLZ</h3>'+r.plz.map(p=>`<div class="sr" data-plz="${p.plz}" data-ortk="${p.ortk}" data-ort="${esc(p.ort)}">⚖ ${p.plz} ${esc(p.ort)} <span class="ctx">→ Gericht finden</span></div>`).join('');}
  if(r.gemeinden.length){h+='<h3>Gemeinden</h3>'+r.gemeinden.map(g=>`<div class="sr" data-ags="${g.ags}">🏘 ${esc(g.name)} <span class="ctx">${esc(g.kreis)} · ${esc(g.land)}</span></div>`).join('');}
  if(r.authorities.length){h+='<h3>Behörden</h3>'+r.authorities.map(a=>`<div class="sr"><span class="chip">${esc(a.kind)}</span> ${esc(a.name)} <span class="ctx">${esc(a.city||'')}</span></div>`).join('');}
  $('#detail').innerHTML=h;},250);});

/* ---------------- QFS-style graph view (zero-dep canvas force sim) ------ */
const G={nodes:new Map(),edges:new Map(),sel:null,path:new Set(),drag:null,pan:{x:0,y:0,k:1}};
const RELSTYLE={appeal:{c:'#9aa7ff',dash:[],arrow:1},supervision:{c:'#6ea8fe',dash:[2,3],arrow:1},
                parent:{c:'#e0b341',dash:[6,4],arrow:0},successor:{c:'#e0645a',dash:[],arrow:1}};
const trustColor=t=>t>=.92?'#5fd0e0':t>=.85?'#6ea8fe':t>=.72?'#e0b341':'#8b97a8';
const kindColor=k=>{let h=0;for(const c of k)h=(h*31+c.charCodeAt(0))%360;return`hsl(${h} 45% 55%)`};
function gAdd(d){
  const n=d.node;
  if(!G.nodes.has(n.id))G.nodes.set(n.id,{...n,x:(Math.random()-.5)*80,y:(Math.random()-.5)*80,vx:0,vy:0,expanded:false});
  const me=G.nodes.get(n.id);me.expanded=true;
  for(const e of d.edges){
    if(!G.nodes.has(e.id))G.nodes.set(e.id,{id:e.id,kind:e.kind,name:e.name,trust:e.node_trust,
      x:me.x+(Math.random()-.5)*120,y:me.y+(Math.random()-.5)*120,vx:0,vy:0,expanded:false});
    const key=`${e.src}|${e.dst}|${e.relation}|${e.matter||''}`;
    G.edges.set(key,{src:e.src,dst:e.dst,relation:e.relation,delta:e.delta,trust:e.trust});
  }
}
async function gExpand(id){const d=await api('graph/node?id='+id);if(!d.error)gAdd(d);gInspect(id);}
function gInspect(id){const n=G.nodes.get(id);if(!n)return;
  const es=[...G.edges.values()].filter(e=>e.src===id||e.dst===id);
  $('#ginfo').innerHTML=`<div class="card"><h3>${esc(n.name)}</h3>
   <div><span class="chip">${esc(n.kind)}</span>${n.legal_form?`<span class="chip lf">${esc(n.legal_form)}</span>`:''}
   <span class="chip" style="color:${trustColor(n.trust||.6)}">trust ${(n.trust||.6).toFixed(2)}${n.source?' · '+esc(n.source):''}</span></div>
   <div class="meta">${es.length} edges · ${n.expanded?'expanded':'click to expand'}</div></div>`;}
function gPhysics(){
  const ns=[...G.nodes.values()];
  for(let i=0;i<ns.length;i++)for(let j=i+1;j<ns.length;j++){
    const a=ns[i],b=ns[j];let dx=b.x-a.x,dy=b.y-a.y;let d2=dx*dx+dy*dy||1;
    if(d2<90000){const f=1800/d2;const d=Math.sqrt(d2);dx/=d;dy/=d;
      a.vx-=dx*f;a.vy-=dy*f;b.vx+=dx*f;b.vy+=dy*f;}}
  for(const e of G.edges.values()){const a=G.nodes.get(e.src),b=G.nodes.get(e.dst);if(!a||!b)continue;
    let dx=b.x-a.x,dy=b.y-a.y;const d=Math.sqrt(dx*dx+dy*dy)||1;
    const f=(d-110)*.02*(.4+.6*e.trust);dx/=d;dy/=d;   /* trusted edges pull harder */
    a.vx+=dx*f;a.vy+=dy*f;b.vx-=dx*f;b.vy-=dy*f;}
  for(const n of ns){if(G.drag&&G.drag.id===n.id)continue;
    if(n.pin){n.x=n.gx;n.y=n.gy;n.vx=n.vy=0;continue;}   /* Länder stay put */
    n.vx*=.85;n.vy*=.85;n.x+=n.vx;n.y+=n.vy;}
}
function gDraw(){
  const cv=$('#gc'),ctx=cv.getContext('2d');
  cv.width=cv.clientWidth;cv.height=cv.clientHeight;
  ctx.save();ctx.translate(cv.width/2+G.pan.x,cv.height/2+G.pan.y);ctx.scale(G.pan.k,G.pan.k);
  for(const[key,e]of G.edges){
    if(!$('#l-'+e.relation)?.checked&&RELSTYLE[e.relation])continue;
    const a=G.nodes.get(e.src),b=G.nodes.get(e.dst);if(!a||!b)continue;
    const st=RELSTYLE[e.relation]||RELSTYLE.appeal;
    const onPath=G.path.has(e.src)&&G.path.has(e.dst);
    ctx.strokeStyle=onPath?'#4ec9a5':st.c;ctx.globalAlpha=onPath?1:.35+.5*e.trust;
    ctx.lineWidth=onPath?2.5:1+e.trust;ctx.setLineDash(st.dash);
    ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();ctx.setLineDash([]);
    if(st.arrow||e.delta>=.9){ /* arrowhead sized by delta: directionality */
      const ang=Math.atan2(b.y-a.y,b.x-a.x),mx=b.x-14*Math.cos(ang),my=b.y-14*Math.sin(ang),s=3+4*e.delta;
      ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();
      ctx.moveTo(mx+s*Math.cos(ang),my+s*Math.sin(ang));
      ctx.lineTo(mx+s*Math.cos(ang+2.5),my+s*Math.sin(ang+2.5));
      ctx.lineTo(mx+s*Math.cos(ang-2.5),my+s*Math.sin(ang-2.5));ctx.fill();}
    ctx.globalAlpha=1;}
  for(const n of G.nodes.values()){
    if(n.land!==undefined){                 /* geographic Land / Bund node */
      const r=n.rG||12;
      ctx.beginPath();ctx.arc(n.x,n.y,r,0,7);
      ctx.fillStyle=n.land==='00'?'#6ea8fe':'#5fd0e0';
      ctx.globalAlpha=.85;ctx.fill();ctx.globalAlpha=1;
      ctx.lineWidth=2;ctx.strokeStyle=n.id===G.hoverGeo?'#fff':'#0d1017';ctx.stroke();
      ctx.fillStyle='#e6ebf2';ctx.font='600 12px -apple-system,sans-serif';
      ctx.fillText(n.name,n.x+r+5,n.y-1);
      ctx.fillStyle='#8b97a8';ctx.font='10px ui-monospace,monospace';
      ctx.fillText(n.cnt.toLocaleString('de')+(n.land==='00'?'':' Behörden'),n.x+r+5,n.y+11);
      continue;}
    const r=n.expanded?9:6;
    ctx.beginPath();ctx.arc(n.x,n.y,r,0,7);ctx.fillStyle=kindColor(n.kind||'x');ctx.fill();
    ctx.lineWidth=2.2;ctx.strokeStyle=G.path.has(n.id)?'#4ec9a5':trustColor(n.trust||.6);ctx.stroke();
    if(n.id===G.sel){ctx.beginPath();ctx.arc(n.x,n.y,r+4,0,7);ctx.strokeStyle='#fff';ctx.lineWidth=1;ctx.stroke();}
    if(G.pan.k>.55){ctx.fillStyle='#cbd3e0';ctx.font='10px ui-monospace,monospace';
      ctx.fillText(n.name.slice(0,34),n.x+r+3,n.y+3);}}
  ctx.restore();
}
let gTimer=null;
function gResize(){const cv=$('#gc');if(cv.clientWidth){cv.width=cv.clientWidth;cv.height=cv.clientHeight;}
  if(!gTimer)gTimer=setInterval(()=>{gPhysics();gDraw();},33);}
/* geographic overview — Länder placed like the map of Germany (ported
   from the production GermanyGraph unit coords), sized by authority count */
const GEO={'01':[0.42,-0.95],'02':[0.50,-0.70],'03':[0.22,-0.38],'04':[0.22,-0.60],
  '05':[-0.25,-0.12],'06':[0.10,0.12],'07':[-0.24,0.38],'08':[0.14,0.80],
  '09':[0.70,0.74],'10':[-0.30,0.64],'11':[1.10,-0.28],'12':[1.02,-0.40],
  '13':[0.95,-0.82],'14':[0.98,0.05],'15':[0.72,-0.26],'16':[0.52,0.08],'00':[-0.62,-0.78]};
const GEO_SCALE=340;
function geoOverview(){
  if(!HIER){seedGraph();return;}
  G.mode='geo';G.nodes.clear();G.edges.clear();G.path.clear();G.sel=null;
  G.pan={x:0,y:0,k:1};graphSeeded=true;
  const mx=Math.max(...HIER.lands.map(l=>l.authorities));
  const bund={id:-900,land:'00',name:'Bund',cnt:HIER.total_authorities,
    gx:GEO['00'][0]*GEO_SCALE,gy:GEO['00'][1]*GEO_SCALE,rG:16,pin:true,vx:0,vy:0};
  bund.x=bund.gx;bund.y=bund.gy;G.nodes.set(-900,bund);
  for(const l of HIER.lands){const g=GEO[l.code]||[0,0];
    const n={id:-(+l.code),land:l.code,name:l.name,cnt:l.authorities,
      gx:g[0]*GEO_SCALE,gy:g[1]*GEO_SCALE,rG:8+Math.sqrt(l.authorities/mx)*16,
      pin:true,vx:0,vy:0};n.x=n.gx;n.y=n.gy;G.nodes.set(n.id,n);
    G.edges.set('bund|'+l.code,{src:-900,dst:-(+l.code),relation:'parent',delta:.45,trust:.9});}
  $('#gstatus').textContent=tr('geo_hint');
  gResize();
}
async function loadLand(code){
  G.mode='web';$('#gstatus').textContent='…';
  const s=await api('seed?land='+code);
  if(s&&s.length){gSeed(s[0].id);
    $('#gstatus').textContent=`${(HIER&&HIER.lands.find(l=>l.code===code)||{}).name||code}: ${esc(s[0].name)}`;}
  else{$('#gstatus').textContent=tr('g_none');geoOverview();}
}
function gSeed(id){G.mode='web';G.nodes.clear();G.edges.clear();G.path.clear();G.sel=null;
  G.pan={x:0,y:0,k:1};graphSeeded=true;gExpand(+id);gResize();}
async function seedGraph(){const s=await api('seed');if(s&&s.length){gSeed(s[0].id);
  $('#gstatus').textContent=`${tr('g_seed')}: ${esc(s[0].name)} (° ${s[0].deg}) · ${tr('g_expand')}`;}}
let gst;$('#gsearch').addEventListener('input',e=>{clearTimeout(gst);gst=setTimeout(async()=>{
  const q=e.target.value.trim();if(q.length<2)return;
  const r=await api('search?q='+encodeURIComponent(q));
  if(r.authorities&&r.authorities.length){gSeed(r.authorities[0].id);
    $('#gstatus').textContent=`${tr('g_loaded')}: ${esc(r.authorities[0].name)}`;}
  else $('#gstatus').textContent=tr('g_none');},300);});
$('#geohome').onclick=()=>geoOverview();
const cvEl=document.getElementById('gc');
function gHit(ev){const cv=cvEl,r=cv.getBoundingClientRect();
  const x=(ev.clientX-r.left-cv.width/2-G.pan.x)/G.pan.k,y=(ev.clientY-r.top-cv.height/2-G.pan.y)/G.pan.k;
  for(const n of G.nodes.values()){const rad=n.rG?n.rG+4:14;
    if((n.x-x)**2+(n.y-y)**2<rad*rad)return n;}return null;}
cvEl.addEventListener('mousedown',ev=>{const n=gHit(ev);
  if(n)G.drag={id:n.id,move:false};else G.drag={pan:true,px:ev.clientX,py:ev.clientY,move:false};});
cvEl.addEventListener('mousemove',ev=>{if(!G.drag)return;G.drag.move=true;
  if(G.drag.pan){G.pan.x+=ev.movementX;G.pan.y+=ev.movementY;}
  else{const n=G.nodes.get(G.drag.id);const cv=cvEl;
    n.x+=(ev.movementX)/G.pan.k;n.y+=(ev.movementY)/G.pan.k;n.vx=n.vy=0;}});
cvEl.addEventListener('mouseup',async ev=>{const drag=G.drag;G.drag=null;
  if(drag&&!drag.move&&!drag.pan){const n=gHit(ev);if(!n)return;
    if(n.land!==undefined){                 /* geographic node clicked */
      if(n.land==='00')geoOverview();else loadLand(n.land);return;}
    if(ev.shiftKey&&G.sel&&G.sel!==n.id){ /* traverse: most trusted path */
      $('#gstatus').textContent='searching most-trusted path…';
      const t=await api(`graph/traverse?src=${G.sel}&dst=${n.id}`);
      G.path.clear();
      if(t.found){G.path.add(G.sel);for(const h of t.hops)G.path.add(h.to);
        $('#gstatus').textContent=`path cost ${t.total_cost} (lower = more trusted): `+
          [t.start,...t.hops.map(h=>`${h.dir==='<-'?'⇠':'⇢'}[${h.relation}] ${h.name}`)].join(' ').slice(0,180);
      } else $('#gstatus').textContent='no path in the web';
    } else {G.sel=n.id;G.path.clear();$('#gstatus').textContent='';gExpand(n.id);}}});
cvEl.addEventListener('wheel',ev=>{ev.preventDefault();
  G.pan.k=Math.min(3,Math.max(.2,G.pan.k*(ev.deltaY<0?1.1:0.9)));},{passive:false});
document.addEventListener('click',e=>{const g=e.target.closest('[data-graph]');
  if(g){e.stopPropagation();showTab('graph');gSeed(+g.dataset.graph);}},true);
window.addEventListener('resize',()=>{if($('#graph').classList.contains('on'))gResize();});

init();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                  # quiet
        pass

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            elif u.path == "/sntiq.svg":
                body, ctype = SNTIQ_SVG, "image/svg+xml"
            else:
                route = u.path.removeprefix("/api/")
                fn = {
                    "lands": lambda: api_lands(),
                    "kreise": lambda: api_kreise(qs["land"]),
                    "gemeinden": lambda: api_gemeinden(qs["kreis"]),
                    "gemeinde": lambda: api_gemeinde(qs["ags"]),
                    "search": lambda: api_search(qs["q"]),
                    "matters": lambda: api_matters(),
                    "court": lambda: api_court(qs["plz"], qs["ortk"],
                                               qs["matter"]),
                    "stats": lambda: api_stats(),
                    "provenance": lambda: api_provenance(),
                    "hierarchy": lambda: api_hierarchy(),
                    "seed": lambda: api_seed(qs.get("land")),
                    "graph/node": lambda: api_graph_node(qs["id"]),
                    "graph/traverse": lambda: api_graph_traverse(
                        qs["src"], qs["dst"]),
                }.get(route)
                if not fn:
                    self.send_error(404)
                    return
                body = json.dumps(fn(), ensure_ascii=False).encode()
                ctype = "application/json; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:                # noqa: BLE001
            self.send_error(500, str(exc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8400)
    args = ap.parse_args()
    print(f"Amtsgraph browser → http://127.0.0.1:{args.port}/")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
