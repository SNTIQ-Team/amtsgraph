"""Amtsgraph domain operations: place identity, competence, evidence.

Pure functions over an open SQLite connection. The REST routes in
``api.main`` and a future MCP adapter call the same functions, so the
contract is tested once, independent of transport.

Identity
--------
* ``place:jz:<PLZ>:<ORTK>``  a place of the official court register
  (Orts- und Gerichtsverzeichnis). The unit of court competence.
* ``place:ags:<AGS>``        a municipality (8-digit Gemeindeschlüssel).
  The unit of non-court (office) competence.
* ``court:de:<XJustiz-ID>``  a court node (see ``api.courts``).

Nothing here selects the first of several rows: every lookup that can match
more than one record reports ``ambiguous_identity`` with the candidates.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import date
from urllib.parse import urlencode

from api.contract import ContractError, envelope, snapshot_info

PLACE_JZ = "place:jz:"
PLACE_AGS = "place:ags:"

COURT_KINDS = frozenset({
    "amtsgericht", "landgericht", "oberlandesgericht", "oberstes_landesgericht",
    "sozialgericht", "landessozialgericht", "verwaltungsgericht",
    "oberverwaltungsgericht", "arbeitsgericht", "landesarbeitsgericht",
    "finanzgericht", "bundesgericht", "verfassungsgericht", "eu_court",
    "staatsanwaltschaft", "generalstaatsanwaltschaft",
})

SEVERITY_ORDER = {"block": 0, "warn": 1, "info": 2}

JUSTIZ_FINDER = "https://www.justizadressen.nrw.de/de/justiz/gericht"

REGISTERS = {
    "justizadressen": {
        "name": "Orts- und Gerichtsverzeichnis der Justiz "
                "(justizadressen.nrw.de)",
        "url": JUSTIZ_FINDER},
    "pvog": {
        "name": "Portalverbund Online-Gateway (PVOG) Suchdienst — "
                "Zuständigkeitsfinder",
        "url": "https://pvog.fitko.net/suchdienst/api"},
    "ba": {
        "name": "Bundesagentur für Arbeit — Gebietsstruktur der "
                "Grundsicherungsträger",
        "url": "https://statistik.arbeitsagentur.de/"},
    "bayernportal": {
        "name": "BayernPortal — Behördenwegweiser und Organigramme",
        "url": "https://www.bayernportal.de/"},
    "override": {
        "name": "Amtsgraph curated correction (pipeline/overrides)",
        "url": None},
}

DERIVATIONS = {
    "asylblg_title_filter":
        "PVOG named several offices for the AsylbLG probe service; this "
        "office was kept by unit-title patterns (heuristic).",
    "asylblg_title_filter_higher_level":
        "Ranked as a higher-level body by unit-title patterns (heuristic); "
        "shown as supervisory, never as the primary answer.",
    "ba_traeger_area_inherited_by_office":
        "The Kreis-level area is inherited from the BA Träger assignment of "
        "a Kreis with a single Träger (inference).",
    "bayernportal_department_name_classification":
        "The office type was classified from the department name and the "
        "area from the organigram root (heuristic); it replaced the generic "
        "PVOG answer for this Kreis.",
    "office_identity_merged_by_name":
        "A PVOG office and a BayernPortal unit with the same normalized name "
        "were merged into one node (identity heuristic).",
}


def normalize(name: str) -> str:
    """Same fold as the pipeline's normalize_name (jz_place.ort_norm)."""
    s = name.lower().strip().split(",")[0]
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.split())


def today() -> str:
    return date.today().isoformat()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    return bool(table_columns(conn, table))


# ============================================================ identities

def jz_place_id(plz: str, ortk: str) -> str:
    return f"{PLACE_JZ}{plz}:{ortk}"


def ags_place_id(ags: str) -> str:
    return f"{PLACE_AGS}{ags}"


def parse_place_id(value: str) -> tuple[str, dict]:
    """-> ('court_register_place', {plz, ortk}) | ('municipality', {ags})."""
    if not isinstance(value, str) or len(value) > 160:
        raise ContractError("invalid_argument", "place_id must be a string",
                            invalid_arguments=["place_id"])
    if value.startswith(PLACE_JZ):
        plz, sep, ortk = value[len(PLACE_JZ):].partition(":")
        if re.fullmatch(r"\d{5}", plz) and sep and ortk.strip() == ortk \
                and ortk and not re.search(r"[\x00-\x1f]", ortk):
            return "court_register_place", {"plz": plz, "ortk": ortk}
    elif value.startswith(PLACE_AGS):
        ags = value[len(PLACE_AGS):]
        if re.fullmatch(r"\d{8}", ags):
            return "municipality", {"ags": ags}
    raise ContractError(
        "invalid_argument",
        "place_id must be place:jz:<PLZ>:<ORTK> or place:ags:<8-digit AGS> "
        "(obtain it from /resolve/place)",
        invalid_arguments=["place_id"])


def _require(pattern: str, value: str | None, name: str) -> None:
    if value is not None and not re.fullmatch(pattern, value):
        raise ContractError("invalid_argument", f"malformed {name}: {value!r}",
                            invalid_arguments=[name])


def _jz_candidate(row) -> dict:
    ags = row["gemeinde_ags"] if "gemeinde_ags" in row.keys() else None
    return {"place_id": jz_place_id(row["plz"], row["ortk"]),
            "type": "court_register_place",
            "plz": row["plz"], "ort": row["ort"], "ortk": row["ortk"],
            "municipality_id": ags_place_id(ags) if ags else None,
            "municipality_link_basis": "plz_and_name_match" if ags else None}


# ============================================================ caveats

