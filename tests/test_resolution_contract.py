"""Place identity, caveat aggregation, relation evidence and the envelope.

Synthetic store (tests/conftest.py); no real court or authority is asserted.
"""
from __future__ import annotations

import pytest

from api.contract import HTTP_STATUS, STATUSES, envelope
from tests.conftest import A_COURT_1, A_OFFICE

ENVELOPE_KEYS = {"schema_version", "operation", "status", "request_scope",
                 "applied_constraints", "ignored_arguments", "snapshot_id",
                 "temporal", "searched", "evidence", "warnings",
                 "failed_components", "total"}

PLACE_A = "place:jz:12345:EXAMPLE-A"
PLACE_B = "place:jz:12345:EXAMPLE-B"
MUNI = "place:ags:99001001"


def test_query_contract_includes_nested_aliases_and_excludes_headers():
    from fastapi import Depends, FastAPI, Header, Query
    from api.contract import _declared_query_names

    def window(limit: int = Query(10, alias="max-results")):
        return limit

    def scope(page_size: int = Depends(window), snapshot: str = Header("")):
        return page_size, snapshot

    app = FastAPI()

    @app.get("/places/{place_id}")
    def places(place_id: str, q: str, selection=Depends(scope)):
        return q

    route = next(route for route in app.routes if route.path == "/places/{place_id}")
    assert _declared_query_names(route) == {"q", "max-results"}


def texts(items):
    return {c["text_de"] for c in items}


def get(client, url, code=None):
    response = client.get(url)
    body = response.json()
    assert ENVELOPE_KEYS <= set(body), sorted(ENVELOPE_KEYS - set(body))
    assert body["status"] in STATUSES
    assert response.status_code == HTTP_STATUS[body["status"]]
    assert body["temporal"] == "current_only"
    assert body["ignored_arguments"] == []
    if code is not None:
        assert response.status_code == code, body
    return body


# ------------------------------------------------------- §8.1 place identity

def test_place_resolve_reports_collision_with_stable_ids(client):
    body = get(client, "/resolve/place?purpose=court&q=12345", 200)
    assert body["status"] == "ambiguous_identity"
    assert body["place_id"] is None and body["place"] is None
    assert [c["place_id"] for c in body["candidates"]] == [PLACE_A, PLACE_B]
    assert body["total"] == 2
    one = get(client, "/resolve/place?purpose=court&q=12346", 200)
    assert one["status"] == "ok"
    assert one["place_id"] == "place:jz:12346:SOLO"
    assert one["place"]["municipality_id"] == "place:ags:99001002"


def test_place_name_filter_cannot_hide_collision(client):
    body = get(client, "/resolve/place?purpose=court&plz=12345&ort=Example",
               200)
    assert body["status"] == "ambiguous_identity"
    assert len(body["candidates"]) == 2


def test_municipality_resolution_is_explicitly_ambiguous_per_plz(client):
    body = get(client, "/resolve/place?purpose=authority&plz=12345", 200)
    assert body["status"] == "ambiguous_identity"
    assert {c["place_id"] for c in body["candidates"]} == {
        "place:ags:99001001", "place:ags:99001002"}
    exact = get(client, "/resolve/place?purpose=authority&ags=99001001", 200)
    assert exact["place_id"] == MUNI


def test_court_chain_never_selects_first_row_on_collision(client):
    body = get(client, "/resolve/court?plz=12345&ort=Example&matter=sozial",
               200)
    assert body["status"] == "ambiguous_identity"
    assert "chain" not in body and "assignments" not in body
    assert [o["place_id"] for o in body["options"]] == [PLACE_A, PLACE_B]
    assert body["needs_place_id"] is True


@pytest.mark.parametrize("place_id,court", [(PLACE_A, 1), (PLACE_B, 2)])
def test_stable_place_id_selects_the_collided_place(client, place_id, court):
    chain = get(client, f"/resolve/court?place_id={place_id}&matter=sozial",
                200)
    assert chain["place"]["place_id"] == place_id
    assert chain["chain"][0]["id"] == court
    comp = get(client, f"/resolve/competence?place_id={place_id}"
                       f"&matter=sozial", 200)
    assert comp["resolved"]["id"] == court
    assert comp["subject"] == "court_matter"


