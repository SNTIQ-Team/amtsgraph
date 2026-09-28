"""Courts as first-class nodes, joinable with decision corpora.

Court id scheme (stable across builds; authority ids are not):

    court:de:<XJustiz-ID>        e.g. court:de:A1000  Bundesgerichtshof
    court:eu:<ECLI court code>   e.g. court:eu:C      Court of Justice

The node set is the official GDS.Gerichte codelist (XJustiz). Each node maps
court id <-> official name <-> XJustiz id <-> ECLI court code (where known)
<-> Amtsgraph authority rows (contact cards, court chains). ECLI codes of
Land courts are NOT guessed from names; they stay null until observed.
"""
from __future__ import annotations

import re
import sqlite3

from api.contract import ContractError, envelope, snapshot_info
from api.competence import (aggregate_caveats, caveat_warnings,
                            critical_warning, has_table, normalize)

COURT_ID = re.compile(r"court:(de:[A-Z]\d{4}[A-Z0-9]*|eu:[A-Z]{1,3})")

# Common German court abbreviations -> official name heads.
_ABBREVIATIONS = [
    ("bverfg", "bundesverfassungsgericht"), ("bgh", "bundesgerichtshof"),
    ("bverwg", "bundesverwaltungsgericht"), ("bfh", "bundesfinanzhof"),
    ("bag", "bundesarbeitsgericht"), ("bsg", "bundessozialgericht"),
    ("bpatg", "bundespatentgericht"),
    ("bayobl g", "bayerisches oberstes landesgericht"),
    ("bayoblg", "bayerisches oberstes landesgericht"),
    ("bayvgh", "bayerischer verwaltungsgerichtshof"),
    ("kg", "kammergericht"), ("ag", "amtsgericht"), ("lg", "landgericht"),
    ("olg", "oberlandesgericht"), ("vg", "verwaltungsgericht"),
    ("ovg", "oberverwaltungsgericht"), ("vgh", "verwaltungsgerichtshof"),
    ("sg", "sozialgericht"), ("lsg", "landessozialgericht"),
    ("arbg", "arbeitsgericht"), ("lag", "landesarbeitsgericht"),
    ("fg", "finanzgericht"),
]
# "BGH 9. Zivilsenat", "BVerfG 2. Senat 1. Kammer", "OLG München 7. ZS"
_PANEL = re.compile(
    r"\s+(\d+[a-z]?\.\s*)?(grosser\s+|vereinigte\s+)?"
    r"(\w*senat\w*|\w*kammer\w*|zs|strafsenat)\b.*$")

MAPPING_FIELDS = ("court_id", "jurisdiction", "official_name", "xjustiz_id",
                  "ecli_court_code", "ecli_basis", "kind", "level",
                  "land_code", "status", "unit_type", "parent_court_id",
                  "source", "observed_on")


def normalize_court_name(name: str) -> str:
    n = normalize(name.replace("-", " "))
    n = _PANEL.sub("", n).strip()
    head, _, rest = n.partition(" ")
    for abbr, full in _ABBREVIATIONS:
        if abbr == head or (" " in abbr and n.startswith(abbr + " ")):
            tail = n[len(abbr):].strip() if " " in abbr else rest
            return " ".join(x for x in (full, tail) if x)
    return n


def _require_registry(conn) -> None:
    if not has_table(conn, "court"):
        raise ContractError("unavailable",
                            "the court registry is not part of this build",
                            failed_components=["court_registry"])


def _record(conn, row, *, with_links: bool = True) -> dict:
    d = {k: row[k] for k in MAPPING_FIELDS}
    ecli = d["ecli_court_code"]
    country = "EU" if d["jurisdiction"] == "EU" else "DE"
    d["ecli_prefix"] = f"ECLI:{country}:{ecli}:" if ecli else None
    d["ecli_status"] = "known" if ecli else "unknown"
    if with_links:
        d["aliases"] = [dict(r) for r in conn.execute(
            """SELECT alias, alias_type, source FROM court_alias
               WHERE court_id = ? ORDER BY alias_type, alias""",
            (d["court_id"],))]
        d["authority_ids"] = [r[0] for r in conn.execute(
            """SELECT authority_id FROM court_authority WHERE court_id = ?
               ORDER BY role DESC, authority_id""", (d["court_id"],))]
    return d


