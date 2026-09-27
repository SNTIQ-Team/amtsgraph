"""Synthetic collisions and partial evidence; no real court assertions."""
import sqlite3
import pytest
from fastapi.testclient import TestClient
from api import main

@pytest.fixture
def fixture(monkeypatch):
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE jz_place(plz TEXT, ortk TEXT, ort TEXT, ort_norm TEXT, gemeinde_ags TEXT);
    CREATE TABLE court_chain(plz TEXT, ortk TEXT, matter TEXT, position INT, role TEXT, note TEXT, authority_id INT);
    CREATE TABLE gemeinde(ags TEXT, name_simple TEXT, kreis_ags TEXT);
    CREATE TABLE gemeinde_plz(ags TEXT, plz TEXT);
    CREATE TABLE authority(id INT, name TEXT, kind TEXT, valid_to TEXT);
    CREATE TABLE competence(authority_id INT, kind TEXT, level TEXT, area TEXT, rank INT);
    CREATE TABLE caveat(id INT, scope_level TEXT, scope_key TEXT, matter TEXT, severity TEXT, text_de TEXT, source TEXT);
    INSERT INTO gemeinde VALUES('00123456','Example','00123');
    INSERT INTO gemeinde_plz VALUES('00123456','12345');
    INSERT INTO jz_place VALUES('12345','A','Example (A)','example','00123456'),('12345','B','Example (B)','example','00123456');
    INSERT INTO court_chain VALUES('12345','A','sozial',1,'court','',1),('12345','B','sozial',1,'court','',2);
    INSERT INTO authority VALUES(1,'A','sozialgericht',NULL),(2,'B','sozialgericht',NULL),(3,'Office','jobcenter',NULL),(4,'Supervisor','ministerium',NULL);
    INSERT INTO competence VALUES(3,'jobcenter','gemeinde','00123456',0),(4,'jobcenter','kreis','00123',1);
    INSERT INTO caveat VALUES(1,'authority','1','sozial','block','Court restriction','fixture'),(2,'jz_place','12345|A',NULL,'warn','Place restriction','fixture'),(3,'gemeinde','00123456',NULL,'info','Municipality','fixture'),(4,'kreis','00123',NULL,'warn','District','fixture'),(5,'authority','3',NULL,'block','Office restriction','fixture'),(6,'authority','4',NULL,'info','Supervisor restriction','fixture'),(7,'global','any',NULL,'info','Global','fixture'),(8,'matter','sozial','sozial','warn','Matter','fixture'),(9,'authority','1','other','block','Unrelated matter','fixture');
    ''')
    monkeypatch.setattr(main, 'db', lambda: db)
    monkeypatch.setattr(main, 'authority_card', lambda conn, key: {'id': key, 'name': str(key)})
    yield TestClient(main.app)
    db.close()

def test_name_filter_cannot_hide_place_collision(fixture):
    answer = fixture.get('/resolve/court?plz=12345&ort=Example&matter=sozial').json()
    assert answer['status'] == 'ambiguous_identity'
    assert [r['ortk'] for r in answer['options']] == ['A','B']
    assert 'chain' not in answer
    selected = fixture.get('/resolve/court?plz=12345&ort=Example&ortk=B&matter=sozial').json()
    assert selected['chain'][0]['id'] == 2
    assert selected['place']['ortk'] == 'B'
    assert fixture.get('/resolve/court?plz=12345&ortk=missing&matter=sozial').status_code == 404

def test_court_carries_all_scoped_evidence_once(fixture):
    answer = fixture.get('/resolve/court?plz=12345&ortk=A&matter=sozial').json()
    assert {r['id'] for r in answer['caveats']} == {1,2,3,4,7,8}
    assert answer['assignments'][0]['authority_id'] == 1
    assert answer['assignments'][0]['evidence']['type'] == 'register_record'

def test_authority_keeps_supervision_and_assignment_evidence_separate(fixture):
    answer = fixture.get('/resolve/authority?ags=00123456&kind=jobcenter').json()
    assert answer['resolved']['id'] == 3
    assert answer['supervisory'][0]['id'] == 4
    assert {r['id'] for r in answer['caveats']} == {3,4,5,6,7}
    assert [(a['authority_id'],a['rank']) for a in answer['assignments']] == [(3,0),(4,1)]

@pytest.mark.parametrize('suffix', ['&at=2020-01-01','&known_at=2020-01-01','&unknown=1','&plz=22222'])
def test_constraints_are_never_silently_ignored(fixture, suffix):
    assert fixture.get('/resolve/court?plz=12345&ortk=A&matter=sozial'+suffix).status_code == 422

def test_capabilities_declare_current_only(fixture):
    assert fixture.get('/capabilities').json()['temporal'] == 'current_only'

def test_mounted_public_prefix_keeps_constraint_checks(fixture):
    from fastapi import FastAPI
    shell = FastAPI()
    shell.mount('/v1', main.app)
    client = TestClient(shell)
    response = client.get('/v1/resolve/court?plz=12345&matter=sozial&at=2020-01-01')
    assert response.status_code == 422
    assert response.json()['status'] == 'unsupported_temporal_request'