def test_legacy_ortk_selector_still_disambiguates(client):
    body = get(client, "/resolve/court?plz=12345&ortk=EXAMPLE-B&matter=sozial",
               200)
    assert body["chain"][0]["id"] == 2
    missing = get(client, "/resolve/court?plz=12345&ortk=NOPE&matter=sozial",
                  404)
    assert missing["status"] == "not_found_in_searched_scope"


@pytest.mark.parametrize("url", [
    f"/resolve/competence?place_id={MUNI}&matter=sozial",
    f"/resolve/competence?place_id={PLACE_A}&kind=jobcenter",
    f"/resolve/competence?place_id={PLACE_A}&matter=sozial&kind=jobcenter",
    f"/resolve/court?place_id={PLACE_A}&plz=12345&matter=sozial",
    "/resolve/court?place_id=place:jz:1234:X&matter=sozial",
    "/resolve/competence?place_id=berlin&matter=sozial",
    f"/resolve/authority?place_id={PLACE_A}&kind=jobcenter",
    "/resolve/place?q=12345",
    "/resolve/place?purpose=court&ags=99001001",
])
def test_place_identity_errors_are_typed(client, url):
    body = get(client, url, 400)
    assert body["status"] == "invalid_argument"


def test_unknown_stable_place_is_not_found(client):
    body = get(client, "/resolve/competence?place_id=place:jz:12345:ZZZ"
                       "&matter=sozial", 404)
    assert body["status"] == "not_found_in_searched_scope"


# ---------------------------------------------------- §8.2 caveats travel

COURT_SCOPES = ["SYN-AUTH-COURT", "SYN-PLACE", "SYN-PLZ", "SYN-MUNI",
                "SYN-KREIS", "SYN-LAND", "SYN-MATTER", "SYN-ASSIGN-COURT",
                "SYN-COURT", "SYN-GLOBAL", "SYN-GAP-SOZIAL"]


@pytest.mark.parametrize("url", [
    f"/resolve/court?place_id={PLACE_A}&matter=sozial",
    f"/resolve/competence?place_id={PLACE_A}&matter=sozial",
    "/resolve/court?plz=12345&ortk=EXAMPLE-A&matter=sozial",
])
@pytest.mark.parametrize("caveat", COURT_SCOPES)
def test_every_scope_caveat_reaches_court_resolver(client, url, caveat):
    body = get(client, url, 200)
    active = {c["text_de"]: c for c in body["caveats"]}
    assert caveat in active
    if active[caveat]["severity"] in ("warn", "block"):
        assert caveat in body["critical_warning"]
    assert any(caveat in w for w in body["warnings"])


OFFICE_SCOPES = ["SYN-AUTH-OFFICE", "SYN-ASSIGN-OFFICE", "SYN-MUNI",
                 "SYN-KREIS", "SYN-LAND", "SYN-PLZ", "SYN-GLOBAL"]


@pytest.mark.parametrize("url", [
    f"/resolve/competence?place_id={MUNI}&kind=jobcenter",
    "/resolve/authority?ags=99001001&kind=jobcenter",
])
@pytest.mark.parametrize("caveat", OFFICE_SCOPES)
def test_every_scope_caveat_reaches_office_resolver(client, url, caveat):
    body = get(client, url, 200)
    assert body["resolved"]["id"] == 10
    active = {c["text_de"]: c for c in body["caveats"]}
    assert caveat in active
    if active[caveat]["severity"] in ("warn", "block"):
        assert caveat in body["critical_warning"]


def test_synthetic_authority_caveat_is_in_structured_and_critical_text(client):
    """The paper's acceptance test (§8.2), stated literally."""
    body = get(client, f"/resolve/competence?place_id={MUNI}&kind=jobcenter")
    hit = [c for c in body["caveats"] if c["text_de"] == "SYN-AUTH-OFFICE"]
    assert hit and hit[0]["severity"] == "block"
    assert {"scope_level": "authority", "scope_key": "10",
            "role": "authority"} in hit[0]["applies_via"]
    assert "[BLOCK] SYN-AUTH-OFFICE" in body["critical_warning"]
    assignment = next(a for a in body["assignments"]
                      if a["assignment_id"] == A_OFFICE)
    assert "SYN-AUTH-OFFICE" in texts(assignment["exceptions"])
    assert "SYN-ASSIGN-OFFICE" in texts(assignment["exceptions"])


