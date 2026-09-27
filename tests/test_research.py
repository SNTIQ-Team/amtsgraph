from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.research import research, create_research_router

def test_joint_scope_preserves_assignment_and_legal_source_metadata():
    calls=[]
    def legal(route):
        calls.append(route)
        return {'status':'ok','data':{'change_total':0,'change_matches':[],'components':{'changes':{'status':'ok'}}}}
    result=research(scope={'q':'AsylbLG','limit':3,'ags':'09177117','kind':'asylblg_behoerde'},court=None,
                    authority=lambda **kw:{'status':'ok','assignments':[{'evidence':{'type':'register_record'}}],'caveats':[{'severity':'warn'}]},legal=legal)
    assert result['status']=='ok'
    assert result['components']['authority']['data']['caveats'][0]['severity']=='warn'
    assert result['relations']==[]
    assert calls==['/search?q=AsylbLG&limit=3&norm_limit=3&catalog_limit=3']

def test_source_outage_keeps_successful_competence():
    result=research(scope={'q':'x','limit':2,'plz':'12345','matter':'sozial'},court=lambda **kw:{'status':'ok','chain':[{'id':7}]},authority=None,
                    legal=lambda route:{'status':'unavailable','reason':'integrity_check_failed'})
    assert result['status']=='partial'
    assert result['components']['court']['data']['chain'][0]['id']==7
    assert result['components']['law_search']['reason']=='integrity_check_failed'

def test_ambiguous_place_is_not_promoted_to_success():
    result=research(scope={'plz':'12345','matter':'sozial'},court=lambda **kw:{'status':'ambiguous_identity','options':[{'ortk':'A'},{'ortk':'B'}]},authority=None)
    assert result['status']=='partial'
    assert 'chain' not in result['components']['court']['data']

def test_http_contract_rejects_unimplemented_scope_and_does_not_call_upstream():
    app=FastAPI();app.include_router(create_research_router(None,None));client=TestClient(app)
    for query in ['q=x&known_at=2020-01-01T00:00:00Z','q=x&at=2020-01-01','ags=09177117','q=x&unknown=1','q=x&q=y','act_id=../../etc/passwd','ortk=A','act_id=fed_x&known_at=2020-01-01T00:00:00']:
        assert client.get('/research?'+query).status_code==422

def test_historical_text_keeps_both_axes_and_source_headers():
    seen=[]
    result=research(scope={'act_id':'fed_asylblg','at':'2020-01-01','known_at':'2026-09-27T00:00:00+00:00'},court=None,authority=None,
                    legal=lambda route:(seen.append(route) or {'status':'ok','data':{'text':'fixture','metadata':{'X-Lexgraph-Source-Exact':'false'}}}))
    assert 'as_of=2026-09-27T00%3A00%3A00%2B00%3A00' in seen[0]
    assert result['components']['law_text']['data']['metadata']['X-Lexgraph-Source-Exact']=='false'
