"""Build data/atlas.db from the latest snapshots + overrides, then validate.

The DB file is written to a temp path and only replaces data/atlas.db after
validate.check() passes — a broken build can never go live.

Usage:
    python build_db.py [--skip-validate] [--allow-warnings]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

import validate
from common import ROOT, latest_snapshot, normalize_name, read_jsonl

DB_PATH = ROOT / "data" / "atlas.db"
SCHEMA = ROOT / "db" / "schema.sql"
EU_OVERLAY = ROOT / "pipeline" / "eu_institutions.yaml"

KIND_BY_NAME = [  # order matters: first match wins
    ("generalstaatsanwaltschaft", "generalstaatsanwaltschaft"),
    ("staatsanwaltschaft", "staatsanwaltschaft"),
    ("oberlandesgericht", "oberlandesgericht"),
    ("oberstes landesgericht", "oberstes_landesgericht"),
    ("kammergericht", "oberlandesgericht"),       # Berlin's OLG
    ("bundesgericht", "bundesgericht"), ("bundesfinanzhof", "bundesgericht"),
    ("bundesarbeitsgericht", "bundesgericht"),
    ("bundessozialgericht", "bundesgericht"),
    ("bundesverwaltungsgericht", "bundesgericht"),
    ("bundesgerichtshof", "bundesgericht"),
    ("verfassungsgericht", "verfassungsgericht"),
    ("landessozialgericht", "landessozialgericht"),
    ("landesarbeitsgericht", "landesarbeitsgericht"),
    ("oberverwaltungsgericht", "oberverwaltungsgericht"),
    ("verwaltungsgerichtshof", "oberverwaltungsgericht"),
    ("landgericht", "landgericht"),
    ("amtsgericht", "amtsgericht"),
    ("mahngericht", "amtsgericht"),   # central Mahngerichte are AG departments
    ("ag ", "amtsgericht"),           # register abbreviates: 'AG Aschersleben - …'
    ("vollstreckungsgericht", "amtsgericht"),
    ("sozialgericht", "sozialgericht"),
    ("verwaltungsgericht", "verwaltungsgericht"),
    ("arbeitsgericht", "arbeitsgericht"),
    ("finanzgericht", "finanzgericht"),
]


def court_kind(name: str) -> str:
    low = name.lower()
    for marker, kind in KIND_BY_NAME:
        if marker in low:
            return kind
    return "justizbehoerde"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------- loaders

def load_geo(db: sqlite3.Connection, snap: Path):
    for s in read_jsonl(snap / "federal_states.jsonl"):
        db.execute("INSERT OR REPLACE INTO land VALUES (?,?)",
                   (s["key"], s["name"]))
    for d in read_jsonl(snap / "districts.jsonl"):
        db.execute("INSERT OR REPLACE INTO kreis VALUES (?,?,?,?,?)",
                   (d["key"], d["federalState"]["key"], d["name"],
                    d.get("type") or "Kreis",
                    (d.get("governmentRegion") or {}).get("name")))
    for m in read_jsonl(snap / "municipalities.jsonl"):
        ags = m["key"]
        kreis = m.get("district", {}).get("key") or ags[:5]
        ars = ags[:5] + "0000" + ags[5:]      # placeholder; real ARS from PVOG/Destatis
        db.execute(
            "INSERT OR REPLACE INTO gemeinde VALUES (?,?,?,?,?,?,?,?)",
            (ags, ars, kreis, m["name"], m["name"].split(",")[0].strip(),
             normalize_name(m["name"]), m.get("type"), None))
        # the Localities route misses some tiny Gemeinden entirely; the
        # Municipality record itself carries (at least) the main PLZ
        plz = (m.get("postalCode") or "").strip()
        if plz.isdigit() and len(plz) == 5:
            db.execute("INSERT OR IGNORE INTO gemeinde_plz VALUES (?,?)",
                       (ags, plz))
    n = 0
    for loc in read_jsonl(snap / "localities.jsonl"):
        ags = loc["municipality"]["key"]
        # city-states (Berlin 11000000, Hamburg 02000000) have no entry in
        # the Municipalities route — they ARE the state; synthesize rows
        if not db.execute("SELECT 1 FROM gemeinde WHERE ags=?", (ags,)).fetchone():
            name = loc["municipality"].get("name") or loc["name"]
            kreis = ags[:5]
            if not db.execute("SELECT 1 FROM kreis WHERE ags=?",
                              (kreis,)).fetchone():
                db.execute("INSERT INTO kreis VALUES (?,?,?,?,NULL)",
                           (kreis, ags[:2], name.split(",")[0].strip(),
                            "Kreisfreie Stadt"))
            db.execute(
                "INSERT INTO gemeinde VALUES (?,?,?,?,?,?,?,?)",
                (ags, ags[:5] + "0000" + ags[5:], kreis, name,
                 name.split(",")[0].strip(), normalize_name(name),
                 "Stadtstaat", None))
        db.execute("INSERT OR IGNORE INTO gemeinde_plz VALUES (?,?)",
                   (ags, loc["postalCode"]))
        n += 1
    # real 12-digit ARS from the Destatis Gemeindeverzeichnis (the Verband
    # digits are not derivable from the AGS)
    dsnap = latest_snapshot("destatis")
    n_ars = 0
    if dsnap and (dsnap / "gemeinden.jsonl").exists():
        for g in read_jsonl(dsnap / "gemeinden.jsonl"):
            cur = db.execute("UPDATE gemeinde SET ars=? WHERE ags=?",
                             (g["ars"], g["ags"]))
            n_ars += cur.rowcount
            if g.get("plz"):
                db.execute("INSERT OR IGNORE INTO gemeinde_plz "
                           "SELECT ?, ? WHERE EXISTS "
                           "(SELECT 1 FROM gemeinde WHERE ags=?)",
                           (g["ags"], g["plz"], g["ags"]))
    db.execute("INSERT INTO place_fts(place_fts) VALUES ('rebuild')")
    print(f"geo: {n} locality rows, {n_ars} real ARS from Destatis")


def load_justiz(db: sqlite3.Connection, snap: Path):
    if not list(snap.glob("pchains*.jsonl")):
        print("justiz: snapshot incomplete (no pchains*.jsonl) — skipped")
        return
    src_url = "https://www.justizadressen.nrw.de/de/justiz/gericht"
    # places
    plz_seen: dict[str, str] = {}
    for p in read_jsonl(snap / "places.jsonl"):
        ort_norm = normalize_name(p["ort"])
        ags = None
        row = db.execute(
            """SELECT g.ags FROM gemeinde g JOIN gemeinde_plz gp ON gp.ags=g.ags
               WHERE gp.plz=? AND g.name_norm=?""",
            (p["plz"], ort_norm)).fetchone()
        if row:
            ags = row[0]
        db.execute("INSERT OR REPLACE INTO jz_place VALUES (?,?,?,?,?,?,?)",
                   (p["plz"], p["ortk"], p["ort"], ort_norm,
                    p["gs_key"], p.get("gebm"), ags))
        plz_seen[p["plz"]] = p["gs_key"]

    # chains -> authorities + court_chain
    auth_by_xj: dict[str, int] = {}
    auth_by_name: dict[str, int] = {}

    def upsert_authority(card: dict) -> int:
        # identity = (XJustiz-ID, name): departments share the parent court's
        # ID but differ in name and filing address — never merge them
        xj = card.get("xjustiz_id")
        key = (xj, card["name"])
        if xj and key in auth_by_xj:
            return auth_by_xj[key]
        if not xj and card["name"] in auth_by_name:
            return auth_by_name[card["name"]]
        cur = db.execute(
            """INSERT INTO authority (kind,name,name_norm,street,plz,city,
                 postal_address,phone,fax,email,web,erv_note,
                 source,source_url,fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (court_kind(card["name"]), card["name"],
             normalize_name(card["name"]),
             card.get("address"), None, None, card.get("postal_address"),
             card.get("phone"), card.get("fax"), card.get("email"),
             card.get("web"), card.get("erv_note"),
             "justizadressen", src_url, now()))
        aid = cur.lastrowid
        if xj:
            auth_by_xj[key] = aid
            db.execute("INSERT OR IGNORE INTO authority_external_id VALUES (?,?,?)",
                       (aid, "xjustiz", xj))
        else:
            auth_by_name[card["name"]] = aid
        return aid

    n_chain = 0
    for c in _read_glob(snap, "pchains*.jsonl"):
        if not c["cards"]:
            continue
        pos = {"court": 0, "prosecution": 0}
        for card in c["cards"]:
            aid = upsert_authority(card)
            pos[card["role"]] += 1
            db.execute("INSERT OR REPLACE INTO court_chain VALUES (?,?,?,?,?,?,?)",
                       (c["plz"], c["ortk"], c["matter"], pos[card["role"]],
                        aid, card["role"], card.get("note")))
            n_chain += 1

    # derived appeal edges (court role only), cross-checked by validate
    for (plz, ortk, matter), rows in _group_chains(db):
        for lower, upper in zip(rows, rows[1:]):
            db.execute(
                       """INSERT OR IGNORE INTO authority_edge
                          (from_authority,to_authority,relation,matter,note,
                           delta,trust,source,source_url)
                          VALUES (?,?,?,?,?,1.0,0.95,
                                  'justizadressen',?)""",
                       (lower, upper, "appeal", matter,
                        f"derived:{plz}|{ortk}", src_url))

    # any (place x core matter) the register answered empty even on retry is
    # a real data gap: cover it with an explicit caveat so the API can say
    # "the official register has no entry" instead of a silent 404
    n_gap = 0
    for matter in validate.CORE_MATTERS:
        for (plz, ortk, ort) in db.execute(
                """SELECT jp.plz, jp.ortk, jp.ort FROM jz_place jp
                   WHERE NOT EXISTS (SELECT 1 FROM court_chain cc
                     WHERE cc.plz=jp.plz AND cc.ortk=jp.ortk
                       AND cc.matter=? AND cc.role='court'
                       AND cc.position=1)""", (matter,)).fetchall():
            db.execute(
                """INSERT INTO caveat (scope_level,scope_key,matter,severity,
                     text_de,source) VALUES ('jz_place',?,?,'warn',?,
                     'justizadressen')""",
                (f"{plz}|{ortk}", matter,
                 f"Das Orts- und Gerichtsverzeichnis lieferte für "
                 f"'{matter}' in {plz} {ort} kein Ergebnis — zuständiges "
                 f"Gericht bitte direkt beim Justizportal erfragen."))
            n_gap += 1
    if n_gap:
        print(f"justiz: {n_gap} (place x matter) gaps covered by caveats")

    # hinweise -> caveats (rows are keyed per place; legacy rows per gs_key)
    for h in _read_glob(snap, "hinweise*.jsonl"):
        scope = h.get("plz") or h.get("rep_plz") or h.get("gs_key")
        db.execute(
            """INSERT INTO caveat (scope_level,scope_key,matter,severity,
                 text_de,source) VALUES ('plz',?,?,'warn',?,'justizadressen')""",
            (scope, h["matter"], h["text"]))
    print(f"justiz: {n_chain} chain rows, "
          f"{len(auth_by_xj) + len(auth_by_name)} authorities")