@pytest.mark.parametrize("url,absent", [
    (f"/resolve/court?place_id={PLACE_A}&matter=sozial",
     {"SYN-OTHER-MATTER", "SYN-KIND-NARROW", "SYN-AUTH-OFFICE"}),
    (f"/resolve/competence?place_id={MUNI}&kind=jobcenter",
     {"SYN-KIND-NARROW", "SYN-MATTER", "SYN-AUTH-COURT", "SYN-PLACE"}),
])
def test_narrowed_caveats_do_not_leak_to_other_matters(client, url, absent):
    body = get(client, url, 200)
    assert not texts(body["caveats"]) & absent


@pytest.mark.parametrize("url,expired", [
    (f"/resolve/court?place_id={PLACE_A}&matter=sozial", "SYN-EXPIRED"),
    (f"/resolve/competence?place_id={MUNI}&kind=jobcenter",
     "SYN-EXPIRED-OFFICE"),
    ("/authorities/1", "SYN-EXPIRED"),
])
def test_expired_caveat_is_reported_but_does_not_qualify(client, url,
                                                         expired):
    body = get(client, url, 200)
    assert expired not in texts(body["caveats"])
    assert expired not in (body["critical_warning"] or "")
    inactive = {c["text_de"]: c for c in body["inactive_caveats"]}
    assert inactive[expired]["inactive_reason"] == "expired"
    assert inactive[expired]["active"] is False


def test_not_yet_valid_caveat_is_inactive(client):
    body = get(client, f"/resolve/competence?place_id={MUNI}&kind=jobcenter")
    inactive = {c["text_de"]: c for c in body["inactive_caveats"]}
    assert inactive["SYN-FUTURE"]["inactive_reason"] == "not_yet_valid"


def test_register_gap_caveat_survives_a_missing_chain(client):
    body = get(client, f"/resolve/court?place_id={PLACE_A}&matter=zivil",
               404)
    assert body["status"] == "not_found_in_searched_scope"
    assert body["chain"] == [] and body["total"] == 0
    assert "SYN-AUTH-COURT" not in texts(body["caveats"])
    assert "SYN-PLACE" in texts(body["caveats"])


def test_authority_detail_shows_entity_caveats_of_all_matters(client):
    body = get(client, "/authorities/1", 200)
    assert {"SYN-AUTH-COURT", "SYN-OTHER-MATTER", "SYN-COURT",
            "SYN-GLOBAL"} <= texts(body["caveats"])
    assert "SYN-OTHER-MATTER" in body["critical_warning"]


# ------------------------------------------- §8.3 / §8.4 relation evidence

def test_court_assignment_carries_relation_level_evidence(client):
    body = get(client, f"/resolve/court?place_id={PLACE_A}&matter=sozial")
    first = next(a for a in body["assignments"]
                 if a["assignment_id"] == A_COURT_1)
    assert first["relation"] == "competent_for"
    assert first["matter"]["code"] == "sozial"
    assert first["territory"]["place_id"] == PLACE_A
    assert first["conditions"] == ["Synthetic chamber note."]
    assert first["temporal"] == {
        "mode": "current_only", "valid_from": None,
        "valid_until_exclusive": None, "interval_status": "not_recorded",
        "observed_on": "2026-06-10"}
    kinds = [e["kind"] for e in first["evidence"]]
    assert kinds == ["register_observation", "statutory_assignment"]
    register, statute = first["evidence"]
    assert register["observed_on"] == "2026-06-10"
    assert "plz=12345" in register["source_url"]
    assert "ang=sozial" in register["source_url"]
    assert "statutory_basis" in register["does_not_establish"]
    assert register["legal_effect_verified"] is False
    assert statute["citation"] == "§ 1 Synthetic Ordinance"
    assert statute["passage"] == "SYNTHETIC TEST TEXT — NOT LAW."
    assert first["statutory_basis"]["status"] == "recorded"
    assert first["statutory_basis"]["legal_effect_verified"] is False
    assert first["unresolved_facts"]
    assert first["court_id"] == "court:de:Q1101"
    # the other Land's statute never attaches
    assert "§ 9 Other Land" not in {e.get("citation")
                                    for e in body["evidence"]}