def aggregate_caveats(conn: sqlite3.Connection,
                      scopes: list[tuple[str, str, str]],
                      narrowing: str | None, *, any_matter: bool = False,
                      on: str | None = None) -> tuple[list[dict], list[dict]]:
    """All caveats of every given scope plus global ones, deduplicated.

    ``scopes`` are (scope_level, scope_key, role) triples; ``role`` says why
    the scope belongs to this answer (place, municipality, district, land,
    matter, authority, court, assignment). A caveat narrowed to a matter
    applies only when that matter (court matter or office kind) is asked.
    Returns (active, inactive); a caveat outside its validity interval is
    reported as inactive with the reason, never silently dropped.
    """
    on = on or today()
    cols = table_columns(conn, "caveat")
    validity = {"valid_from", "valid_until_exclusive"} <= cols
    select = ("id, scope_level, scope_key, matter, severity, text_de, source"
              + (", valid_from, valid_until_exclusive" if validity else ""))
    roles: dict[tuple[str, str], list[str]] = defaultdict(list)
    for level, key, role in scopes:
        if key is None:
            continue
        if role not in roles[(level, str(key))]:
            roles[(level, str(key))].append(role)
    by_level: dict[str, list[str]] = defaultdict(list)
    for level, key in roles:
        by_level[level].append(key)
    matter_sql = "" if any_matter else " AND (matter IS NULL OR matter = ?)"
    matter_arg = () if any_matter else (narrowing,)

    found: dict[int, dict] = {}

    def add(row, via):
        item = found.get(row["id"])
        if item is None:
            item = {"id": row["id"], "scope_level": row["scope_level"],
                    "scope_key": row["scope_key"], "matter": row["matter"],
                    "severity": row["severity"], "text_de": row["text_de"],
                    "source": row["source"],
                    "valid_from": row["valid_from"] if validity else None,
                    "valid_until_exclusive":
                        row["valid_until_exclusive"] if validity else None,
                    "applies_via": []}
            found[row["id"]] = item
        if via not in item["applies_via"]:
            item["applies_via"].append(via)

    for level, keys in by_level.items():
        keys = sorted(set(keys))
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                    f"""SELECT {select} FROM caveat WHERE scope_level = ?
                        AND scope_key IN ({marks}){matter_sql}""",
                    (level, *chunk, *matter_arg)):
                for role in roles[(level, row["scope_key"])]:
                    add(row, {"scope_level": level,
                              "scope_key": row["scope_key"], "role": role})
    for row in conn.execute(
            f"SELECT {select} FROM caveat WHERE scope_level = 'global'"
            f"{matter_sql}", matter_arg):
        add(row, {"scope_level": "global", "scope_key": row["scope_key"],
                  "role": "global"})

    active, inactive = [], []
    for item in found.values():
        start, end = item["valid_from"], item["valid_until_exclusive"]
        if start and on < start:
            item.update(active=False, inactive_reason="not_yet_valid")
            inactive.append(item)
        elif end and on >= end:
            item.update(active=False, inactive_reason="expired")
            inactive.append(item)
        else:
            item.update(active=True, inactive_reason=None)
            active.append(item)
    order = lambda c: (SEVERITY_ORDER.get(c["severity"], 9), c["id"])  # noqa: E731
    return sorted(active, key=order), sorted(inactive, key=order)


def critical_warning(active: list[dict]) -> str | None:
    """One human-readable line with every blocking/warning caveat."""
    critical = [c for c in active if c["severity"] in ("block", "warn")]
    if not critical:
        return None
    return (f"ACHTUNG — {len(critical)} Vorbehalt(e) zu diesem Ergebnis: "
            + " | ".join(f"[{c['severity'].upper()}] {c['text_de']}"
                         for c in critical))


def caveat_warnings(active: list[dict], inactive: list[dict]) -> list[str]:
    out = [f"[{c['severity']}] {c['text_de']}" for c in active]
    if inactive:
        out.append(f"{len(inactive)} caveat(s) outside their validity "
                   f"interval are listed in inactive_caveats and do not "
                   f"qualify this current answer")
    return out


# ============================================================ cards

def authority_card(conn: sqlite3.Connection, authority_id: int) -> dict:
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


def court_ids_for(conn: sqlite3.Connection, authority_ids) -> dict[int, str]:
    ids = sorted({int(a) for a in authority_ids})
    if not ids or not has_table(conn, "court_authority"):
        return {}
    marks = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT authority_id, court_id FROM court_authority "
        f"WHERE authority_id IN ({marks})", ids)}


# ============================================================ evidence

def _statutory(conn, subject: str, matter: str) -> list[dict]:
    if not has_table(conn, "assignment_evidence"):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM assignment_evidence WHERE subject = ? AND matter = ?",
        (subject, matter))]


def _statutory_evidence(rows: list[dict], territory: set[tuple[str, str]],
                        court_id: str | None, on: str) -> list[dict]:
    out = []
    for r in rows:
        if (r["territory_level"], r["territory_key"]) not in territory:
            continue
        if r.get("court_id") and r["court_id"] != court_id:
            continue
        start, end = r.get("valid_from"), r.get("valid_until_exclusive")
        in_force = not ((start and on < start) or (end and on >= end))
        out.append({
            "evidence_id": f"evidence:curated:{r['id']}",
            "kind": r["evidence_kind"],
            "citation": r["citation"], "norm_id": r.get("norm_id"),
            "passage": r.get("passage"), "source_url": r["source_url"],
            "observed_on": r["observed_on"],
            "valid_from": start, "valid_until_exclusive": end,
            "interval_status": "current" if in_force else "not_current",
            "territory": {"level": r["territory_level"],
                          "key": r["territory_key"]},
            "derived_by": r["derived_by"],
            "legal_effect_verified": bool(r["legal_effect_verified"]),
            "note": r.get("note")})
    return out


