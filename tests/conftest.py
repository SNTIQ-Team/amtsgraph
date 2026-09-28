"""Synthetic Amtsgraph store built from the real schema (no real assertions).

Everything here is invented: Land 99, PLZ 12345/12346, XJustiz codes with the
unused prefix Q. The fixture deliberately contains the collisions and scoped
caveats that the resolvers must handle (paper §8.1, §8.2).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SNAPSHOTS = {"openplz": "2026-06-10", "justiz": "2026-06-10",
             "pvog": "2026-06-10", "xrepository": "2026-06-11"}

A_COURT_1 = "assignment:court:12345:EXAMPLE-A:sozial:court:1"
A_OFFICE = "assignment:competence:10:jobcenter:gemeinde:99001001"

# (id, scope_level, scope_key, matter, severity, text, valid_from, valid_until)
CAVEATS = [
    (1, "authority", "1", "sozial", "warn", "SYN-AUTH-COURT", None, None),
    (2, "authority", "10", None, "block", "SYN-AUTH-OFFICE", None, None),
    (3, "jz_place", "12345|EXAMPLE-A", None, "warn", "SYN-PLACE", None, None),
    (4, "plz", "12345", None, "info", "SYN-PLZ", None, None),
    (5, "gemeinde", "99001001", None, "warn", "SYN-MUNI", None, None),
    (6, "kreis", "99001", None, "info", "SYN-KREIS", None, None),
    (7, "land", "99", None, "info", "SYN-LAND", None, None),
    (8, "matter", "sozial", "sozial", "warn", "SYN-MATTER", None, None),
    (9, "assignment", A_COURT_1, None, "warn", "SYN-ASSIGN-COURT", None, None),
    (10, "assignment", A_OFFICE, None, "warn", "SYN-ASSIGN-OFFICE", None, None),
    (11, "court", "court:de:Q1101", None, "warn", "SYN-COURT", None, None),
    (12, "global", "*", None, "info", "SYN-GLOBAL", None, None),
    (13, "authority", "1", "zivil", "block", "SYN-OTHER-MATTER", None, None),
    (14, "kreis", "99001", "asylblg_behoerde", "warn", "SYN-KIND-NARROW",
     None, None),
    (15, "authority", "1", None, "warn", "SYN-EXPIRED", "1990-01-01",
     "2000-01-01"),
    (16, "gemeinde", "99001001", None, "warn", "SYN-FUTURE", "2999-01-01",
     None),
    (17, "authority", "10", None, "warn", "SYN-EXPIRED-OFFICE", None,
     "2001-01-01"),
    (18, "jz_place", "12345|EXAMPLE-A", "sozial", "info", "SYN-GAP-SOZIAL",
     None, None),
]


def build_store(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.executescript((ROOT / "db" / "schema.sql").read_text())
    x = db.execute
    x("INSERT INTO land VALUES ('99','Testland')")
    x("INSERT INTO kreis VALUES ('99001','99','Testkreis','Landkreis',NULL)")
    for ags, name in (("99001001", "Example"), ("99001002", "Other")):
        x("INSERT INTO gemeinde VALUES (?,?,?,?,?,?,?,?)",
          (ags, ags + "0000", "99001", name, name, name.lower(), "Gemeinde",
           100))
    # one PLZ spans two municipalities
    x("INSERT INTO gemeinde_plz VALUES ('99001001','12345')")
    x("INSERT INTO gemeinde_plz VALUES ('99001002','12345')")
    x("INSERT INTO gemeinde_plz VALUES ('99001002','12346')")
    # court-register collision: same PLZ, same place name, two keys
    for plz, ortk, ort, ags in (("12345", "EXAMPLE-B", "Example", "99001001"),
                                ("12345", "EXAMPLE-A", "Example", "99001001"),
                                ("12346", "SOLO", "Solo", "99001002")):
        x("INSERT INTO jz_place VALUES (?,?,?,?,?,?,?)",
          (plz, ortk, ort, ort.lower(), "gs", None, ags))
    for aid, kind, name, src in (
            (1, "sozialgericht", "Sozialgericht A", "justizadressen"),
            (2, "sozialgericht", "Sozialgericht B", "justizadressen"),
            (3, "landessozialgericht", "Landessozialgericht Q",
             "justizadressen"),
            (4, "sozialgericht", "Sozialgericht A - Zweigstelle -",
             "justizadressen"),
            (10, "jobcenter", "Jobcenter Example", "pvog"),
            (11, "ministerium", "Supervising Ministry", "pvog"),
            (12, "sozialamt", "Sozialamt One", "pvog"),
            (13, "sozialamt", "Sozialamt Two", "pvog"),
            (14, "standesamt", "Standesamt Curated", "override")):
        x("""INSERT INTO authority (id, kind, name, name_norm, street, source,
               source_url, fetched_at) VALUES (?,?,?,?,?,?,?,?)""",
          (aid, kind, name, name.lower(), "Teststraße 1", src,
           f"https://example.invalid/{aid}", "2026-06-10"))
    for aid, code in ((1, "Q1101"), (2, "Q1102"), (3, "Q2100"), (4, "Q1101")):
        x("INSERT INTO authority_external_id VALUES (?,?,?)",
          (aid, "xjustiz", code))
    for plz, ortk, pos, aid, note in (
            ("12345", "EXAMPLE-A", 1, 1, "Synthetic chamber note."),
            ("12345", "EXAMPLE-A", 2, 3, None),
            ("12345", "EXAMPLE-B", 1, 2, None),
            ("12346", "SOLO", 1, 2, None)):
        x("INSERT INTO court_chain VALUES (?,?,?,?,?,?,?)",
          (plz, ortk, "sozial", pos, aid, "court", note))
    x("""INSERT INTO authority_edge (from_authority, to_authority, relation,
           matter, note, source, source_url)
         VALUES (1, 3, 'appeal', 'sozial', 'derived:12345|EXAMPLE-A',
                 'justizadressen', 'https://example.invalid/chain')""")
    comp = """INSERT INTO competence (authority_id, kind, level, area, rank,
                source, observed_on, source_ref, source_url, derivation,
                review_due) VALUES (?,?,?,?,?,?,?,?,?,?,?)"""
    x(comp, (10, "jobcenter", "gemeinde", "99001001", 0, "pvog", "2026-06-10",
             "LB.1@990010010000", "pvog:organisationunit:OE.10", None, None))
    x(comp, (11, "jobcenter", "kreis", "99001", 1, "pvog", "2026-06-10",
             "LB.1@990010000000", "pvog:organisationunit:OE.11",
             "asylblg_title_filter_higher_level", None))
    for aid in (12, 13):
        x(comp, (aid, "sozialamt", "gemeinde", "99001001", 0, "pvog",
                 "2026-06-10", f"LB.2@{aid}", None, None, None))
    x(comp, (14, "standesamt", "gemeinde", "99001001", 0, "override",
             "2020-01-01", "override:2020-01-01-test.yaml",
             "https://example.invalid/curated", None, "2020-12-31"))
    for cid in CAVEATS:
        x("""INSERT INTO caveat (id, scope_level, scope_key, matter, severity,
               text_de, source, valid_from, valid_until_exclusive)
             VALUES (?,?,?,?,?,?,'fixture',?,?)""", cid)
    for code, name, kind, unit, parent in (
            ("Q1101", "Sozialgericht A", "sozialgericht", "court", None),
            ("Q1101G", "Sozialgericht A - Registerstelle -", "sozialgericht",
             "department", "court:de:Q1101"),
            ("Q1102", "Sozialgericht B", "sozialgericht", "court", None),
            ("Q2100", "Landessozialgericht Q", "landessozialgericht", "court",
             None),
            ("Q3100", "Amtsgericht Twin", "amtsgericht", "court", None),
            ("Q4100", "Amtsgericht Twin", "amtsgericht", "court", None)):
        x("""INSERT INTO court VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (f"court:de:{code}", "DE", code, name, name.lower().replace(
              " - ", " - "), kind, "land", "99", "active", unit, parent,
           None, None, None, "xrepository:gds.gerichte", "2026-06-11"))
    x("""INSERT INTO court VALUES ('court:de:Q9000','DE','Q9000',
           'Synthetisches Bundesgericht','synthetisches bundesgericht',
           'bundesgericht','federal',NULL,'active','court',NULL,'SYNB',
           'federal_portal_convention','https://example.invalid/ecli',
           'xrepository:gds.gerichte','2026-06-11')""")
    for cid, aid, role in (("court:de:Q1101", 1, "main"),
                           ("court:de:Q1101", 4, "department"),
                           ("court:de:Q1102", 2, "main"),
                           ("court:de:Q2100", 3, "main")):
        x("INSERT INTO court_authority VALUES (?,?,?,'xjustiz_id')",
          (cid, aid, role))
    x("""INSERT INTO court_alias VALUES ('court:de:Q9000','SynB','synb',
           'short','fixture')""")
    ev = """INSERT INTO assignment_evidence (evidence_kind, subject, matter,
              territory_level, territory_key, court_id, norm_id, citation,
              passage, source_url, observed_on, valid_from,
              valid_until_exclusive, derived_by, legal_effect_verified, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    x(ev, ("statutory_assignment", "court_matter", "sozial", "land", "99",
           "court:de:Q1101", "fixture/norm/1", "§ 1 Synthetic Ordinance",
           "SYNTHETIC TEST TEXT — NOT LAW.", "https://example.invalid/law",
           "2026-09-28", "2020-01-01", None, "user", 0, None))
    x(ev, ("statutory_assignment", "court_matter", "sozial", "land", "98",
           None, None, "§ 9 Other Land", None, "https://example.invalid/other",
           "2026-09-28", None, None, "user", 0, None))
    x("INSERT INTO build_info VALUES ('built_at','2026-09-28T00:00:00+00:00')")
    x("INSERT INTO build_info VALUES ('snapshots', ?)",
      (json.dumps(SNAPSHOTS),))
    x("INSERT INTO build_info VALUES ('schema_version','2')")
    db.commit()
    db.close()
    return path


@pytest.fixture
def store(tmp_path):
    return build_store(tmp_path / "atlas.db")


@pytest.fixture
def client(store, monkeypatch):
    from fastapi.testclient import TestClient
    from api import main
    monkeypatch.setattr(main, "DB_PATH", store)
    return TestClient(main.app)
