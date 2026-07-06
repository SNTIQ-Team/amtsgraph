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


# ------------------------------------------------------------------ HTML

PAGE = r"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<title>Amtsgraph — data browser</title>
<style>
:root{--bg:#10141a;--panel:#171c24;--fg:#cdd6e4;--dim:#6b7689;--acc:#4fb4ff;
--grn:#5fd38d;--yel:#e8c468;--red:#e87979;--mono:'JetBrains Mono',ui-monospace,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 var(--mono);display:flex;flex-direction:column;height:100vh}
header{padding:10px 16px;background:var(--panel);display:flex;gap:14px;align-items:center;
border-bottom:1px solid #232a36}
header h1{font-size:15px;margin:0;color:var(--acc)}#stats{color:var(--dim);font-size:12px}
#q{flex:1;max-width:480px;background:#0c0f14;border:1px solid #2a3242;color:var(--fg);
padding:7px 12px;border-radius:6px;font:inherit;outline:none}
#q:focus{border-color:var(--acc)}
main{flex:1;display:flex;min-height:0}
#tree{width:380px;overflow:auto;padding:10px;border-right:1px solid #232a36}
#detail{flex:1;overflow:auto;padding:18px 24px}
.node{cursor:pointer;padding:2px 6px;border-radius:4px;white-space:nowrap}
.node:hover{background:#1f2733}.node .n{color:var(--dim);font-size:11px}
.kids{margin-left:18px;border-left:1px dotted #2a3242;padding-left:6px}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;
background:#223047;color:var(--acc);margin:1px 3px 1px 0}
.chip.lf{background:#1d3326;color:var(--grn)}
.card{background:var(--panel);border:1px solid #232a36;border-radius:8px;
padding:10px 14px;margin:8px 0}
.card h3{margin:0 0 4px;font-size:14px;color:var(--fg)}
.card .meta{color:var(--dim);font-size:12px}
.card a{color:var(--acc);text-decoration:none}
.caveat{border-left:3px solid var(--yel);background:#1e1c14;padding:8px 12px;
margin:8px 0;font-size:12px;border-radius:0 6px 6px 0}
.role-pros{opacity:.75}
.pos{color:var(--yel);font-weight:bold}
h2{font-size:16px;border-bottom:1px solid #232a36;padding-bottom:6px}
select{background:#0c0f14;color:var(--fg);border:1px solid #2a3242;
border-radius:6px;padding:5px 8px;font:inherit}
.hint{color:var(--dim);font-size:12px}
.sr{padding:4px 8px;cursor:pointer;border-radius:4px}.sr:hover{background:#1f2733}
.sr .ctx{color:var(--dim);font-size:11px}
.kbadge{color:var(--dim)}
</style></head><body>
<header><h1>⚖ Amtsgraph</h1>
<input id="q" placeholder="Suche: Stadt, PLZ oder Behörde… (z.B. Nördlingen, 12555, Jobcenter Kiel)">
<span id="stats"></span></header>
<main><div id="tree"></div><div id="detail"><p class="hint">Wähle links Land → Kreis → Gemeinde, oder nutze die Suche.</p></div></main>
<div id="gov" style="display:none;position:fixed;inset:0;background:rgba(7,10,15,.96);z-index:50;flex-direction:column">
 <div style="display:flex;gap:10px;align-items:center;padding:8px 14px;background:var(--panel);border-bottom:1px solid #232a36">
  <b style="color:var(--acc)">⚛ Organisational web</b>
  <span class="hint">click = expand · drag = pin · shift-click two nodes = most-trusted path</span>
  <label class="chip"><input type="checkbox" id="l-parent" checked> parent δ=0.45</label>
  <label class="chip"><input type="checkbox" id="l-supervision" checked> supervision δ=0.7</label>
  <label class="chip"><input type="checkbox" id="l-appeal" checked> appeal δ=1.0</label>
  <span id="gstatus" class="hint"></span>
  <button id="gclose" style="margin-left:auto">✕ close</button>
 </div>
 <canvas id="gc" style="flex:1;cursor:grab"></canvas>
 <div id="ginfo" style="position:absolute;right:12px;top:52px;width:340px;max-height:70vh;overflow:auto"></div>
</div>
<script>
const $=s=>document.querySelector(s);
const api=async p=>(await fetch('/api/'+p)).json();
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let MATTERS=[];

async function init(){
  const st=await api('stats');
  $('#stats').textContent=`${st.authorities} Behörden · ${st.places} Orte · ${st.chain_links} Instanz-Links · Stand ${st.built_at.slice(0,10)}`;
  MATTERS=await api('matters');
  const lands=await api('lands');
  $('#tree').innerHTML=lands.map(l=>
    `<div class="node" data-land="${l.code}">▸ ${esc(l.name)} <span class="n">${l.kreise} Kreise</span><div class="kids" hidden></div></div>`).join('');
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
    h+=`<div class="card" ${a.rank>0?'style="opacity:.7"':''}><h3>${esc(a.name)} ${a.legal_form?`<span class="chip lf">${esc(a.legal_form)}</span>`:''}${a.rank>0?'<span class="chip" style="background:#3a2a20;color:#ffb38a">übergeordnet / Aufsicht</span>':''} <span class="chip sr" data-graph="${a.id}" style="background:#1d2a3a;cursor:pointer">⚛ web</span></h3>
    <div>${(a.competence_kinds||[]).map(k=>`<span class="chip">${esc(k)}</span>`).join('')}<span class="chip" style="background:#2a2333;color:#c9a6ff">${esc(a.level)}</span></div>
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
      ${c.xjustiz_id?`<span class="chip">XJustiz ${esc(c.xjustiz_id)}</span>`:''}${c.note?`<span class="chip" style="background:#332520;color:#ffb38a">${esc(c.note)}</span>`:''}</h3>
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
const RELSTYLE={appeal:{c:'#9aa7b8',dash:[],arrow:1},supervision:{c:'#6f9dff',dash:[2,3],arrow:1},
                parent:{c:'#e8c468',dash:[6,4],arrow:0},successor:{c:'#e87979',dash:[],arrow:1}};
const trustColor=t=>t>=.92?'#22d3ee':t>=.85?'#4fb4ff':t>=.72?'#e8c468':'#6b7689';
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
    ctx.strokeStyle=onPath?'#5fd38d':st.c;ctx.globalAlpha=onPath?1:.35+.5*e.trust;
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
    const r=n.expanded?9:6;
    ctx.beginPath();ctx.arc(n.x,n.y,r,0,7);ctx.fillStyle=kindColor(n.kind||'x');ctx.fill();
    ctx.lineWidth=2.2;ctx.strokeStyle=G.path.has(n.id)?'#5fd38d':trustColor(n.trust||.6);ctx.stroke();
    if(n.id===G.sel){ctx.beginPath();ctx.arc(n.x,n.y,r+4,0,7);ctx.strokeStyle='#fff';ctx.lineWidth=1;ctx.stroke();}
    if(G.pan.k>.55){ctx.fillStyle='#cdd6e4';ctx.font='10px monospace';
      ctx.fillText(n.name.slice(0,34),n.x+r+3,n.y+3);}}
  ctx.restore();
}
let gTimer=null;
function gOpen(id){$('#gov').style.display='flex';G.nodes.clear();G.edges.clear();G.path.clear();G.sel=null;
  G.pan={x:0,y:0,k:1};gExpand(+id);
  if(!gTimer)gTimer=setInterval(()=>{gPhysics();gDraw();},33);}
$('#gclose').onclick=()=>{$('#gov').style.display='none';clearInterval(gTimer);gTimer=null;};
const cvEl=document.getElementById('gc');
function gHit(ev){const cv=cvEl,r=cv.getBoundingClientRect();
  const x=(ev.clientX-r.left-cv.width/2-G.pan.x)/G.pan.k,y=(ev.clientY-r.top-cv.height/2-G.pan.y)/G.pan.k;
  for(const n of G.nodes.values())if((n.x-x)**2+(n.y-y)**2<200)return n;return null;}
cvEl.addEventListener('mousedown',ev=>{const n=gHit(ev);
  if(n)G.drag={id:n.id,move:false};else G.drag={pan:true,px:ev.clientX,py:ev.clientY,move:false};});
cvEl.addEventListener('mousemove',ev=>{if(!G.drag)return;G.drag.move=true;
  if(G.drag.pan){G.pan.x+=ev.movementX;G.pan.y+=ev.movementY;}
  else{const n=G.nodes.get(G.drag.id);const cv=cvEl;
    n.x+=(ev.movementX)/G.pan.k;n.y+=(ev.movementY)/G.pan.k;n.vx=n.vy=0;}});
cvEl.addEventListener('mouseup',async ev=>{const drag=G.drag;G.drag=null;
  if(drag&&!drag.move&&!drag.pan){const n=gHit(ev);if(!n)return;
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
  if(g){e.stopPropagation();gOpen(g.dataset.graph);}},true);

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