def _statutory_basis(evidence: list[dict]) -> dict:
    statutory = [e for e in evidence if e["kind"] == "statutory_assignment"]
    if not statutory:
        return {"status": "not_recorded", "evidence_ids": [],
                "note": "No statutory assignment is recorded for this "
                        "relation; the register observation is the only "
                        "evidence."}
    return {"status": "recorded", "evidence_ids":
            [e["evidence_id"] for e in statutory],
            "legal_effect_verified": all(e["legal_effect_verified"]
                                         for e in statutory)}


CHAIN_FUNCTIONS = {"court": ("first_instance_listed", "higher_instance_listed"),
                   "prosecution": ("prosecution_listed", "prosecution_listed")}


def court_assignment(*, place: dict, matter: str, matter_label: str | None,
                     row, court_id: str | None, snapshot: dict,
                     statutory: list[dict], territory: set, on: str) -> dict:
    plz, ortk, ort = place["plz"], place["ortk"], place["ort"]
    role, position = row["role"], row["position"]
    function = CHAIN_FUNCTIONS.get(role, ("listed", "listed"))[
        0 if position == 1 else 1]
    observed = snapshot["source_snapshots"].get("justiz")
    assignment_id = f"assignment:court:{plz}:{ortk}:{matter}:{role}:{position}"
    register = {
        "evidence_id": f"evidence:register:justizadressen:{observed}:"
                       f"{plz}:{ortk}:{matter}",
        "kind": "register_observation",
        "register": {"id": "justizadressen",
                     "name": REGISTERS["justizadressen"]["name"]},
        "observed_on": observed,
        "snapshot": f"justiz/{observed}" if observed else None,
        "query": {"plz": plz, "ort": ort, "ang": matter},
        "source_url": f"{JUSTIZ_FINDER}?"
                      + urlencode({"plz": plz, "ort": ort, "ang": matter}),
        "record_locator": {"table": "court_chain", "plz": plz, "ortk": ortk,
                           "matter": matter, "role": role,
                           "position": position},
        "supports": ["matter", "territory", "instance_position"],
        "does_not_establish": ["statutory_basis", "validity_interval",
                               "case_specific_applicability",
                               "appeal_availability"],
        "content_hash": None, "content_hash_status": "not_retained",
        "derived_by": "parser", "legal_effect_verified": False}
    evidence = [register] + _statutory_evidence(statutory, territory,
                                                court_id, on)
    unresolved = [{
        "code": "case_specific_applicability",
        "text": "The register maps the place and matter category to this "
                "chain. Competence for a concrete case can depend on facts "
                "not checked here (e.g. agreed or special jurisdiction, "
                "place of performance or of the act)."}]
    if role == "court" and position == 1 and matter == "zivil":
        unresolved.append({
            "code": "first_instance_by_value_or_assignment",
            "text": "Whether the Amtsgericht or the Landgericht decides in "
                    "first instance depends on the value in dispute or a "
                    "special assignment (§§ 23, 71 GVG); the register lists "
                    "the instance sequence, not the case value."})
    if role == "court" and position > 1:
        unresolved.append({
            "code": "appeal_availability_not_assessed",
            "text": "Listed as a higher instance of the register chain. "
                    "Whether an appeal is available, admissible and in time "
                    "depends on procedure, decision type, value or leave and "
                    "statutory exceptions; adjacency in the chain does not "
                    "establish it."})
    return {
        "assignment_id": assignment_id,
        "relation": "competent_for",
        "function": function,
        "authority_id": row["authority_id"],
        "court_id": court_id,
        "matter": {"code": matter, "label_de": matter_label},
        "territory": {"place_id": jz_place_id(plz, ortk),
                      "type": "court_register_place", "plz": plz,
                      "ortk": ortk, "ort": ort},
        "conditions": [row["note"]] if row["note"] else [],
        "role": role, "position": position,
        "temporal": {"mode": "current_only", "valid_from": None,
                     "valid_until_exclusive": None,
                     "interval_status": "not_recorded",
                     "observed_on": observed},
        "assignment_basis": "observed",
        "derivations": [],
        "exceptions": [],
        "unresolved_facts": unresolved,
        "evidence": evidence,
        "statutory_basis": _statutory_basis(evidence),
        "legal_effect_verified": False,
    }