def test_higher_instance_is_not_an_appeal_guarantee(client):
    body = get(client, f"/resolve/court?place_id={PLACE_A}&matter=sozial")
    higher = next(a for a in body["assignments"] if a["position"] == 2)
    assert higher["function"] == "higher_instance_listed"
    assert "appeal_availability_not_assessed" in {
        f["code"] for f in higher["unresolved_facts"]}
    assert higher["statutory_basis"]["status"] == "not_recorded"


def test_office_assignment_separates_observation_inference_and_curation(
        client):
    body = get(client, f"/resolve/competence?place_id={MUNI}&kind=jobcenter")
    by_id = {a["authority_id"]: a for a in body["assignments"]}
    observed, inferred = by_id[10], by_id[11]
    assert observed["assignment_basis"] == "observed"
    assert observed["evidence"][0]["kind"] == "register_observation"
    assert observed["evidence"][0]["source_ref"] == "LB.1@990010010000"
    assert inferred["assignment_basis"] == "inferred"
    assert inferred["derivations"][0]["method"] == \
        "asylblg_title_filter_higher_level"
    assert inferred["function"] == "higher_level_or_supervisory"
    assert [s["id"] for s in body["supervisory"]] == [11]
    curated = get(client, f"/resolve/competence?place_id={MUNI}"
                          f"&kind=standesamt")
    (a,) = curated["assignments"]
    assert a["assignment_basis"] == "curated"
    assert a["evidence"][0]["kind"] == "curated_source_review"
    assert a["evidence"][0]["review_status"] == "overdue"
    assert "curated_review_overdue" in {f["code"]
                                        for f in a["unresolved_facts"]}


def test_several_primary_offices_are_ambiguous_not_first(client):
    body = get(client, f"/resolve/competence?place_id={MUNI}&kind=sozialamt",
               200)
    assert body["status"] == "ambiguous_identity"
    assert body["ambiguity"] == "competent_authority"
    assert body["resolved"] is None
    assert [c["id"] for c in body["candidates"]] == [12, 13]


def test_graph_heuristics_stay_visualization_metadata(client, monkeypatch):
    from api import main
    monkeypatch.setattr(main, "_GRAPH_CACHE", None)
    monkeypatch.setattr(main, "_GRAPH_JSON", None)
    graph = client.get("/graph").json()
    # the live viewer sends a cache-busting token; strictness must allow it
    assert client.get("/graph?v=5").status_code == 200
    assert client.get("/graph?v=5&x=1").status_code == 400
    assert graph["visualization_metadata"]["legal_effect"] is False
    assert graph["visualization_metadata"]["fields"] == ["land", "kreis"]
    assert "not proof" in graph["edge_semantics"]["appeal"]
    detail = get(client, "/authorities/1", 200)
    (edge,) = detail["related"]
    assert edge["edge_basis"] == "derived_from_register_chain_adjacency"
    assert edge["legal_effect_verified"] is False
    assert "territorial_competence" in \
        detail["evidence"][0]["does_not_establish"]


# ------------------------------------------------------ §8.5 current_only

MCP_URLS = [
    "/resolve/place?purpose=court&q=12346",
    f"/resolve/competence?place_id={PLACE_A}&matter=sozial",
    f"/resolve/court?place_id={PLACE_A}&matter=sozial",
    f"/resolve/authority?place_id={MUNI}&kind=jobcenter",
    "/authorities/1",
    "/courts/court:de:Q1101",
    "/courts/lookup?name=Sozialgericht%20A",
]


@pytest.mark.parametrize("url", MCP_URLS)
@pytest.mark.parametrize("arg", ["at=2020-01-01", "as_of=2020-01-01",
                                 "valid_at=2020-01-01",
                                 "known_at=2020-01-01T00:00:00Z"])
def test_historical_arguments_are_rejected_not_ignored(client, url, arg):
    body = get(client, f"{url}&{arg}" if "?" in url else f"{url}?{arg}", 400)
    assert body["status"] == "unsupported_temporal_request"
    assert body["temporal_arguments"] == [arg.split("=")[0]]


def test_capabilities_declare_current_only(client):
    caps = client.get("/capabilities").json()
    assert caps["temporal"] == "current_only"
    assert caps["temporal_scope"]["historical_supported"] is False
    assert caps["place_identity"]["court_register_place"] == \
        "place:jz:<PLZ>:<ORTK>"