def court_mapping(conn: sqlite3.Connection, *, limit: int = 5000,
                  offset: int = 0, request_scope: dict | None = None) -> dict:
    """Bulk court id <-> name <-> XJustiz <-> ECLI mapping for ingestion."""
    _require_registry(conn)
    snapshot = snapshot_info(conn)
    if not 1 <= limit <= 5000 or offset < 0:
        raise ContractError("invalid_argument", "limit 1..5000, offset >= 0",
                            invalid_arguments=["limit", "offset"])
    total = conn.execute("SELECT COUNT(*) FROM court").fetchone()[0]
    aliases: dict[str, list] = {}
    for r in conn.execute("SELECT court_id, alias, alias_type FROM court_alias"
                          " ORDER BY court_id, alias_type, alias"):
        aliases.setdefault(r[0], []).append(
            {"alias": r[1], "alias_type": r[2]})
    links: dict[str, list] = {}
    for r in conn.execute("""SELECT court_id, authority_id, role
                             FROM court_authority
                             ORDER BY court_id, role DESC, authority_id"""):
        links.setdefault(r[0], []).append(r[1])
    rows = conn.execute("SELECT * FROM court ORDER BY court_id LIMIT ? OFFSET ?",
                        (limit, offset)).fetchall()
    courts = []
    for row in rows:
        d = _record(conn, row, with_links=False)
        d["aliases"] = aliases.get(d["court_id"], [])
        d["authority_ids"] = links.get(d["court_id"], [])
        courts.append(d)
    return envelope(
        operation="court_mapping", status="ok", request_scope=request_scope or {},
        snapshot=snapshot, applied_constraints={"limit": limit, "offset": offset},
        searched=[{"source": "court", "status": "ok", "count": total}],
        warnings=["authority_ids are local to one build; court_id is the "
                  "durable key",
                  "ecli_court_code is null where this build does not know "
                  "it; it is never derived from the court name"],
        total=total,
        id_scheme={"de": "court:de:<XJustiz-ID>",
                   "eu": "court:eu:<ECLI court code>",
                   "decision_fallback": "court:<...>/<YYYY-MM-DD>/<first Az>"},
        courts=courts)