def competence_assignment(*, row, authority_source: str | None,
                          authority_url: str | None, snapshot: dict,
                          statutory: list[dict], territory: set,
                          on: str) -> dict:
    keys = row.keys()
    source = row["source"] if "source" in keys and row["source"] \
        else authority_source
    recorded = "source" in keys and bool(row["source"])
    observed = (row["observed_on"] if "observed_on" in keys and
                row["observed_on"] else
                snapshot["source_snapshots"].get(source or ""))
    derivation = row["derivation"] if "derivation" in keys else None
    derivations = [d for d in (derivation or "").split(";") if d]
    review_due = row["review_due"] if "review_due" in keys else None
    assignment_id = (f"assignment:competence:{row['authority_id']}:"
                     f"{row['kind']}:{row['level']}:{row['area']}")
    kind = "curated_source_review" if source == "override" \
        else "register_observation"
    item = {
        "evidence_id": f"evidence:{source or 'unknown'}:{observed}:"
                       f"{row['authority_id']}:{row['kind']}:{row['level']}:"
                       f"{row['area']}",
        "kind": kind,
        "register": {"id": source,
                     "name": REGISTERS.get(source or "", {}).get("name")},
        "observed_on": observed,
        "source_ref": row["source_ref"] if "source_ref" in keys else None,
        "source_url": (row["source_url"] if "source_url" in keys
                       and row["source_url"] else authority_url),
        "record_locator": {"table": "competence",
                           "authority_id": row["authority_id"],
                           "kind": row["kind"], "level": row["level"],
                           "area": row["area"]},
        "relation_provenance": "recorded" if recorded
                               else "inferred_from_authority_record",
        "supports": ["office_kind", "territory"],
        "does_not_establish": ["statutory_basis", "validity_interval",
                               "case_specific_applicability"],
        "content_hash": None, "content_hash_status": "not_retained",
        "derived_by": "parser" if not derivations else "heuristic",
        "legal_effect_verified": False}
    if kind == "curated_source_review":
        item["review_due"] = review_due
        item["review_status"] = ("overdue" if review_due and on > review_due
                                 else "current" if review_due else "unknown")
    evidence = [item] + _statutory_evidence(statutory, territory, None, on)
    basis = ("curated" if kind == "curated_source_review"
             else "inferred" if derivations else "observed")
    unresolved = [{
        "code": "case_specific_applicability",
        "text": "Competence can depend on facts of the case (residence "
                "status, reception phase, special assignments) that are "
                "not checked here."}]
    if review_due and on > review_due:
        unresolved.append({"code": "curated_review_overdue",
                           "text": f"The curated correction was due for "
                                   f"re-check on {review_due}."})
    return {
        "assignment_id": assignment_id,
        "relation": "competent_for",
        "function": "primary_office" if row["rank"] == 0
                    else "higher_level_or_supervisory",
        "authority_id": row["authority_id"],
        "matter": {"code": row["kind"], "type": "authority_kind"},
        "territory": {"level": row["level"], "key": row["area"]},
        "rank": row["rank"],
        "conditions": [],
        "temporal": {"mode": "current_only", "valid_from": None,
                     "valid_until_exclusive": None,
                     "interval_status": "not_recorded",
                     "observed_on": observed},
        "assignment_basis": basis,
        "derivations": [{"method": d, "description": DERIVATIONS.get(d)}
                        for d in derivations],
        "exceptions": [],
        "unresolved_facts": unresolved,
        "evidence": evidence,
        "statutory_basis": _statutory_basis(evidence),
        "legal_effect_verified": False,
    }


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for e in items:
        if e["evidence_id"] not in seen:
            seen.add(e["evidence_id"])
            out.append(e)
    return out


def _attach_exceptions(assignments: list[dict], caveats: list[dict]) -> None:
    """Caveats that target one assignment (or its authority/court) are that
    assignment's exceptions; they also stay in the aggregated list."""
    for a in assignments:
        keys = {("assignment", a["assignment_id"]),
                ("authority", str(a["authority_id"]))}
        if a.get("court_id"):
            keys.add(("court", a["court_id"]))
        a["exceptions"] = [
            {"caveat_id": c["id"], "severity": c["severity"],
             "text_de": c["text_de"]}
            for c in caveats
            if any((v["scope_level"], v["scope_key"]) in keys
                   for v in c["applies_via"])]


def _matter_label(conn, matter: str) -> tuple[bool, str | None]:
    try:
        row = conn.execute("SELECT label_de FROM matter WHERE code = ?",
                           (matter,)).fetchone()
    except sqlite3.Error:
        return True, None                # no taxonomy in this store
    return (row is not None, row["label_de"] if row else None)


def _geo_scopes(conn, ags: str | None) -> list[tuple[str, str, str]]:
    if not ags:
        return []
    out = [("gemeinde", ags, "municipality")]
    g = conn.execute("SELECT kreis_ags FROM gemeinde WHERE ags = ?",
                     (ags,)).fetchone()
    if g and g["kreis_ags"]:
        out.append(("kreis", g["kreis_ags"], "district"))
    out.append(("land", ags[:2], "land"))
    return out


# ============================================================ place_resolve

