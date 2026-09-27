"""Shared legal-research contract; no second corpus and no inferred competence law."""
from datetime import date, datetime, timezone
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import urlopen
from fastapi import APIRouter, HTTPException, Query, Request

LEXGRAPH = os.environ.get('LEXGRAPH_RESEARCH_URL', 'http://127.0.0.1:8002').rstrip('/')
MAX_BYTES = 4 * 1024 * 1024
ALLOWED = {'q', 'limit', 'plz', 'ortk', 'matter', 'ags', 'kind', 'act_id', 'norm', 'at', 'known_at'}

def lexgraph_get(route):
    try:
        with urlopen(LEXGRAPH + route, timeout=12) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return {'status': 'unavailable', 'reason': 'response_too_large'}
            content_type = response.headers.get_content_type()
            if content_type == 'application/json':
                data = json.loads(body)
            elif content_type in ('text/markdown', 'text/plain'):
                data = {'text': body.decode('utf-8'), 'metadata': {
                    k: v for k, v in response.headers.items() if k.lower().startswith('x-lexgraph-')}}
            else:
                return {'status': 'unavailable', 'reason': 'upstream_representation_unusable'}
            return {'status': 'partial' if isinstance(data, dict) and data.get('status') == 'partial' else 'ok', 'data': data}
    except HTTPError as exc:
        reasons = {404: 'not_in_corpus', 409: 'ambiguous_identity', 422: 'unsupported_request'}
        return {'status': 'unavailable', 'reason': reasons.get(exc.code, 'source_unavailable'), 'http_status': exc.code}
    except (URLError, TimeoutError, OSError, ValueError, UnicodeError):
        return {'status': 'unavailable', 'reason': 'source_unavailable'}

def component(call):
    try:
        value = call()
        status = value.get('status', 'ok')
        return {'status': status, 'data': value}
    except HTTPException as exc:
        return {'status': 'unavailable', 'reason': 'not_in_corpus' if exc.status_code == 404 else 'invalid_request', 'http_status': exc.status_code}

def research(*, scope, court, authority, legal=lexgraph_get):
    """Callable from HTTP, batch, tests and a future MCP adapter."""
    unknown = set(scope) - ALLOWED
    if unknown:
        raise ValueError("unknown scope fields: " + ",".join(sorted(unknown)))
    if not any(scope.get(k) for k in ['q', 'act_id', 'plz', 'ags']):
        raise ValueError("empty scope")
    if (scope.get('at') or scope.get('known_at')) and (not scope.get('act_id') or any(scope.get(k) for k in ['q', 'plz', 'ags'])):
        raise ValueError("unsupported temporal request")
    if bool(scope.get('plz')) != bool(scope.get('matter')) or bool(scope.get('ags')) != bool(scope.get('kind')):
        raise ValueError("incomplete scope")
    if scope.get('q') and (not isinstance(scope['q'], str) or not 1 <= len(scope['q']) <= 400):
        raise ValueError("invalid query")
    if 'limit' in scope and (type(scope['limit']) is not int or not 1 <= scope['limit'] <= 25):
        raise ValueError("invalid limit")
    components = {}
    if scope.get('q'):
        components['law_search'] = legal('/search?' + urlencode({'q': scope['q'], 'limit': scope.get('limit',10), 'norm_limit': scope.get('limit',10), 'catalog_limit': scope.get('limit',10)}))
    if scope.get('act_id'):
        parameters = {k: v for k, v in {'norm': scope.get('norm'), 'at': scope.get('at'), 'as_of': scope.get('known_at')}.items() if v}
        components['law_text'] = legal('/acts/' + quote(scope['act_id'], safe='') + '/markdown?' + urlencode(parameters))
    if scope.get('plz'):
        components['court'] = component(lambda: court(plz=scope['plz'], matter=scope['matter'], ortk=scope.get('ortk')))
    if scope.get('ags'):
        components['authority'] = component(lambda: authority(ags=scope['ags'], kind=scope['kind']))
    return {'schema_version': 1, 'status': 'ok' if all(c['status'] == 'ok' for c in components.values()) else 'partial',
            'request_scope': scope, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'snapshot_consistency': 'per_component', 'components': components,
            'relations': [], 'relation_policy': 'only_source_asserted_assignments; no_inferred_statutory_basis'}

def create_research_router(court, authority):
    router = APIRouter()
    @router.get('/research')
    def combined(request: Request, q: str | None = Query(None, min_length=1, max_length=400),
                 limit: int = Query(10, ge=1, le=25), plz: str | None = None,
                 ortk: str | None = None, matter: str | None = None,
                 ags: str | None = None, kind: str | None = None,
                 act_id: str | None = None, norm: str | None = None,
                 at: date | None = None, known_at: datetime | None = None):
        bad = sorted(set(request.query_params) - ALLOWED)
        repeats = sorted(k for k in request.query_params if len(request.query_params.getlist(k)) > 1)
        if bad or repeats:
            raise HTTPException(422, {'status': 'invalid_request', 'unsupported_arguments': bad, 'repeated_arguments': repeats})
        if not (q or act_id or plz or ags):
            raise HTTPException(422, 'provide q, act_id, plz/matter or ags/kind')
        if bool(plz) != bool(matter) or bool(ags) != bool(kind) or (ortk and not plz) or (norm and not act_id):
            raise HTTPException(422, 'incomplete scope')
        if (plz and not re.fullmatch(r'\d{5}', plz)) or (ags and not re.fullmatch(r'\d{8}', ags)) or (act_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', act_id)):
            raise HTTPException(422, 'invalid identity')
        if any(v and len(v) > 200 for v in [ortk, matter, kind, norm]):
            raise HTTPException(422, 'scope value too long')
        if (at or known_at) and (not act_id or q or plz or ags):
            raise HTTPException(422, {'status': 'unsupported_temporal_request', 'supported_operation': 'act_id text'})
        if known_at and known_at.tzinfo is None:
            raise HTTPException(422, 'known_at must include timezone')
        scope = {k: v for k, v in dict(q=q, limit=limit, plz=plz, ortk=ortk, matter=matter,
                 ags=ags, kind=kind, act_id=act_id, norm=norm,
                 at=at.isoformat() if at else None, known_at=known_at.isoformat() if known_at else None).items() if v is not None}
        return research(scope=scope, court=court, authority=authority)
    return router