def court_detail(conn: sqlite3.Connection, court_id: str, *,
                 request_scope: dict | None = None) -> dict:
    _require_registry(conn)
    snapshot = snapshot_info(conn)
    common = dict(operation="court_detail", request_scope=request_scope or {},
                  snapshot=snapshot,
                  applied_constraints={"court_id": court_id})
    if not COURT_ID.fullmatch(court_id):
        raise ContractError("invalid_argument",
                            "court_id must be court:de:<XJustiz-ID> or "
                            "court:eu:<ECLI code>",
                            invalid_arguments=["court_id"])
    row = conn.execute("SELECT * FROM court WHERE court_id = ?",
                       (court_id,)).fetchone()
    if not row:
        return envelope(status="not_in_corpus", total=0,
                        searched=[{"source": "court", "status": "ok",
                                   "count": 0}],
                        warnings=["unknown court id"], **common)
    court = _record(conn, row)
    authorities = [dict(r) for r in conn.execute(
        """SELECT a.id, a.name, a.kind, ca.role, a.valid_to IS NULL AS active
           FROM court_authority ca JOIN authority a ON a.id = ca.authority_id
           WHERE ca.court_id = ? ORDER BY ca.role DESC, a.id""", (court_id,))]
    units = [dict(r) for r in conn.execute(
        """SELECT court_id, official_name, status FROM court
           WHERE parent_court_id = ? ORDER BY court_id""", (court_id,))]
    ids = [a["id"] for a in authorities]
    chains = []
    if ids:
        marks = ",".join("?" * len(ids))
        chains = [dict(r) for r in conn.execute(
            f"""SELECT matter, role, position, COUNT(*) AS places
                FROM court_chain WHERE authority_id IN ({marks})
                GROUP BY matter, role, position ORDER BY matter, position""",
            ids)]
    scopes = [("court", court_id, "court")] + [
        ("authority", str(a), "authority") for a in ids]
    active, inactive = aggregate_caveats(conn, scopes, None, any_matter=True)
    evidence = [{
        "evidence_id": f"evidence:codelist:{row['source']}:{court_id}",
        "kind": "register_record", "role": "identity",
        "register": {"id": row["source"]},
        "observed_on": row["observed_on"],
        "supports": ["identity", "xjustiz_id"],
        "does_not_establish": ["territorial_competence"],
        "legal_effect_verified": False}]
    if row["ecli_court_code"]:
        evidence.append({
            "evidence_id": f"evidence:ecli:{court_id}", "kind": "curated_mapping",
            "role": "ecli_court_code", "basis": row["ecli_basis"],
            "source_url": row["ecli_source_url"],
            "supports": ["ecli_court_code"], "legal_effect_verified": False})
    warnings = caveat_warnings(active, inactive)
    if row["status"] == "dissolved":
        warnings.insert(0, "this court is dissolved (codelist); decisions "
                           "may still reference it")
    return envelope(
        status="ok", total=1,
        searched=[{"source": "court", "status": "ok", "count": 1},
                  {"source": "court_authority", "status": "ok",
                   "count": len(authorities)}],
        evidence=evidence, warnings=warnings,
        court=court, authorities=authorities, units=units,
        court_chain_summary=chains,
        decision_join={"court_id": court_id,
                       "ecli_prefix": court["ecli_prefix"],
                       "decision_id_fallback": f"{court_id}/<YYYY-MM-DD>/"
                                               f"<first Az>"},
        caveats=active, inactive_caveats=inactive,
        critical_warning=critical_warning(active), **common)


