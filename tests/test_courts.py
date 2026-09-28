"""Courts as first-class nodes: stable ids joinable with decisions."""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from api.courts import normalize_court_name
from tests.conftest import ROOT

sys.path.insert(0, str(ROOT / "pipeline"))


def test_chain_entries_and_assignments_carry_court_ids(client):
    body = client.get("/resolve/court?place_id=place:jz:12345:EXAMPLE-A"
                      "&matter=sozial").json()
    assert [c["court_id"] for c in body["chain"]] == ["court:de:Q1101",
                                                     "court:de:Q2100"]
    assert body["assignments"][0]["court_id"] == "court:de:Q1101"


def test_court_detail_maps_id_name_xjustiz_ecli_and_authorities(client):
    response = client.get("/courts/court:de:Q1101")
    body = response.json()
    assert response.status_code == 200 and body["status"] == "ok"
    court = body["court"]
    assert court["official_name"] == "Sozialgericht A"
    assert court["xjustiz_id"] == "Q1101"
    assert court["ecli_court_code"] is None
    assert court["ecli_status"] == "unknown"
    assert court["authority_ids"] == [1, 4]           # main first
    assert [u["court_id"] for u in body["units"]] == ["court:de:Q1101G"]
    assert body["decision_join"]["court_id"] == "court:de:Q1101"
    assert {"SYN-COURT", "SYN-AUTH-COURT"} <= {c["text_de"]
                                               for c in body["caveats"]}


def test_federal_court_publishes_its_ecli_prefix(client):
    court = client.get("/courts/court:de:Q9000").json()["court"]
    assert court["ecli_prefix"] == "ECLI:DE:SYNB:"
    hit = client.get("/courts/lookup?ecli=ECLI:DE:SYNB:2020:123").json()
    assert hit["status"] == "ok" and hit["court_id"] == "court:de:Q9000"


def test_unknown_ecli_court_code_is_not_guessed(client):
    response = client.get("/courts/lookup?ecli=ECLI:DE:LGXYZ:2020:1")
    body = response.json()
    assert response.status_code == 404
    assert body["status"] == "not_found_in_searched_scope"
    assert any("not guessed" in w for w in body["warnings"])


def test_homonymous_courts_are_ambiguous(client):
    body = client.get("/courts/lookup?name=AG%20Twin").json()
    assert body["status"] == "ambiguous_identity"
    assert body["court_id"] is None
    assert {c["court_id"] for c in body["candidates"]} == {
        "court:de:Q3100", "court:de:Q4100"}


def test_lookup_by_short_name_panel_suffix_and_register_card(client):
    assert client.get("/courts/lookup?name=SynB%203.%20Senat").json()[
        "court_id"] == "court:de:Q9000"
    assert client.get("/courts/lookup?authority_id=4").json()[
        "court_id"] == "court:de:Q1101"
    assert client.get("/courts/lookup?xjustiz=q1102").json()[
        "court_id"] == "court:de:Q1102"


def test_mapping_is_complete_and_bounded(client):
    body = client.get("/courts/mapping").json()
    assert body["status"] == "ok" and body["total"] == 7
    ids = [c["court_id"] for c in body["courts"]]
    assert ids == sorted(ids)
    q1101 = next(c for c in body["courts"] if c["court_id"] == "court:de:Q1101")
    assert q1101["authority_ids"] == [1, 4]
    assert body["id_scheme"]["de"] == "court:de:<XJustiz-ID>"
    page = client.get("/courts/mapping?limit=2&offset=2").json()
    assert [c["court_id"] for c in page["courts"]] == ids[2:4]


@pytest.mark.parametrize("court_id,code", [("court:de:Q7777", 404),
                                           ("berlin", 400)])
def test_court_detail_errors_are_typed(client, court_id, code):
    assert client.get(f"/courts/{court_id}").status_code == code