def place_resolve(conn: sqlite3.Connection, *, purpose: str | None,
                  q: str | None = None, plz: str | None = None,
                  ort: str | None = None, ags: str | None = None,
                  limit: int = 20, request_scope: dict | None = None,
                  snapshot: dict | None = None) -> dict:
    """Resolve free text / PLZ / AGS to stable place ids, never guessing.

    ``purpose=court`` returns court-register places (place:jz:...), the unit
    of court competence; ``purpose=authority`` returns municipalities
    (place:ags:...), the unit of office competence.
    """
    snapshot = snapshot or snapshot_info(conn)
    if purpose not in ("court", "authority"):
        raise ContractError("invalid_argument",
                            "purpose is required: court | authority",
                            invalid_arguments=["purpose"])
    given = [k for k, v in (("q", q), ("plz", plz), ("ags", ags)) if v]
    if len(given) != 1:
        raise ContractError("invalid_argument",
                            "give exactly one of q, plz, ags",
                            invalid_arguments=given or ["q"])
    if ort is not None and not plz:
        raise ContractError("invalid_argument", "ort narrows a plz only",
                            invalid_arguments=["ort"])
    if ags and purpose != "authority":
        raise ContractError("invalid_argument",
                            "ags identifies a municipality: use "
                            "purpose=authority", invalid_arguments=["ags"])
    if not 1 <= int(limit) <= 100:
        raise ContractError("invalid_argument", "limit must be 1..100",
                            invalid_arguments=["limit"])
    _require(r"\d{5}", plz, "plz")
    _require(r"\d{8}", ags, "ags")
    if q is not None:
        q = q.strip()
        if not q or len(q) > 200:
            raise ContractError("invalid_argument", "q must be 1..200 chars",
                                invalid_arguments=["q"])
        m = re.fullmatch(r"(\d{5})(?:\s+(.+))?", q)
        if m:
            plz, ort = m.group(1), m.group(2)
        elif re.fullmatch(r"\d+", q):
            raise ContractError("invalid_argument",
                                "a numeric q must be a 5-digit PLZ",
                                invalid_arguments=["q"])
    name = normalize(ort) if ort else None
    match = "exact"
    if purpose == "court":
        if plz:
            sql = "FROM jz_place WHERE plz = ?" + (" AND ort_norm = ?" if name else "")
            args = (plz, name) if name else (plz,)
            source = "jz_place by plz" + (" and ort" if name else "")
        else:
            sql, args = "FROM jz_place WHERE ort_norm = ?", (normalize(q),)
            source = "jz_place by exact name"
            if not conn.execute(f"SELECT 1 {sql} LIMIT 1", args).fetchone():
                sql = "FROM jz_place WHERE ort_norm LIKE ? || '%'"
                args, match = (normalize(q),), "prefix"
                source = "jz_place by name prefix"
        total = conn.execute(f"SELECT COUNT(*) {sql}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * {sql} ORDER BY plz, ortk LIMIT ?",
                            (*args, limit)).fetchall()
        candidates = [_jz_candidate(r) for r in rows]
    else:
        base = """FROM gemeinde g LEFT JOIN kreis k ON k.ags = g.kreis_ags
                  LEFT JOIN land l ON l.code = k.land_code"""
        if ags:
            sql, args, source = f"{base} WHERE g.ags = ?", (ags,), "gemeinde by ags"
        elif plz:
            sql = (f"{base} JOIN gemeinde_plz gp ON gp.ags = g.ags "
                   f"WHERE gp.plz = ?" + (" AND g.name_norm = ?" if name else ""))
            args = (plz, name) if name else (plz,)
            source = "gemeinde by plz" + (" and name" if name else "")
        else:
            sql, args = f"{base} WHERE g.name_norm = ?", (normalize(q),)
            source = "gemeinde by exact name"
            if not conn.execute(f"SELECT 1 {sql} LIMIT 1", args).fetchone():
                sql = f"{base} WHERE g.name_norm LIKE ? || '%'"
                args, match = (normalize(q),), "prefix"
                source = "gemeinde by name prefix"
        total = conn.execute(f"SELECT COUNT(*) {sql}", args).fetchone()[0]
        rows = conn.execute(
            f"""SELECT g.ags, g.name_simple AS name, g.kind,
                       g.kreis_ags, k.name AS kreis, l.name AS land,
                       (SELECT COUNT(*) FROM gemeinde_plz p
                        WHERE p.ags = g.ags) AS plz_count
                {sql} ORDER BY g.ags LIMIT ?""", (*args, limit)).fetchall()
        candidates = [{"place_id": ags_place_id(r["ags"]),
                       "type": "municipality", **dict(r)} for r in rows]
    status = ("ok" if total == 1 else "ambiguous_identity" if total > 1
              else "not_found_in_searched_scope")
    warnings = []
    if total > len(candidates):
        warnings.append(f"{total} candidates, {len(candidates)} returned "
                        f"(limit); narrow the query")
    if match == "prefix" and total:
        warnings.append("no exact name match; candidates are prefix matches")
    return envelope(
        operation="place_resolve", status=status,
        request_scope=request_scope or {}, snapshot=snapshot,
        applied_constraints={"purpose": purpose, "plz": plz, "ort": ort,
                             "ags": ags, "name_match": match if q and not plz
                             else None, "limit": limit},
        searched=[{"source": source, "status": "ok", "count": total}],
        warnings=warnings, total=total,
        place_id=candidates[0]["place_id"] if status == "ok" else None,
        place=candidates[0] if status == "ok" else None,
        candidates=candidates if status != "ok" else [],
        truncated=total > len(candidates),
        purpose=purpose)


# ============================================================ court chain

def select_court_place(conn, *, plz=None, ort=None, ortk=None,
                       place_id=None) -> tuple[list, dict]:
    if place_id is not None:
        if any(v is not None for v in (plz, ort, ortk)):
            raise ContractError("invalid_argument",
                                "place_id cannot be combined with plz/ort/ortk",
                                invalid_arguments=["place_id"])
        ptype, key = parse_place_id(place_id)
        if ptype != "court_register_place":
            raise ContractError(
                "invalid_argument",
                "court competence needs a court-register place "
                "(place:jz:...); resolve it with /resolve/place?purpose=court",
                invalid_arguments=["place_id"])
        plz, ortk = key["plz"], key["ortk"]
        rows = conn.execute("SELECT * FROM jz_place WHERE plz = ? AND ortk = ?",
                            (plz, ortk)).fetchall()
        return rows, {"place_id": place_id}
    if not plz:
        raise ContractError("invalid_argument", "give place_id or plz",
                            missing_arguments=["place_id"])
    _require(r"\d{5}", plz, "plz")
    rows = conn.execute(
        "SELECT * FROM jz_place WHERE plz = ?" +
        (" AND ort_norm = ?" if ort else "") +
        (" AND ortk = ?" if ortk else ""),
        (plz, *([normalize(ort)] if ort else []),
         *([ortk] if ortk else []))).fetchall()
    return rows, {"plz": plz, "ort": ort, "ortk": ortk}


