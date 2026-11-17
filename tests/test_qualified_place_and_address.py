import sqlite3
import json
from contextlib import closing

import pytest
from tests.conftest import ROOT


def test_numbered_court_instances_precede_separately_labelled_prosecutors(client, store):
    with closing(sqlite3.connect(store)) as conn:
        conn.execute("""INSERT INTO court_chain (plz,ortk,matter,position,authority_id,role)
                        VALUES ('12346','SOLO','sozial',1,10,'prosecution')""")
        conn.commit()
    chain = client.get("/resolve/court", params={"plz": "12346", "matter": "sozial"}).json()["chain"]
    assert chain[0]["role"] == "court"
    assert chain[-1]["role"] == "prosecution"


def test_place_suggestions_preserve_qualifiers_and_rank_exact_before_limit(client, store):
    with closing(sqlite3.connect(store)) as conn:
        for index, name in enumerate(["Burgdorf", "Burg (Mosel)", "Burg (Mosel) West"]):
            ags = f"9900200{index}"
            conn.execute("INSERT INTO gemeinde VALUES (?,?,?,?,?,?,?,?)",
                         (ags, ags + "0000", "99001", name, name, name.lower(), "Gemeinde",
                          1 if name == "Burg (Mosel)" else 500))
        conn.commit()
    matches = client.get("/places", params={"q": "Burg (Mosel)", "limit": 1}).json()["matches"]
    assert [p["name"] for p in matches] == ["Burg (Mosel)"]


@pytest.mark.parametrize(("street", "number", "expected"), [
    ("Behlertstr.", "3a", "Behlertstr. 3a"),
    ("Friedrich-Ebert-Straße", "79/81", "Friedrich-Ebert-Straße 79/81"),
    ("Straße 3 a", "3A", "Straße 3 a"),
    ("Straße 5–7", "5-7", "Straße 5–7"),
    ("Straße", "", "Straße"),
    ("Straße", "Bei Briefen Amt angeben!", "Straße"),
    (None, "4", None),
])
def test_pvog_house_numbers(monkeypatch, street, number, expected):
    monkeypatch.syspath_prepend(str(ROOT / "pipeline"))
    from build_db import pvog_street
    assert pvog_street({"street": street, "houseNumber": number}) == expected


def test_sgb_xii_unit_keeps_its_own_function_and_does_not_inherit_ba_identity(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "pipeline"))
    from common import conflicts_with_jobcenter
    from build_db import load_ba, load_pvog

    assert not conflicts_with_jobcenter("Gemeinsame Stelle SGB II und SGB XII")
    assert not conflicts_with_jobcenter("Sozialleistungen SGB XII / II")
    title = "Arbeitsgruppe Grundsicherung und Hilfe zum Lebensunterhalt SGB XII"
    (tmp_path / "units.jsonl").write_text(json.dumps({"oe_id": "OE.1", "detail": {"title": title}}))
    (tmp_path / "competences.jsonl").write_text("\n".join(json.dumps({
        "oe_id": "OE.1", "oe_title": title, "kind": kind, "ags": "12054000", "ars": "120540000000",
    }) for kind in ["sozialamt", "jobcenter"]))
    (tmp_path / "traeger.jsonl").write_text(json.dumps(
        {"traeger_nr": "03904", "name": "Jobcenter Potsdam, Stadt", "legal_form": "gE"}))
    (tmp_path / "zuordnung.jsonl").write_text(json.dumps(
        {"traeger_nr": "03904", "kreis_ags": "12054", "split": False}))
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript((ROOT / "db/schema.sql").read_text())
        load_pvog(conn, tmp_path)
        load_ba(conn, tmp_path)
        assert conn.execute("""SELECT a.name,c.kind,c.level FROM competence c
                               JOIN authority a ON a.id=c.authority_id ORDER BY c.kind""").fetchall() == [
            ("Jobcenter Potsdam, Stadt", "jobcenter", "kreis"), (title, "sozialamt", "gemeinde")]
        assert conn.execute("""SELECT a.name FROM authority_external_id i JOIN authority a
                               ON a.id=i.authority_id WHERE i.scheme='ba_traeger'""").fetchall() == [
            ("Jobcenter Potsdam, Stadt",)]


def test_postcode_prefix_suggestions_identify_the_actual_returned_postcodes(client):
    matches = client.get("/places", params={"q": "1234"}).json()["matches"]
    assert {match["plz"] for match in matches} == {"12345", "12346"}
    assert all(match["explicit_postcode"] is True for match in matches)