# ----------------------------------------------- strict schemas, envelope

@pytest.mark.parametrize("url", MCP_URLS + ["/places?q=Example", "/matters",
                                             "/stats", "/courts/mapping"])
def test_unknown_arguments_are_named_in_a_400(client, url):
    joined = f"{url}&bogus=1&other=2" if "?" in url else f"{url}?bogus=1&other=2"
    body = get(client, joined, 400)
    assert body["status"] == "invalid_argument"
    assert body["unknown_arguments"] == ["bogus", "other"]


@pytest.mark.parametrize("url", [
    f"/resolve/court?place_id={PLACE_A}&matter=sozial&matter=zivil",
    "/resolve/place?purpose=court&q=12345&q=12346",
])
def test_repeated_arguments_are_rejected(client, url):
    body = get(client, url, 400)
    assert body["repeated_arguments"]


def test_missing_and_malformed_arguments_are_named(client):
    body = get(client, "/resolve/court?plz=12345", 400)
    assert body["missing_arguments"] == ["matter"]
    bad = get(client, "/resolve/court?plz=12345&matter=nonsense", 400)
    assert bad["invalid_arguments"] == ["matter"]
    kind = get(client, f"/resolve/authority?place_id={MUNI}"
                       f"&kind=sozialgericht", 400)
    assert kind["invalid_arguments"] == ["kind"]


@pytest.mark.parametrize("prefix", ["/v1", "/amtsgraph"])
def test_mounted_prefixes_keep_the_contract(store, monkeypatch, prefix):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import main
    monkeypatch.setattr(main, "DB_PATH", store)
    shell = FastAPI()
    shell.mount(prefix, main.app)
    client = TestClient(shell)
    response = client.get(f"{prefix}/resolve/court?plz=12345&matter=sozial"
                          f"&at=2020-01-01")
    assert response.status_code == 400
    assert response.json()["status"] == "unsupported_temporal_request"
    ok = client.get(f"{prefix}/resolve/court?place_id={PLACE_A}"
                    f"&matter=sozial")
    assert ok.status_code == 200


def test_snapshot_pin_detects_a_different_build(client):
    current = get(client, "/resolve/place?purpose=court&q=12346")["snapshot_id"]
    assert current.startswith("amtsgraph-20260928T000000Z-")
    same = get(client, f"/resolve/place?purpose=court&q=12346"
                       f"&snapshot_id={current}", 200)
    assert same["snapshot_id"] == current
    other = get(client, "/resolve/place?purpose=court&q=12346"
                        "&snapshot_id=amtsgraph-old", 409)
    assert other["status"] == "stale_snapshot"
    assert other["current_snapshot_id"] == current


def test_unknown_authority_is_not_in_corpus(client):
    body = get(client, "/authorities/424242", 404)
    assert body["status"] == "not_in_corpus"


def test_envelope_refuses_untyped_status_and_field_clobbering():
    with pytest.raises(ValueError):
        envelope(operation="x", status="no_primary_assignment",
                 request_scope={})
    with pytest.raises(ValueError):
        envelope(operation="x", status="ok", request_scope={},
                 warnings=[], **{"snapshot_id": "forged"})


def test_request_scope_echoes_the_original_request(client):
    body = get(client, f"/resolve/court?place_id={PLACE_A}&matter=sozial")
    assert body["request_scope"] == {"place_id": PLACE_A, "matter": "sozial"}
    assert body["applied_constraints"]["matter"] == "sozial"


def test_legacy_ui_contract_is_preserved(client):
    """The sntiq.com viewer reads these fields; they must stay."""
    chain = get(client, "/resolve/court?plz=12346&matter=sozial", 200)
    assert {"chain", "caveats"} <= set(chain)
    assert {"position", "role", "note", "id", "name"} <= set(chain["chain"][0])
    amb = get(client, "/resolve/court?plz=12345&matter=sozial", 200)
    assert amb["needs_ort"] is True and amb["options"][0]["ortk"]
    office = get(client, "/resolve/authority?ags=99001001&kind=jobcenter")
    assert {"resolved", "candidates", "supervisory", "caveats",
            "gemeinde", "kind"} <= set(office)
    places = client.get("/places?q=Example").json()
    assert places["matches"][0]["place_id"] == "place:ags:99001001"