def court_chain(conn: sqlite3.Connection, *, matter: str,
                plz: str | None = None, ort: str | None = None,
                ortk: str | None = None, place_id: str | None = None,
                request_scope: dict | None = None,
                snapshot: dict | None = None,
                operation: str = "court_chain") -> dict:
    snapshot = snapshot or snapshot_info(conn)
    on = today()
    known, label = _matter_label(conn, matter)
    if not known:
        raise ContractError("invalid_argument",
                            f"unknown court matter {matter!r} (see /matters)",
                            invalid_arguments=["matter"])
    places, constraints = select_court_place(
        conn, plz=plz, ort=ort, ortk=ortk, place_id=place_id)
    constraints["matter"] = matter
    common = dict(operation=operation, request_scope=request_scope or {},
                  snapshot=snapshot, applied_constraints=constraints,
                  matter=matter)
    if not places:
        return envelope(
            status="not_found_in_searched_scope", total=0,
            searched=[{"source": "jz_place", "status": "ok", "count": 0}],
            warnings=["no court-register place matches this input"],
            chain=[], caveats=[], inactive_caveats=[],
            critical_warning=None, **common)
    if len(places) != 1:
        cands = [_jz_candidate(p) for p in
                 sorted(places, key=lambda p: (p["plz"], p["ortk"]))]
        return envelope(
            status="ambiguous_identity", total=None,
            searched=[{"source": "jz_place", "status": "ok",
                       "count": len(places)}],
            warnings=[f"{len(places)} court-register places match; choose "
                      f"one place_id — no place is selected implicitly"],
            ambiguity="place", needs_ort=True, needs_place_id=True,
            options=[{"plz": c["plz"], "ort": c["ort"], "ortk": c["ortk"],
                      "place_id": c["place_id"]} for c in cands],
            candidates=cands, **common)

    (place,) = places
    rows = conn.execute(
        """SELECT position, role, note, authority_id FROM court_chain
           WHERE plz = ? AND ortk = ? AND matter = ?
           ORDER BY role DESC, position""",
        (place["plz"], place["ortk"], matter)).fetchall()
    court_ids = court_ids_for(conn, [r["authority_id"] for r in rows])
    ags = place["gemeinde_ags"] if "gemeinde_ags" in place.keys() else None
    territory = {("jz_place", f"{place['plz']}|{place['ortk']}"),
                 ("plz", place["plz"])}
    if ags:
        territory |= {(level, key) for level, key, _ in _geo_scopes(conn, ags)}
    statutory = _statutory(conn, "court_matter", matter)
    assignments = [court_assignment(
        place=place, matter=matter, matter_label=label, row=r,
        court_id=court_ids.get(r["authority_id"]), snapshot=snapshot,
        statutory=statutory, territory=territory, on=on) for r in rows]

    scopes = [("jz_place", f"{place['plz']}|{place['ortk']}", "place"),
              ("plz", place["plz"], "place"), ("matter", matter, "matter")]
    scopes += _geo_scopes(conn, ags)
    scopes += [("authority", str(r["authority_id"]), "authority") for r in rows]
    scopes += [("court", cid, "court") for cid in court_ids.values()]
    scopes += [("assignment", a["assignment_id"], "assignment")
               for a in assignments]
    active, inactive = aggregate_caveats(conn, scopes, matter, on=on)
    _attach_exceptions(assignments, active)

    place_out = _jz_candidate(place)
    chain = [{"position": r["position"], "role": r["role"], "note": r["note"],
              "court_id": court_ids.get(r["authority_id"]),
              "assignment_id": a["assignment_id"],
              **authority_card(conn, r["authority_id"])}
             for r, a in zip(rows, assignments)]
    warnings = caveat_warnings(active, inactive)
    warnings.append("Register observation only: no statutory basis or "
                    "validity interval is recorded for these assignments"
                    if all(a["statutory_basis"]["status"] == "not_recorded"
                           for a in assignments) else
                    "Some assignments carry curated statutory evidence")
    status = "ok" if rows else "not_found_in_searched_scope"
    if not rows:
        warnings.insert(0, f"the court register lists no chain for matter "
                           f"'{matter}' at this place")
    return envelope(
        status=status, total=len(rows),
        searched=[{"source": "jz_place", "status": "ok", "count": 1},
                  {"source": "court_chain (justizadressen register "
                             "observation)", "status": "ok",
                   "count": len(rows)},
                  {"source": "caveat", "status": "ok",
                   "count": len(active) + len(inactive)}],
        evidence=_dedupe([e for a in assignments for e in a["evidence"]]),
        warnings=warnings,
        place=place_out, place_id=place_out["place_id"],
        matter_label=label, chain=chain, assignments=assignments,
        caveats=active, inactive_caveats=inactive,
        critical_warning=critical_warning(active),
        instance_note=("Positions follow the register's instance sequence. "
                       "They do not establish that an appeal is available "
                       "in a concrete case."), **common)


# ============================================================ authorities

def select_municipality(conn, *, ags=None, place_id=None) -> tuple[str, dict]:
    if place_id is not None:
        if ags is not None:
            raise ContractError("invalid_argument",
                                "place_id cannot be combined with ags",
                                invalid_arguments=["place_id", "ags"])
        ptype, key = parse_place_id(place_id)
        if ptype != "municipality":
            raise ContractError(
                "invalid_argument",
                "office competence needs a municipality (place:ags:...); "
                "resolve it with /resolve/place?purpose=authority",
                invalid_arguments=["place_id"])
        return key["ags"], {"place_id": place_id}
    if not ags:
        raise ContractError("invalid_argument", "give place_id or ags",
                            missing_arguments=["place_id"])
    _require(r"\d{8}", ags, "ags")
    return ags, {"ags": ags}