def test_registry_absent_is_unavailable_not_empty(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api import main
    path = tmp_path / "old.db"
    sqlite3.connect(path).executescript(
        "CREATE TABLE build_info(key TEXT, value TEXT);")
    monkeypatch.setattr(main, "DB_PATH", path)
    response = TestClient(main.app).get("/courts/mapping")
    assert response.status_code == 503
    assert response.json()["failed_components"] == ["court_registry"]
    assert response.json()["total"] is None


@pytest.mark.parametrize("raw,expected", [
    ("BGH 9. Zivilsenat", "bundesgerichtshof"),
    ("BVerfG 2. Senat 1. Kammer", "bundesverfassungsgericht"),
    ("OLG München", "oberlandesgericht muenchen"),
    ("LSG Nordrhein-Westfalen 7. Senat", "landessozialgericht nordrhein westfalen"),
    ("Amtsgericht Köln", "amtsgericht koeln"),
])
def test_court_name_normalization(raw, expected):
    assert normalize_court_name(raw) == expected


# ------------------------------------------------------------- pipeline

def test_pipeline_builds_court_nodes_from_the_codelist(tmp_path):
    import build_db
    snap = tmp_path / "2026-06-11"
    snap.mkdir()
    rows = [
        {"code": "Q1000", "wert": "Oberlandesgericht Synth"},
        {"code": "Q1101", "wert": "Amtsgericht Synth"},
        {"code": "Q1101G", "wert": "Amtsgericht Synth -Grundbuchamt-"},
        {"code": "Q1102", "wert": "aufgelöst-Amtsgericht Alt"},
        {"code": "Q1000S", "wert": "Generalstaatsanwaltschaft Synth"},
        {"code": "Q6001", "wert": "Justizvollzugsanstalt Synth"},
        {"code": "Q6002", "wert": "Führungsaufsichtsstelle beim Landgericht"},
        {"code": "Q6003", "wert": "Bundessynthgericht"},
    ]
    (snap / "gerichte.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    ident = tmp_path / "ident.yaml"
    ident.write_text("""
xjustiz_land_prefix: {Q: "99"}
federal_courts:
  - {xjustiz: Q6003, short: BSyn, kind: bundesgericht, ecli: BSyn,
     ecli_basis: federal_portal_convention}
federal_ecli_source_url: https://example.invalid/
eu_courts: []
""", encoding="utf-8")
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "db" / "schema.sql").read_text())
    for aid, name, code in ((1, "Amtsgericht Synth", "Q1101"),
                            (2, "Amtsgericht Synth - Insolvenzabteilung -",
                             "Q1101"),
                            (3, "AG Nur Register", "Q5555")):
        db.execute("""INSERT INTO authority (id, kind, name, name_norm,
                        source, fetched_at)
                      VALUES (?, 'amtsgericht', ?, ?, 'justizadressen',
                              '2026-06-10')""", (aid, name, name.lower()))
        db.execute("INSERT INTO authority_external_id VALUES (?,?,?)",
                   (aid, "xjustiz", code))
    assert build_db.load_courts(db, snap, identity_path=ident) == 6
    got = {r[0]: r[1:] for r in db.execute(
        """SELECT court_id, kind, level, land_code, status, unit_type,
                  parent_court_id, ecli_court_code, source FROM court""")}
    # prosecutors, prisons and a supervision office are not courts
    assert set(got) == {"court:de:Q1000", "court:de:Q1101",
                        "court:de:Q1101G", "court:de:Q1102",
                        "court:de:Q6003", "court:de:Q5555"}
    assert got["court:de:Q1101G"][4:6] == ("department", "court:de:Q1101")
    assert got["court:de:Q1102"][3] == "dissolved"
    assert got["court:de:Q6003"][1:3] == ("federal", None)
    assert got["court:de:Q6003"][6] == "BSyn"
    assert got["court:de:Q1000"][2] == "99"
    assert got["court:de:Q5555"][7] == "justizadressen"
    roles = dict(db.execute(
        "SELECT authority_id, role FROM court_authority "
        "WHERE court_id='court:de:Q1101'"))
    assert roles == {1: "main", 2: "department"}