def _group_chains(db):
    rows = db.execute(
        """SELECT plz, ortk, matter, position, authority_id FROM court_chain
           WHERE role='court' ORDER BY plz, ortk, matter, position""").fetchall()
    grouped: dict[tuple, list] = {}
    for plz, ortk, matter, _, aid in rows:
        grouped.setdefault((plz, ortk, matter), []).append(aid)
    return grouped.items()


def _read_glob(snap: Path, pattern: str):
    for f in sorted(snap.glob(pattern)):
        yield from read_jsonl(f)


def load_pvog(db: sqlite3.Connection, snap: Path):
    auth_by_oe: dict[str, int] = {}
    for u in _read_glob(snap, "units*.jsonl"):
        d = u["detail"]
        oe = u["oe_id"]
        if oe in auth_by_oe:
            continue
        loc = d.get("location") or {}
        addr = next((a for a in loc.get("addresses", [])
                     if a.get("type") == "Hausanschrift"),
                    (loc.get("addresses") or [{}])[0])
        post = next((a for a in loc.get("addresses", [])
                     if a.get("type") == "Postanschrift"), None)
        postal = (", ".join(x for x in (post.get("street"), post.get("zip"),
                                        post.get("city")) if x)
                  if post else None)
        comms = {c.get("name"): c.get("value")
                 for c in loc.get("communications", [])}
        hours = next((i.get("text") for i in d.get("additionalInformation", [])
                      if i.get("type") == "INFO_TIMES"), None)
        web = next((w.get("uri") for w in d.get("internetAddresses", [])), None)
        geo = addr.get("geo") or {}
        name = d.get("title") or d.get("name") or "?"
        cur = db.execute(
            """INSERT INTO authority (kind,name,name_norm,street,plz,city,
                 postal_address,phone,fax,email,web,hours,lat,lon,
                 source,source_url,fetched_at,source_updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("sonstige", name, normalize_name(name),
             addr.get("street"), addr.get("zip"), addr.get("city"), postal,
             comms.get("Telefon"), comms.get("Fax"), comms.get("E-Mail"), web,
             hours, geo.get("latitude"), geo.get("longitude"),
             "pvog", f"pvog:organisationunit:{oe}", now(), d.get("lastUpdate")))
        auth_by_oe[oe] = cur.lastrowid
        db.execute("INSERT OR IGNORE INTO authority_external_id VALUES (?,?,?)",
                   (cur.lastrowid, "pvog_oe", oe))

    # AsylbLG candidate cleanup: the probe's multi-collect also catches units
    # whose services merely cite the AsylbLG (Kita fee waivers, women's
    # shelters, Bildungspaket). Drop those when a benefits-office candidate
    # exists; flag genuine ambiguity only among the remaining core units.
    ASYL_CORE = re.compile(
        r"sozial|grundsicherung|asyl|flücht|migra|leistung|materielle hilfen|"
        r"wohnen|unterkunft|landratsamt|kreisverwaltung|ordnungsamt|"
        # generic city/Kreis administration names are valid first contacts
        # (PVOG often names the Gebietskörperschaft itself, e.g.
        # "Landeshauptstadt München")
        r"^landeshauptstadt|^stadt |^kreis |^gemeinde |^bezirksamt|"
        r"^magistrat|^regionalverband|^amt ", re.I)
    ASYL_NOISE = re.compile(
        r"kindertag|frühkindlich|entgelt|frauen|tagestreff|bildung|schul|"
        r"kultur|sport|senioren(?!.*asyl)|"
        # structurally never the AsylbLG *benefit* office: Jobcenter admins
        # SGB II (mutually exclusive with AsylbLG §1 by law); civil-registry
        # and vehicle desks are unrelated. Dropped only when a non-noise
        # candidate remains — the `kept = … or uniq` fallback below keeps
        # a pure-noise area's row rather than emptying it.
        r"jobcenter|job-center|\bstandesamt|gewerbeamt|zulassungsstelle|"
        r"\bkfz\b|führerschein|fahrerlaubnis|meldebeh|einwohnermeldeamt|"
        r"personalausweis|reisepass|\bwahlamt|friedhof|"
        # courts belong in court_chain, not as application offices
        r"sozialgericht|verwaltungsgericht|amtsgericht|landgericht", re.I)
    # höhere Verwaltungsebene: zuständig für Unterbringung/Aufsicht, nicht
    # für die Leistungsbeantragung vor Ort -> rank 1 + supervision edge
    ASYL_OBER = re.compile(
        r"^Regierung von|^Bezirksregierung|Landesverwaltungsamt|"
        r"^Landesamt|Ministerium|^Senatsverwaltung|"
        # Hamburg ministries are "Behörde für X", Bremen's "Senator(in) für X"
        r"^Behörde für |^Senator(in)? für ", re.I)

    by_ak: dict[tuple[str, str], list[dict]] = {}
    plain_rows: list[dict] = []
    for c in _read_glob(snap, "competences*.jsonl"):
        if c["kind"] == "asylblg_behoerde":
            by_ak.setdefault((c["ags"], c["kind"]), []).append(c)
        else:
            plain_rows.append(c)

    def asyl_filter(cands: list[dict]) -> tuple[list[dict], list[dict], bool]:
        """-> (local offices, übergeordnete Behörden, genuinely ambiguous)."""
        seen, uniq = set(), []
        for c in cands:
            if c["oe_id"] not in seen:
                seen.add(c["oe_id"])
                uniq.append(c)
        core = [c for c in uniq
                if ASYL_CORE.search(c.get("oe_title") or "")
                and not ASYL_NOISE.search(c.get("oe_title") or "")]
        kept = core or [c for c in uniq
                        if not ASYL_NOISE.search(c.get("oe_title") or "")] or uniq
        ober = [c for c in kept if ASYL_OBER.search(c.get("oe_title") or "")]
        local = [c for c in kept if c not in ober]
        if not local:                 # only higher-level known: keep as is
            local, ober = ober, []
        return local, ober, len(local) > 1

    kinds_assigned: dict[int, set] = {}
    ambiguous: dict[tuple[str, str], int] = {}
    n = 0

    def insert_comp(c: dict, rank: int = 0):
        nonlocal n
        aid = auth_by_oe.get(c["oe_id"])
        if not aid:
            return None
        db.execute(
            "INSERT OR IGNORE INTO competence VALUES (?,?,'gemeinde',?,?)",
            (aid, c["kind"], c["ags"], rank))
        kinds_assigned.setdefault(aid, set()).add(c["kind"])
        n += 1
        return aid

    supervision_edges: set[tuple[int, int]] = set()
    for c in plain_rows:
        insert_comp(c)
    for (ags, kind), cands in by_ak.items():
        local, ober, is_ambiguous = asyl_filter(cands)
        local_ids = [a for c in local if (a := insert_comp(c, rank=0))]
        ober_ids = [a for c in ober if (a := insert_comp(c, rank=1))]
        for lo in local_ids:
            for ob in ober_ids:
                supervision_edges.add((lo, ob))
        if is_ambiguous:
            ambiguous[(ags, kind)] = 1
    for lo, ob in supervision_edges:
        db.execute(
            """INSERT OR IGNORE INTO authority_edge
               (from_authority,to_authority,relation,matter,note,delta,trust,
                source,source_url)
               VALUES (?,?,'supervision',NULL,
                       'asylblg: übergeordnete Behörde',0.7,0.75,'pvog',
                       'https://pvog.fitko.net/suchdienst/api')""",
            (lo, ob))       # semi-directed upward, pvog trust
    if supervision_edges:
        print(f"pvog: {len(supervision_edges)} supervision edges (asylblg)")
    # resolution runs on competence.kind; authority.kind is just the primary
    # label — single-hat offices get it, multi-hat offices stay 'sonstige'
    for aid, ks in kinds_assigned.items():
        if len(ks) == 1:
            db.execute(
                "UPDATE authority SET kind=? WHERE id=? AND kind='sonstige'",
                (ks.pop(), aid))
    # heterogeneous competence (e.g. AsylbLG): honest caveat instead of a guess
    for (ags, kind) in ambiguous:
        db.execute(
            """INSERT INTO caveat (scope_level,scope_key,matter,severity,
                 text_de,source)
               VALUES ('gemeinde',?,NULL,'warn',?,'pvog')""",
            (ags, f"Für '{kind}' nennt der Zuständigkeitsfinder mehrere "
                  f"Stellen; die Praxis ist regional unterschiedlich — "
                  f"bitte vor Antragstellung telefonisch bestätigen."))
    # Bayern: AsylbLG competence is PHASE-dependent, which no per-place
    # table can capture. During residence in a staatliche Aufnahme-
    # einrichtung (incl. ANKER-Einrichtungen) the Regierung des Bezirks
    # grants the benefits; only after Zuweisung to a Landkreis / kreisfreie
    # Stadt does the local authority take over (DVAsyl / AsylbLG § 10 iVm
    # bayer. Zuständigkeitsrecht). This is exactly why 600 Bavarian areas
    # resolve only to a Regierung. Surface it per Kreis so the API can say
    # "which office depends on your phase", not guess one.
    n_anker = 0
    for (kreis_ags,) in db.execute(
            "SELECT ags FROM kreis WHERE land_code='09'").fetchall():
        db.execute(
            """INSERT INTO caveat (scope_level,scope_key,matter,severity,
                 text_de,source) VALUES ('kreis',?,NULL,'info',?,'sntiq')""",
            (kreis_ags,
             "AsylbLG in Bayern: Während des Aufenthalts in einer "
             "staatlichen Aufnahmeeinrichtung (z. B. ANKER-Einrichtung) "
             "gewährt die Regierung des Bezirks die Leistungen; erst nach "
             "der Zuweisung an einen Landkreis / eine kreisfreie Stadt ist "
             "die dortige Behörde zuständig (DVAsyl). Im Einzelfall — je "
             "nach Unterbringungsphase — telefonisch bestätigen."))
        n_anker += 1
    print(f"bayern: {n_anker} ANKER/Aufnahmephase AsylbLG caveats")
    # units whose every competence row was filtered out as noise (Kita fee
    # offices etc. caught by the AsylbLG multi-probe) have no place in the DB
    db.execute("""DELETE FROM authority_external_id WHERE authority_id IN (
                    SELECT a.id FROM authority a WHERE a.source='pvog'
                    AND NOT EXISTS (SELECT 1 FROM competence c
                                    WHERE c.authority_id=a.id))""")
    cur = db.execute("""DELETE FROM authority WHERE source='pvog'
                        AND NOT EXISTS (SELECT 1 FROM competence c
                                        WHERE c.authority_id=authority.id)""")
    print(f"pvog: {len(auth_by_oe)} units, {n} competence rows, "
          f"{len(ambiguous)} ambiguous (ags,kind) pairs, "
          f"{cur.rowcount} noise units dropped")


def load_ba(db: sqlite3.Connection, snap: Path):
    """SGB-II Jobcenter register: authoritative for existence + legal form.

    Enrichment respects SPLIT Kreise: where a Kreis is served by more than
    one Träger (12 such Kreise nationwide), local offices must NOT blindly
    inherit a legal form — the office may belong to either Träger. There we
    leave legal_form unset on PVOG offices, create the BA Träger records
    with kreis competence, and rely on the split caveat.
    """
    zuord: dict[str, list[dict]] = {}
    traeger_by_kreis: dict[str, set[str]] = {}
    split_kreise: set[str] = set()
    for z in read_jsonl(snap / "zuordnung.jsonl"):
        zuord.setdefault(z["traeger_nr"], []).append(z)
        traeger_by_kreis.setdefault(z["kreis_ags"], set()).add(z["traeger_nr"])
        if z["split"]:
            split_kreise.add(z["kreis_ags"])
    split_kreise |= {k for k, ts in traeger_by_kreis.items() if len(ts) > 1}

    merged = created = 0
    for t in read_jsonl(snap / "traeger.jsonl"):
        kreise = zuord.get(t["traeger_nr"], [])
        # inheritance is only safe where this Träger is the ONLY one
        sole_kreise = [z["kreis_ags"] for z in kreise
                       if z["kreis_ags"] not in split_kreise]
        rows = db.execute(f"""
            SELECT DISTINCT c.authority_id FROM competence c
            WHERE c.kind='jobcenter' AND c.level='gemeinde'
              AND substr(c.area,1,5) IN ({','.join('?' * len(sole_kreise))})
            """, sole_kreise).fetchall() if sole_kreise else []
        if rows:
            for (aid,) in rows:
                db.execute("UPDATE authority SET legal_form=? WHERE id=?",
                           (t["legal_form"], aid))
                db.execute("INSERT OR IGNORE INTO authority_external_id "
                           "VALUES (?,?,?)", (aid, "ba_traeger",
                                              t["traeger_nr"]))
            merged += len(rows)
            if len(rows) == 1:
                for k in sole_kreise:
                    db.execute("INSERT OR IGNORE INTO competence VALUES "
                               "(?,'jobcenter','kreis',?,0)", (rows[0][0], k))
        # standalone BA record when the Träger has no enrichable office OR
        # serves split Kreise (there it is the only reliable answer)
        standalone_kreise = ([z["kreis_ags"] for z in kreise]
                             if not rows else
                             [z["kreis_ags"] for z in kreise
                              if z["kreis_ags"] in split_kreise])
        if standalone_kreise:
            cur = db.execute(
                """INSERT INTO authority (kind,name,name_norm,legal_form,
                     source,source_url,fetched_at)
                   VALUES ('jobcenter',?,?,?,'ba',?,?)""",
                (t["name"], normalize_name(t["name"]), t["legal_form"],
                 "https://statistik.arbeitsagentur.de/ Gebietsstruktur "
                 "Grundsicherungstraeger", now()))
            aid = cur.lastrowid
            created += 1
            db.execute("INSERT OR IGNORE INTO authority_external_id "
                       "VALUES (?,?,?)", (aid, "ba_traeger", t["traeger_nr"]))
            for k in standalone_kreise:
                db.execute("INSERT OR IGNORE INTO competence VALUES "
                           "(?,'jobcenter','kreis',?,0)", (aid, k))

    # an office reached by SEVERAL Träger (it serves gemeinden across
    # their Kreise) may only keep a legal form all of them agree on
    traeger_form = {}
    for t in read_jsonl(snap / "traeger.jsonl"):
        traeger_form[t["traeger_nr"]] = t["legal_form"]
    conflicted = db.execute("""
        SELECT e.authority_id, GROUP_CONCAT(DISTINCT e.value)
        FROM authority_external_id e
        WHERE e.scheme='ba_traeger'
        GROUP BY e.authority_id HAVING COUNT(DISTINCT e.value) > 1""").fetchall()
    n_conflict = 0
    for aid, values in conflicted:
        forms = {traeger_form.get(v) for v in values.split(",")}
        if len(forms) > 1:
            db.execute("UPDATE authority SET legal_form=NULL WHERE id=?",
                       (aid,))
            db.execute(
                """INSERT INTO caveat (scope_level,scope_key,matter,severity,
                     text_de,source) VALUES ('authority',?,NULL,'warn',?,'ba')""",
                (str(aid),
                 "Dieses Jobcenter liegt im Gebiet mehrerer SGB-II-Träger "
                 "mit unterschiedlicher Rechtsform — Trägerschaft bitte "
                 "direkt beim Jobcenter erfragen."))
            n_conflict += 1
    if n_conflict:
        print(f"ba: {n_conflict} offices with conflicting Träger forms "
              f"— legal_form cleared + caveat")

    for z in {(z2["kreis_ags"], z2["kreis_name"])
              for zs in zuord.values() for z2 in zs
              if z2["kreis_ags"] in split_kreise}:
        db.execute(
            """INSERT INTO caveat (scope_level,scope_key,matter,
                 severity,text_de,source)
               VALUES ('kreis',?,NULL,'warn',?,'ba')""",
            (z[0], f"Der Kreis {z[1]} ist auf mehrere Jobcenterbezirke "
                   f"aufgeteilt — Zuständigkeit bitte beim Jobcenter "
                   f"bestätigen."))
    print(f"ba: jobcenter enriched {merged}, created {created}, "
          f"{len(split_kreise)} split Kreise protected")


def _match_authority(db, m: dict):
    if "external_id" in m:
        row = db.execute(
            "SELECT authority_id FROM authority_external_id "
            "WHERE scheme=? AND value=?",
            (m["external_id"]["scheme"], m["external_id"]["value"])).fetchone()
        return row and row[0]
    if "name" in m:
        row = db.execute(
            "SELECT id FROM authority WHERE name=? AND valid_to IS NULL"
            + (" AND kind=?" if m.get("kind") else ""),
            (m["name"], m["kind"]) if m.get("kind") else (m["name"],)).fetchone()
        return row and row[0]
    return None


def load_bayernportal(db: sqlite3.Connection, snap: Path):
    """Bavaria's full organisational web from the BayernPortal organigrams.

    EVERY unit (Landrat, Abteilung, Fachbereich, Sachgebiet) becomes an
    authority row, linked child->parent via authority_edge 'parent' — the
    supervisory web. Units matching a kind mapping additionally get
    kreis-level competence; when such a unit is unique for (kreis, kind) it
    REPLACES the generic Kreisverwaltungsbehörde as the rank-0 answer.
    """
    DEPT_KIND = [
        (re.compile(r"asyl", re.I), "asylblg_behoerde"),
        (re.compile(r"ausländer|staatsangehörigkeit", re.I),
         "auslaenderbehoerde"),
        (re.compile(r"jobcenter|grundsicherung für arbeit", re.I),
         "jobcenter"),
        (re.compile(r"jugendamt|jugend(hilfe)?\b|vormundschaft|beistandschaft",
                    re.I), "jugendamt"),
        (re.compile(r"sozial(e|es|amt)?\b|wirtschaftliche hilfen|"
                    r"grundsicherung(?!.*arbeit)", re.I), "sozialamt"),
        (re.compile(r"wohngeld", re.I), "wohngeldstelle"),
        (re.compile(r"gewerbe", re.I), "gewerbeamt"),
        (re.compile(r"bürgerbüro|bürgeramt|bürgerservice|einwohnerwesen",
                    re.I), "buergeramt"),
        (re.compile(r"standesamt", re.I), "standesamt"),
    ]

    # match in Python: SQL lower() folds neither ß nor umlauts, and
    # Fürth/Würzburg exist twice (Landkreis + kreisfreie Stadt)
    kreise = [(normalize_name(name), kind, ags) for ags, name, kind in
              db.execute("SELECT ags, name, kind FROM kreis "
                         "WHERE land_code='09'")]

    def kreis_for(root_name: str):
        is_stadt = (root_name.startswith(("Stadt ", "Landeshauptstadt"))
                    or "Kreisfreie Stadt" in root_name)
        base = re.sub(r"^(Landratsamt|Stadt|Landeshauptstadt)\s+", "",
                      root_name.replace(", Kreisfreie Stadt", ""))
        n = normalize_name(base)
        for kn, kk, ags in kreise:          # prefer matching kind
            if kn == n and (("tadt" in kk) == is_stadt):
                return ags
        for kn, kk, ags in kreise:
            if kn == n:
                return ags
        return None

    by_root: dict[str, list[dict]] = {}
    for u in read_jsonl(snap / "units.jsonl"):
        by_root.setdefault(u["root_id"], []).append(u)

    n_auth = n_edges = n_comp = n_replaced = 0
    unmatched_roots = []
    for units in by_root.values():
        root_name = units[0]["root_name"]
        kreis = kreis_for(root_name)
        if not kreis:
            unmatched_roots.append(root_name)
        # anchor: the existing generic authority for the root, if known
        root_aid = None
        row = db.execute(
            "SELECT id FROM authority WHERE name_norm=? AND valid_to IS NULL",
            (normalize_name(root_name.replace(", Kreisfreie Stadt", "")),)
        ).fetchone()
        if row:
            root_aid = row[0]
        else:
            # no generic record known (name drift between sources): create
            # the root node itself so the web stays connected
            cur = db.execute(
                """INSERT INTO authority (kind,name,name_norm,
                     source,source_url,fetched_at)
                   VALUES ('sonstige',?,?,'bayernportal',?,?)""",
                (root_name.replace(", Kreisfreie Stadt", ""),
                 normalize_name(root_name.replace(", Kreisfreie Stadt", "")),
                 f"https://www.bayernportal.de/dokumente/behoerde/"
                 f"{units[0]['root_id']}", now()))
            root_aid = cur.lastrowid
            n_auth += 1

        aid_by_key: dict[str, int] = {}
        kind_hits: dict[str, list[int]] = {}
        # most-specific-wins: a container (Abteilung 2 - Jugend, Familie,
        # Soziales) must not compete with its own child department
        # (Sachgebiet 24 - Kreisjugendamt). A unit only counts for a kind
        # if none of its DESCENDANTS matches the same kind.
        parent_of = {u["key"]: u.get("parent_key") for u in units}

        def ancestors(key):
            k = parent_of.get(key)
            while k:
                yield k
                k = parent_of.get(k)

        # a unit may wear SEVERAL hats ("21 Ausländer- und
        # Asylangelegenheiten" is both the ABH and the AsylbLG office)
        kinds_of: dict[str, list[str]] = {}
        for u in units:
            kinds_of[u["key"]] = [k for rx, k in DEPT_KIND
                                  if rx.search(u["name"])]
        shadowed: dict[str, set[str]] = {}      # key -> kinds shadowed by a child
        for u in units:
            for k in kinds_of[u["key"]]:
                for anc in ancestors(u["key"]):
                    if k in kinds_of.get(anc, []):
                        shadowed.setdefault(anc, set()).add(k)

        for u in units:
            ukinds = [k for k in kinds_of[u["key"]]
                      if k not in shadowed.get(u["key"], set())]
            kind = ukinds[0] if ukinds else "sonstige"
            full_name = (u["name"] if u["name"].lower().startswith(
                ("landratsamt", "stadt", "landeshauptstadt"))
                else f"{root_name} - {u['name']}")
            cur = db.execute(
                """INSERT INTO authority (kind,name,name_norm,postal_address,
                     phone,fax,email,source,source_url,fetched_at)
                   VALUES (?,?,?,?,?,?,?,'bayernportal',?,?)""",
                (kind, full_name, normalize_name(full_name),
                 u.get("postal_address"), u.get("phone"), u.get("fax"),
                 u.get("email"),
                 (f"https://www.bayernportal.de/dokumente/behoerde/"
                  f"{u['oe_id']}" if u.get("oe_id") else
                  f"https://www.bayernportal.de/dokumente/behoerde/"
                  f"{u['root_id']}/organigramm"), now()))
            aid = cur.lastrowid
            n_auth += 1
            addr = u.get("address")
            if addr:
                m = re.match(r"(.+?)\s+(\d{5})\s+(.+)", addr)
                if m:
                    db.execute("UPDATE authority SET street=?, plz=?, city=? "
                               "WHERE id=?",
                               (m.group(1), m.group(2), m.group(3), aid))
            if u.get("oe_id"):
                db.execute("INSERT OR IGNORE INTO authority_external_id "
                           "VALUES (?,?,?)",
                           (aid, "bayernportal_oe", u["oe_id"]))
            aid_by_key[u["key"]] = aid
            if kreis:
                for k in ukinds:
                    db.execute("INSERT OR IGNORE INTO competence VALUES "
                               "(?,?,'kreis',?,0)", (aid, k, kreis))
                    kind_hits.setdefault(k, []).append(aid)
                    n_comp += 1

        # parent web: child -> parent (top-level units hang on the root)
        for u in units:
            child = aid_by_key.get(u["key"])
            parent = (aid_by_key.get(u["parent_key"])
                      if u.get("parent_key") else root_aid)
            if child and parent and child != parent:
                db.execute(
                    """INSERT OR IGNORE INTO authority_edge
                       (from_authority,to_authority,relation,matter,note,
                        delta,trust,source,source_url)
                       VALUES (?,?,'parent',NULL,'bayernportal organigramm',
                               0.45,0.90,'bayernportal',?)""",
                    (child, parent,
                     (f"https://www.bayernportal.de/dokumente/behoerde/"
                      f"{u.get('root_id')}/organigramm")))
                n_edges += 1

        # unique department replaces the generic answer for its kind
        if kreis:
            for kind, ids in kind_hits.items():
                if len(ids) != 1:
                    continue
                cur = db.execute(
                    """DELETE FROM competence WHERE kind=? AND rank=0
                       AND authority_id != ? AND authority_id IN (
                         SELECT id FROM authority WHERE source='pvog')
                       AND ((level='kreis' AND area=?) OR
                            (level='gemeinde' AND substr(area,1,5)=?))""",
                    (kind, ids[0], kreis, kreis))
                n_replaced += cur.rowcount

    # cross-source dedup: a PVOG OE and a BayernPortal unit describing the
    # same real-world office (identical normalized name) must be ONE node —
    # otherwise the PVOG twin floats outside the organisational web
    n_merged = 0
    for pvog_id, bp_id in db.execute("""
            SELECT p.id, MIN(b.id) FROM authority p
            JOIN authority b ON b.name_norm = p.name_norm
                AND b.source='bayernportal' AND b.valid_to IS NULL
            WHERE p.source='pvog' AND p.valid_to IS NULL
            GROUP BY p.id""").fetchall():
        # transfer competences and edges, prefer pvog contact data where
        # the portal card was empty
        db.execute("""UPDATE OR IGNORE competence SET authority_id=?
                      WHERE authority_id=?""", (bp_id, pvog_id))
        db.execute("DELETE FROM competence WHERE authority_id=?", (pvog_id,))
        for col in ("street", "plz", "city", "phone", "fax", "email",
                    "web", "hours"):
            db.execute(f"""UPDATE authority SET {col} =
                             COALESCE({col}, (SELECT {col} FROM authority
                                              WHERE id=?))
                           WHERE id=?""", (pvog_id, bp_id))
        db.execute("""UPDATE OR IGNORE authority_edge SET from_authority=?
                      WHERE from_authority=?""", (bp_id, pvog_id))
        db.execute("""UPDATE OR IGNORE authority_edge SET to_authority=?
                      WHERE to_authority=?""", (bp_id, pvog_id))
        db.execute("DELETE FROM authority_edge WHERE from_authority=? "
                   "OR to_authority=?", (pvog_id, pvog_id))
        db.execute("INSERT OR IGNORE INTO authority_external_id "
                   "SELECT ?, scheme, value FROM authority_external_id "
                   "WHERE authority_id=?", (bp_id, pvog_id))
        db.execute("UPDATE authority SET valid_to=? WHERE id=?",
                   (now(), pvog_id))
        n_merged += 1
    if n_merged:
        print(f"bayernportal: {n_merged} cross-source duplicates merged")

    # pvog offices orphaned by replacement: retire (the web keeps
    # bayernportal units alive via parent edges)
    db.execute("""UPDATE authority SET valid_to=? WHERE source='pvog'
                  AND valid_to IS NULL
                  AND NOT EXISTS (SELECT 1 FROM competence c
                                  WHERE c.authority_id=authority.id)
                  AND NOT EXISTS (SELECT 1 FROM court_chain cc
                                  WHERE cc.authority_id=authority.id)""",
               (now(),))
    if unmatched_roots:
        print(f"bayernportal: WARN {len(unmatched_roots)} roots without "
              f"kreis match: {unmatched_roots[:4]}")
    print(f"bayernportal: {n_auth} units, {n_edges} parent edges, "
          f"{n_comp} competences, {n_replaced} generic replaced")


def load_eu_overlay(db: sqlite3.Connection,
                    path: Path = EU_OVERLAY) -> str:
    """Load the small, reviewed EU institutional overlay.

    This is intentionally not a guessed EU->Germany hierarchy.  Every edge
    joins two entities declared in the overlay, carries an official EU source,
    and names its limited legal meaning.  In particular, the loader rejects
    the generic ``supervision`` relation: only the narrowly scoped
    ``sectoral_oversight`` relation is available for the EDPS.

    Returns the overlay's verification date for build metadata.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("meta"), dict):
        raise ValueError(f"EU overlay {path} has no meta mapping")
    authorities = raw.get("authorities")
    edges = raw.get("edges")
    if not isinstance(authorities, list) or len(authorities) < 11:
        raise ValueError("EU overlay must contain the seven Article 13 "
                         "institutions, both CJEU courts and two EU bodies")
    if not isinstance(edges, list):
        raise ValueError("EU overlay edges must be a list")

    verified_at = str(raw["meta"].get("verified_at") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", verified_at):
        raise ValueError("EU overlay meta.verified_at must be YYYY-MM-DD")

    def official_url(value: object, field: str) -> str:
        url = str(value or "").strip()
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "europa.eu" or
                                             host.endswith(".europa.eu")):
            raise ValueError(f"EU overlay {field} is not an official "
                             f"europa.eu HTTPS URL: {url!r}")
        return url

    allowed_kinds = {"eu_institution", "eu_body", "eu_court"}
    allowed_relations = {"appeal", "institutional_part",
                         "political_accountability", "judicial_review",
                         "cooperation", "co_legislation",
                         "reporting_accountability", "financial_audit",
                         "maladministration_review", "sectoral_oversight"}
    aid_by_key: dict[str, int] = {}
    seen_external: set[str] = set()
    for record in authorities:
        if not isinstance(record, dict):
            raise ValueError("EU authority entry must be a mapping")
        key = str(record.get("key") or "").strip()
        ext = str(record.get("external_id") or "").strip()
        kind = str(record.get("kind") or "").strip()
        name = str(record.get("name") or "").strip()
        if not key or key in aid_by_key:
            raise ValueError(f"duplicate/empty EU authority key: {key!r}")
        if not ext or ext in seen_external:
            raise ValueError(f"duplicate/empty EU external id: {ext!r}")
        if kind not in allowed_kinds:
            raise ValueError(f"unsupported EU authority kind: {kind!r}")
        if not name:
            raise ValueError(f"EU authority {key!r} has no name")
        source_url = official_url(record.get("source_url"),
                                  f"authorities[{key}].source_url")
        web = official_url(record.get("web"), f"authorities[{key}].web")
        cur = db.execute(
            """INSERT INTO authority
               (kind,name,name_norm,city,web,source,source_url,fetched_at,
                source_updated_at)
               VALUES (?,?,?,?,?,'eu_curated',?,?,?)""",
            (kind, name, normalize_name(name), record.get("city"), web,
             source_url, verified_at, verified_at))
        aid = cur.lastrowid
        aid_by_key[key] = aid
        seen_external.add(ext)
        db.execute("INSERT INTO authority_external_id VALUES (?,?,?)",
                   (aid, "eu_official", ext))
        db.execute(
            """INSERT INTO caveat
               (scope_level,scope_key,matter,severity,text_de,source)
               VALUES ('authority',?,NULL,'info',?,'eu_curated')""",
            (str(aid),
             "EU-Kanten bilden nur die ausdrücklich bezeichnete, "
             "quellenbelegte Rechtsbeziehung ab. Sie begründen weder eine "
             "allgemeine Weisungs- oder Aufsichtskette noch eine direkte "
             "Zuständigkeit für deutsche Behörden."))
        if kind == "eu_court":
            db.execute(
                """INSERT INTO caveat
                   (scope_level,scope_key,matter,severity,text_de,source)
                   VALUES ('authority',?,NULL,'info',?,'eu_curated')""",
                (str(aid),
                 "Zugang, Zuständigkeit und Rechtsmittel zu den "
                 "Unionsgerichten richten sich nach Verträgen, Satzung und "
                 "Verfahrensordnungen; die Graphkante ist kein direkter "
                 "Einreichungsweg."))

    seen_edges: set[tuple[str, str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("EU edge entry must be a mapping")
        src = str(edge.get("from") or "")
        dst = str(edge.get("to") or "")
        rel = str(edge.get("relation") or "")
        if src not in aid_by_key or dst not in aid_by_key or src == dst:
            raise ValueError(f"invalid EU edge endpoints: {src!r}->{dst!r}")
        if rel not in allowed_relations:
            raise ValueError(f"unsupported EU edge relation: {rel!r}")
        key = (src, dst, rel)
        if key in seen_edges:
            raise ValueError(f"duplicate EU edge: {key}")
        seen_edges.add(key)
        note = " ".join(str(edge.get("note") or "").split())
        if not note:
            raise ValueError(f"EU edge {key} has no limiting note")
        if rel in {"judicial_review", "political_accountability"} and \
                "keine" not in note.lower():
            raise ValueError(f"EU edge {key} must state its non-hierarchical "
                             "limit")
        if rel == "sectoral_oversight" and \
                "keine zuständigkeit für deutsche behörden" not in note.lower():
            raise ValueError(f"EU edge {key} must exclude German authorities")
        delta = float(edge.get("delta"))
        trust = float(edge.get("trust"))
        if not 0.0 <= delta <= 1.0 or not 0.0 <= trust <= 1.0:
            raise ValueError(f"EU edge {key} has invalid delta/trust")
        if rel in {"cooperation", "co_legislation"} and delta != 0.0:
            raise ValueError(f"EU {rel} edge {key} must be symmetric")
        source_url = official_url(edge.get("source_url"),
                                  f"edges[{src}->{dst}:{rel}].source_url")
        db.execute(
            """INSERT INTO authority_edge
               (from_authority,to_authority,relation,matter,note,delta,trust,
                source,source_url)
               VALUES (?,?,?,NULL,?,?,?,'eu_curated',?)""",
            (aid_by_key[src], aid_by_key[dst], rel, note, delta, trust,
             source_url))

    print(f"eu_curated: {len(aid_by_key)} authorities, {len(seen_edges)} "
          f"evidence-backed edges (no EU->German edges)")
    return verified_at


def apply_overrides(db: sqlite3.Connection):
    """Manual corrections — the ONLY place for hand fixes. Actions:

    set (default)      match an authority, update contact fields
    add_authority      create an authority (e.g. a department PVOG doesn't
                       model) with its competence rows
    remove_competence  match an authority, drop a wrong/too-coarse
                       competence (kind [+ level/area_prefix])
    """
    odir = ROOT / "pipeline" / "overrides"
    for f in sorted(odir.glob("*.yaml")):
        o = yaml.safe_load(f.read_text())
        action = o.get("action", "set")

        if action == "add_authority":
            a = o["authority"]
            cur = db.execute(
                """INSERT INTO authority (kind,name,name_norm,street,plz,city,
                     phone,fax,email,web,hours,source,source_url,fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'override',?,?)""",
                (a["kind"], a["name"], normalize_name(a["name"]),
                 a.get("street"), a.get("plz"), a.get("city"),
                 a.get("phone"), a.get("fax"), a.get("email"), a.get("web"),
                 a.get("hours"), o.get("source"), now()))
            aid = cur.lastrowid
            for c in o.get("competences", []):
                db.execute("INSERT OR IGNORE INTO competence VALUES (?,?,?,?,?)",
                           (aid, c["kind"], c["level"], str(c["area"]),
                            c.get("rank", 0)))
            print(f"  override {f.name}: added authority {aid} "
                  f"({len(o.get('competences', []))} competences)")
            continue

        aid = _match_authority(db, o.get("match", {}))
        if not aid:
            print(f"  override {f.name}: NO MATCH — skipped")
            continue

        if action == "remove_competence":
            c = o["competence"]
            sql = "DELETE FROM competence WHERE authority_id=? AND kind=?"
            args = [aid, c["kind"]]
            if c.get("level"):
                sql += " AND level=?"
                args.append(c["level"])
            if c.get("area_prefix"):
                sql += " AND area LIKE ?||'%'"
                args.append(str(c["area_prefix"]))
            cur = db.execute(sql, args)
            # an authority left with no area and no court chain is retired,
            # not deleted — it stays resolvable by id with its history
            orphan = not db.execute(
                "SELECT 1 FROM competence WHERE authority_id=? LIMIT 1",
                (aid,)).fetchone() and not db.execute(
                "SELECT 1 FROM court_chain WHERE authority_id=? LIMIT 1",
                (aid,)).fetchone()
            if orphan:
                db.execute("UPDATE authority SET valid_to=? WHERE id=?",
                           (now(), aid))
            print(f"  override {f.name}: removed {cur.rowcount} competence "
                  f"rows from authority {aid}"
                  + (" (authority retired)" if orphan else ""))
            continue

        sets = ", ".join(f"{k}=?" for k in o["set"])
        db.execute(f"UPDATE authority SET {sets}, source='override' WHERE id=?",
                   (*o["set"].values(), aid))
        print(f"  override {f.name}: applied to authority {aid}")


def drop_self_loops(db: sqlite3.Connection) -> int:
    """Remove malformed edges before validation and serialization.

    Cross-source identity merges can turn a previously valid edge between two
    duplicate records into ``id -> id``. Keep the validation gate as a hard
    backstop, but normalize this known merge artefact in the builder itself.
    """
    n = db.execute(
        "SELECT COUNT(*) FROM authority_edge "
        "WHERE from_authority=to_authority"
    ).fetchone()[0]
    if n:
        db.execute(
            "DELETE FROM authority_edge WHERE from_authority=to_authority"
        )
        print(f"graph: removed {n} self-loop authority edges")
    return n


def classify_landratsamt_roots(db: sqlite3.Connection) -> int:
    """Give standalone district-office roots their structural node kind.

    PVOG and BayernPortal ingest generic organisational containers as
    ``sonstige`` because their actual competences live on relation rows.  A
    standalone official name of the form ``Landratsamt <district>`` is,
    however, an unambiguous authority type.  Keep the rule deliberately
    structural: department names contain ``" - "`` and compactly named
    units such as ``Landratsamt IT`` have an outgoing ``parent`` edge.  They
    must remain at their source-derived/specialist kind.
    """
    cur = db.execute(
        """UPDATE authority AS a SET kind='landratsamt'
           WHERE a.valid_to IS NULL
             AND a.kind='sonstige'
             AND a.name GLOB 'Landratsamt *'
             AND instr(a.name, ' - ') = 0
             AND NOT EXISTS (
                 SELECT 1 FROM authority_edge e
                 WHERE e.from_authority=a.id AND e.relation='parent'
             )"""
    )
    if cur.rowcount:
        print(f"authority kinds: {cur.rowcount} standalone Landratsamt "
              "roots classified")
    return cur.rowcount


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-validate", action="store_true")
    args = ap.parse_args()

    tmp = DB_PATH.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA.read_text())

    snaps = {}
    for source, loader in (("openplz", load_geo), ("justiz", load_justiz),
                           ("pvog", load_pvog), ("ba", load_ba),
                           ("bayernportal", load_bayernportal)):
        snap = latest_snapshot(source)
        if snap:
            print(f"[{source}] loading {snap}")
            loader(db, snap)
            snaps[source] = snap.name
        else:
            print(f"[{source}] no snapshot — skipped")

    print(f"[eu_curated] loading {EU_OVERLAY}")
    snaps["eu_curated"] = load_eu_overlay(db)

    apply_overrides(db)
    drop_self_loops(db)
    classify_landratsamt_roots(db)
    db.execute("INSERT OR REPLACE INTO build_info VALUES ('built_at', ?)", (now(),))
    db.execute("INSERT OR REPLACE INTO build_info VALUES ('snapshots', ?)",
               (json.dumps(snaps),))
    db.commit()

    if not args.skip_validate:
        errors = validate.check(db)
        if errors:
            print(f"VALIDATION FAILED ({len(errors)}):")
            for e in errors:
                print("  ✗", e)
            print(f"build kept at {tmp} for inspection; live DB untouched")
            return 1
        print("validation OK")

    db.close()
    tmp.replace(DB_PATH)
    print(f"-> {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