def authority_resolution(conn: sqlite3.Connection, *, kind: str,
                         ags: str | None = None, place_id: str | None = None,
                         request_scope: dict | None = None,
                         snapshot: dict | None = None,
                         operation: str = "competence_resolve") -> dict:
    snapshot = snapshot or snapshot_info(conn)
    on = today()
    if kind in COURT_KINDS:
        raise ContractError(
            "invalid_argument",
            "court kinds resolve via /resolve/court (competence is "
            "matter-specific)", invalid_arguments=["kind"])
    if has_table(conn, "authority_kind") and not conn.execute(
            "SELECT 1 FROM authority_kind WHERE kind = ?", (kind,)).fetchone():
        raise ContractError("invalid_argument", f"unknown authority kind "
                            f"{kind!r}", invalid_arguments=["kind"])
    ags, constraints = select_municipality(conn, ags=ags, place_id=place_id)
    constraints["kind"] = kind
    common = dict(operation=operation, request_scope=request_scope or {},
                  snapshot=snapshot, applied_constraints=constraints,
                  kind=kind, ags=ags)
    g = conn.execute("SELECT * FROM gemeinde WHERE ags = ?", (ags,)).fetchone()
    if not g:
        return envelope(
            status="not_found_in_searched_scope", total=0,
            searched=[{"source": "gemeinde", "status": "ok", "count": 0}],
            warnings=["unknown AGS"], resolved=None, candidates=[],
            supervisory=[], caveats=[], inactive_caveats=[],
            critical_warning=None, **common)
    plzs = [r[0] for r in conn.execute(
        "SELECT plz FROM gemeinde_plz WHERE ags = ?", (ags,))]
    area_sql = """(c.level='gemeinde' AND c.area = ?)
                  OR (c.level='kreis' AND c.area = ?)
                  OR (c.level='land' AND c.area = ?)
                  OR (c.level='plz' AND c.area IN
                      (SELECT plz FROM gemeinde_plz WHERE ags = ?))"""
    area_args = (ags, g["kreis_ags"], ags[:2], ags)
    rows = conn.execute(
        f"""SELECT c.*, a.source AS a_source, a.source_url AS a_url
            FROM competence c JOIN authority a ON a.id = c.authority_id
            WHERE c.kind = ? AND a.valid_to IS NULL AND ({area_sql})
            ORDER BY c.rank, c.authority_id, c.level, c.area""",
        (kind, *area_args)).fetchall()
    rank_of: dict[int, int] = {}
    for r in rows:
        rank_of[r["authority_id"]] = min(rank_of.get(r["authority_id"], 9),
                                         r["rank"])
    primary = sorted(a for a, rk in rank_of.items() if rk == 0)
    supervisory = sorted(a for a, rk in rank_of.items() if rk > 0)

    territory = {("gemeinde", ags), ("kreis", g["kreis_ags"]),
                 ("land", ags[:2])} | {("plz", p) for p in plzs}
    statutory = _statutory(conn, "authority_kind", kind)
    assignments = [competence_assignment(
        row=r, authority_source=r["a_source"], authority_url=r["a_url"],
        snapshot=snapshot, statutory=statutory, territory=territory, on=on)
        for r in rows]
    scopes = _geo_scopes(conn, ags)
    scopes += [("plz", p, "place") for p in plzs]
    scopes += [("matter", kind, "matter")]
    scopes += [("authority", str(a), "authority") for a in rank_of]
    scopes += [("assignment", a["assignment_id"], "assignment")
               for a in assignments]
    active, inactive = aggregate_caveats(conn, scopes, kind, on=on)
    _attach_exceptions(assignments, active)

    cards = [authority_card(conn, a) for a in primary]
    warnings = caveat_warnings(active, inactive)
    extra = {}
    if len(cards) == 1:
        status = "ok"
    elif cards:
        status = "ambiguous_identity"
        extra["ambiguity"] = "competent_authority"
        warnings.insert(0, f"{len(cards)} offices are listed as competent; "
                           f"none is selected implicitly")
    elif supervisory:
        status = "partial"
        extra["reason"] = "no_primary_assignment"
        warnings.insert(0, "only higher-level/supervisory bodies are known "
                           "for this municipality and kind")
    else:
        status = "not_found_in_searched_scope"
        warnings.insert(0, f"no {kind} is recorded for this municipality")
    inferred = [a for a in assignments if a["assignment_basis"] == "inferred"]
    if inferred:
        warnings.append(f"{len(inferred)} assignment(s) rest on an inference "
                        f"(see assignments[].derivations), not on a direct "
                        f"register statement")
    return envelope(
        status=status, total=len(primary),
        searched=[{"source": "gemeinde", "status": "ok", "count": 1},
                  {"source": "competence (register observations)",
                   "status": "ok", "count": len(rows)},
                  {"source": "caveat", "status": "ok",
                   "count": len(active) + len(inactive)}],
        evidence=_dedupe([e for a in assignments for e in a["evidence"]]),
        warnings=warnings,
        place_id=ags_place_id(ags), gemeinde=g["name_simple"],
        resolved=cards[0] if len(cards) == 1 else None,
        candidates=cards if len(cards) > 1 else [],
        supervisory=[authority_card(conn, a) for a in supervisory],
        assignments=assignments, caveats=active, inactive_caveats=inactive,
        critical_warning=critical_warning(active), **extra, **common)