def test_validation_gates_competence_without_provenance(tmp_path):
    import validate
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "db" / "schema.sql").read_text())
    db.execute("""INSERT INTO authority (id, kind, name, name_norm, source,
                    fetched_at) VALUES (1,'jobcenter','J','j','pvog','x')""")
    db.execute("""INSERT INTO competence (authority_id, kind, level, area,
                    rank) VALUES (1,'jobcenter','kreis','99001',0)""")
    errors = validate.check(db)
    assert any("without source" in e for e in errors)


def test_assignment_evidence_loader_requires_a_source(tmp_path):
    import build_db
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "db" / "schema.sql").read_text())
    good = tmp_path / "ok.yaml"
    good.write_text("""entries:
  - evidence_kind: statutory_assignment
    subject: court_matter
    matter: mahn
    territory: {level: land, key: "99"}
    citation: "§ 1 Synthetic"
    source_url: https://example.invalid/law
    observed_on: 2026-09-28
""", encoding="utf-8")
    assert build_db.load_assignment_evidence(db, good) == 1
    bad = tmp_path / "bad.yaml"
    bad.write_text(good.read_text().replace("https://example.invalid/law",
                                            "http://insecure"))
    with pytest.raises(ValueError):
        build_db.load_assignment_evidence(db, bad)


def test_pvog_loader_scopes_caveats_and_records_assignment_provenance(
        tmp_path):
    """Audit fix: kind-specific caveats no longer attach to every answer."""
    import build_db
    snap = tmp_path / "2026-06-10"
    snap.mkdir()
    units = [{"oe_id": f"OE.{i}", "detail": {"title": title}}
             for i, title in ((1, "Sozialamt Eins"), (2, "Sozialamt Zwei"),
                              (3, "Ordnungsamt; Ausländerbehörde"))]
    comps = [{"ags": "09001001", "ars": "090010010000", "kind":
              "asylblg_behoerde", "oe_id": "OE.1", "oe_title":
              "Sozialamt Eins", "lb_id": "LB.9"},
             {"ags": "09001001", "ars": "090010010000", "kind":
              "asylblg_behoerde", "oe_id": "OE.2", "oe_title":
              "Sozialamt Zwei", "lb_id": "LB.9"},
             {"ags": "09001001", "ars": "090010010000", "kind":
              "auslaenderbehoerde", "oe_id": "OE.3", "oe_title":
              "Ordnungsamt; Ausländerbehörde", "lb_id": "LB.7"}]
    (snap / "units.jsonl").write_text(
        "\n".join(json.dumps(u) for u in units), encoding="utf-8")
    (snap / "competences.jsonl").write_text(
        "\n".join(json.dumps(c) for c in comps), encoding="utf-8")
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "db" / "schema.sql").read_text())
    db.execute("INSERT INTO land VALUES ('09','Bayern')")
    db.execute("INSERT INTO kreis VALUES ('09001','09','Synth','Landkreis',"
               "NULL)")
    build_db.load_pvog(db, snap)
    caveats = db.execute("SELECT scope_level, matter FROM caveat "
                         "ORDER BY scope_level").fetchall()
    assert caveats == [("gemeinde", "asylblg_behoerde"),
                       ("kreis", "asylblg_behoerde")]
    rows = db.execute("""SELECT kind, source, observed_on, source_ref,
                            derivation FROM competence ORDER BY kind""").fetchall()
    assert ("auslaenderbehoerde", "pvog", "2026-06-10", "LB.7@090010010000",
            None) in rows
    assert {r[4] for r in rows if r[0] == "asylblg_behoerde"} == {
        "asylblg_title_filter"}
    assert {r[0] for r in db.execute("SELECT DISTINCT fetched_at FROM "
                                     "authority")} == {"2026-06-10"}