def court_lookup(conn: sqlite3.Connection, *, name: str | None = None,
                 ecli: str | None = None, xjustiz: str | None = None,
                 authority_id: int | None = None, land: str | None = None,
                 request_scope: dict | None = None) -> dict:
    """Map a decision's court designation to court ids, never guessing."""
    _require_registry(conn)
    snapshot = snapshot_info(conn)
    given = [k for k, v in (("name", name), ("ecli", ecli),
                            ("xjustiz", xjustiz),
                            ("authority_id", authority_id)) if v is not None]
    if len(given) != 1:
        raise ContractError("invalid_argument",
                            "give exactly one of name, ecli, xjustiz, "
                            "authority_id", invalid_arguments=given or ["name"])
    if land is not None and not re.fullmatch(r"\d{2}", land):
        raise ContractError("invalid_argument", "land is a 2-digit code",
                            invalid_arguments=["land"])
    basis, rows, note = given[0], [], []
    if ecli is not None:
        m = re.fullmatch(r"ECLI:(DE|EU):([A-Za-z0-9]+)(:.*)?", ecli.strip(),
                         re.I)
        if not m:
            raise ContractError("invalid_argument",
                                "ecli must look like ECLI:DE:<court>:...",
                                invalid_arguments=["ecli"])
        rows = conn.execute(
            """SELECT * FROM court WHERE jurisdiction = ?
               AND lower(ecli_court_code) = lower(?)""",
            (m.group(1).upper(), m.group(2))).fetchall()
        if not rows:
            note.append("this build knows ECLI court codes of federal and EU "
                        "courts only; Land court codes are not guessed")
    elif xjustiz is not None:
        rows = conn.execute("SELECT * FROM court WHERE xjustiz_id = ?",
                            (xjustiz.strip().upper(),)).fetchall()
    elif authority_id is not None:
        rows = conn.execute(
            """SELECT c.* FROM court c JOIN court_authority ca
               ON ca.court_id = c.court_id WHERE ca.authority_id = ?""",
            (authority_id,)).fetchall()
    else:
        if not name.strip() or len(name) > 200:
            raise ContractError("invalid_argument", "name must be 1..200 chars",
                                invalid_arguments=["name"])
        n = normalize_court_name(name)
        rows = conn.execute(
            """SELECT DISTINCT c.* FROM court c
               LEFT JOIN court_alias al ON al.court_id = c.court_id
               WHERE c.name_norm = ? OR al.alias_norm = ?""",
            (n, n)).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT * FROM court WHERE name_norm LIKE ? || ' %'",
                (n,)).fetchall()
            if rows:
                note.append("matched by official-name prefix")
        basis = f"name:{n}"
    if land is not None:
        rows = [r for r in rows if r["land_code"] == land
                or r["level"] != "land"]
    courts = [_record(conn, r) for r in rows]
    related: list = []
    top = [c for c in courts if c["unit_type"] == "court"]
    if len(courts) > 1 and len(top) == 1:
        # departments share the court's normalized name; the court itself
        # is the unit a decision is attributed to
        related = [c for c in courts if c["unit_type"] != "court"]
        courts = top
        note.append("departments with the same normalized name are listed "
                    "in related_units")
    status = ("ok" if len(courts) == 1 else "ambiguous_identity" if courts
              else "not_found_in_searched_scope")
    if status == "ambiguous_identity":
        note.insert(0, f"{len(courts)} courts match; narrow with land= or "
                       f"use the XJustiz id — none is selected implicitly")
    return envelope(
        operation="court_lookup", status=status,
        request_scope=request_scope or {}, snapshot=snapshot,
        applied_constraints={"basis": basis, "land": land},
        searched=[{"source": "court + court_alias", "status": "ok",
                   "count": len(courts)}],
        warnings=note, total=len(courts),
        court_id=courts[0]["court_id"] if status == "ok" else None,
        court=courts[0] if status == "ok" else None,
        candidates=courts if status == "ambiguous_identity" else [],
        related_units=related)


def court_search(conn: sqlite3.Connection, *, q: str | None = None,
                 kind: str | None = None, land: str | None = None,
                 level: str | None = None, status: str | None = None,
                 limit: int = 50, offset: int = 0,
                 request_scope: dict | None = None) -> dict:
    _require_registry(conn)
    snapshot = snapshot_info(conn)
    if not 1 <= limit <= 200 or offset < 0:
        raise ContractError("invalid_argument", "limit 1..200, offset >= 0",
                            invalid_arguments=["limit", "offset"])
    if level is not None and level not in ("federal", "land", "eu"):
        raise ContractError("invalid_argument", "level: federal|land|eu",
                            invalid_arguments=["level"])
    if status is not None and status not in ("active", "dissolved"):
        raise ContractError("invalid_argument", "status: active|dissolved",
                            invalid_arguments=["status"])
    where, args = [], []
    if q:
        where.append("name_norm LIKE '%' || ? || '%'")
        args.append(normalize_court_name(q))
    for col, val in (("kind", kind), ("land_code", land), ("level", level),
                     ("status", status)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    sql = " WHERE " + " AND ".join(where) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM court{sql}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM court{sql} ORDER BY court_id LIMIT ? OFFSET ?",
        (*args, limit, offset)).fetchall()
    return envelope(
        operation="court_search",
        status="ok" if total else "not_found_in_searched_scope",
        request_scope=request_scope or {}, snapshot=snapshot,
        applied_constraints={"q": q, "kind": kind, "land": land,
                             "level": level, "status": status,
                             "limit": limit, "offset": offset},
        searched=[{"source": "court", "status": "ok", "count": total}],
        total=total, courts=[_record(conn, r) for r in rows],
        truncated=offset + len(rows) < total)