def competence_resolve(conn: sqlite3.Connection, *, place_id: str,
                       matter: str | None = None, kind: str | None = None,
                       request_scope: dict | None = None,
                       snapshot: dict | None = None) -> dict:
    """One entry point: court matter at a court-register place, or office
    kind at a municipality. The subject decides which competence plane."""
    ptype, _ = parse_place_id(place_id)
    if (matter is None) == (kind is None):
        raise ContractError("invalid_argument",
                            "give exactly one of matter (court) or kind "
                            "(office)", invalid_arguments=["matter", "kind"])
    if matter is not None:
        if ptype != "court_register_place":
            raise ContractError(
                "invalid_argument",
                "court competence needs a court-register place (place:jz:...)",
                invalid_arguments=["place_id"])
        body = court_chain(conn, matter=matter, place_id=place_id,
                           request_scope=request_scope, snapshot=snapshot,
                           operation="competence_resolve")
        firsts = [c for c in body.get("chain", [])
                  if c["role"] == "court" and c["position"] == 1]
        body["subject"] = "court_matter"
        body["resolved"] = firsts[0] if len(firsts) == 1 else None
        return body
    if ptype != "municipality":
        raise ContractError(
            "invalid_argument",
            "office competence needs a municipality (place:ags:...)",
            invalid_arguments=["place_id"])
    body = authority_resolution(conn, kind=kind, place_id=place_id,
                                request_scope=request_scope,
                                snapshot=snapshot)
    body["subject"] = "authority_kind"
    return body


# ============================================================ detail

EDGE_BASIS = {
    "appeal": ("derived_from_register_chain_adjacency",
               "Next instance in an observed register chain; not proof that "
               "an appeal is available."),
    "parent": ("organigram_or_register_hierarchy",
               "Organisational parent; not a statement of competence."),
    "supervision": ("pvog_title_heuristic",
                    "Higher-level body inferred from unit titles."),
}


def authority_detail(conn: sqlite3.Connection, authority_id: int, *,
                     request_scope: dict | None = None,
                     snapshot: dict | None = None) -> dict:
    snapshot = snapshot or snapshot_info(conn)
    common = dict(operation="authority_detail",
                  request_scope=request_scope or {}, snapshot=snapshot,
                  applied_constraints={"authority_id": authority_id})
    if not conn.execute("SELECT 1 FROM authority WHERE id = ?",
                        (authority_id,)).fetchone():
        return envelope(status="not_in_corpus", total=0,
                        searched=[{"source": "authority", "status": "ok",
                                   "count": 0}],
                        warnings=["unknown authority id (ids are local to "
                                  "one build; use court ids or external "
                                  "ids for durable references)"],
                        **common)
    card = authority_card(conn, authority_id)

    def edges(sql):
        out = []
        for e in conn.execute(sql, (authority_id,)):
            d = dict(e)
            basis, meaning = EDGE_BASIS.get(d["relation"], (
                "curated_source" if d.get("source") == "eu_curated"
                else "graph_edge", None))
            d.update(edge_basis=basis, edge_meaning=meaning,
                     legal_effect_verified=False)
            out.append(d)
        return out

    card["related"] = edges(
        """SELECT e.relation, e.matter, e.note, e.source, e.source_url,
                  a2.id, a2.name, a2.kind
           FROM authority_edge e JOIN authority a2 ON a2.id = e.to_authority
           WHERE e.from_authority = ?""")
    card["related_incoming"] = edges(
        """SELECT e.relation, e.matter, e.note, e.source, e.source_url,
                  a2.id, a2.name, a2.kind
           FROM authority_edge e JOIN authority a2 ON a2.id = e.from_authority
           WHERE e.to_authority = ?""")
    court_id = court_ids_for(conn, [authority_id]).get(authority_id)
    court = None
    if court_id and has_table(conn, "court"):
        row = conn.execute("SELECT * FROM court WHERE court_id = ?",
                           (court_id,)).fetchone()
        court = dict(row) if row else None
    scopes = [("authority", str(authority_id), "authority")]
    if court_id:
        scopes.append(("court", court_id, "court"))
    # all matters: the card is about the entity, not about one question
    active, inactive = aggregate_caveats(conn, scopes, None, any_matter=True)
    comp = []
    if has_table(conn, "competence"):
        comp = [dict(r) for r in conn.execute(
            """SELECT kind, level, rank, COUNT(*) AS areas FROM competence
               WHERE authority_id = ? GROUP BY kind, level, rank
               ORDER BY kind, level, rank""", (authority_id,))]
    chains = [dict(r) for r in conn.execute(
        """SELECT matter, role, position, COUNT(*) AS places
           FROM court_chain WHERE authority_id = ?
           GROUP BY matter, role, position ORDER BY matter, position""",
        (authority_id,))]
    identity_evidence = {
        "evidence_id": f"evidence:record:{card['provenance']['source']}:"
                       f"{authority_id}",
        "kind": "register_record",
        "role": "identity_and_contact",
        "register": {"id": card["provenance"]["source"],
                     "name": REGISTERS.get(card["provenance"]["source"] or "",
                                           {}).get("name")},
        "source_url": card["provenance"]["url"],
        "observed_on": card["provenance"]["fetched_at"],
        "supports": ["identity", "contact"],
        "does_not_establish": ["territorial_competence",
                               "subject_matter_competence"],
        "legal_effect_verified": False}
    warnings = caveat_warnings(active, inactive)
    warnings.append("The record establishes identity and contact data only; "
                    "competence is served per assignment by "
                    "/resolve/competence and /resolve/court.")
    if card.get("valid_to"):
        warnings.insert(0, "this authority is retired in this build")
    status = "ok"
    return envelope(
        status=status, total=1,
        searched=[{"source": "authority", "status": "ok", "count": 1},
                  {"source": "authority_edge", "status": "ok",
                   "count": len(card["related"]) +
                   len(card["related_incoming"])},
                  {"source": "caveat", "status": "ok",
                   "count": len(active) + len(inactive)}],
        evidence=[identity_evidence], warnings=warnings,
        **card, court_id=court_id, court=court,
        competence_summary=comp, court_chain_summary=chains,
        caveats=active, inactive_caveats=inactive,
        critical_warning=critical_warning(active), **common)
