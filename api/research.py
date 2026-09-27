"""Shared legal-research contract; no second corpus and no inferred competence law."""
from datetime import date, datetime, timezone
import hashlib
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
            if not isinstance(data, dict):
                return {'status': 'unavailable', 'reason': 'upstream_representation_unusable'}
            headers = {k.lower(): v for k, v in response.headers.items() if k.lower().startswith('x-lexgraph-')}
            return {'status': 'partial' if data.get('status') == 'partial' else 'ok', 'data': data,
                    'representation': {'service': 'lexgraph', 'route': route, 'media_type': content_type,
                        'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
                        'retrieved_at': datetime.now(timezone.utc).isoformat(),
                        'snapshot': headers.get('x-lexgraph-snapshot'), 'source_metadata': headers}}

    except HTTPError as exc:
        reasons = {404: 'not_in_corpus', 409: 'ambiguous_identity', 422: 'unsupported_request'}
        reason = reasons.get(exc.code, 'source_unavailable')
        try:
            error = json.loads(exc.read(8192))
            detail = error.get('detail', error)
            reported = detail.get('status') if isinstance(detail, dict) else None
            if reported in {'integrity_check_failed', 'stale_snapshot', 'unsupported_temporal_request', 'temporally_unresolved'}:
                reason = reported
        except (OSError, ValueError, AttributeError):
            pass
        return {'status': 'unavailable', 'reason': reason, 'http_status': exc.code}
    except (URLError, TimeoutError, OSError, ValueError, UnicodeError):
        return {'status': 'unavailable', 'reason': 'source_unavailable'}

def component(call):
    try:
        value = call()
        status = value.get('status', 'ok')
        return {'status': status, 'data': value}
    except HTTPException as exc:
        return {'status': 'unavailable', 'reason': 'not_in_corpus' if exc.status_code == 404 else 'invalid_request', 'http_status': exc.status_code}

def validate_scope(scope):
    """One parser for HTTP, batch and the future MCP. Return an owned copy."""
    if not isinstance(scope, dict):
        raise ValueError("scope must be an object")
    unknown = set(scope) - ALLOWED
    if unknown:
        raise ValueError("unknown scope fields: " + ",".join(sorted(unknown)))
    for key, value in scope.items():
        if key == 'limit':
            if type(value) is not int or not 1 <= value <= 25:
                raise ValueError("invalid limit")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError("invalid scope value: " + key)
    if not any(scope.get(k) for k in ['q', 'act_id', 'plz', 'ags']):
        raise ValueError("empty scope")
    if bool(scope.get('plz')) != bool(scope.get('matter')) or bool(scope.get('ags')) != bool(scope.get('kind')) or (scope.get('ortk') and not scope.get('plz')) or (scope.get('norm') and not scope.get('act_id')):
        raise ValueError("incomplete scope")
    for key, pattern in [('plz', r'\d{5}'), ('ags', r'\d{8}'), ('act_id', r'[A-Za-z0-9_-]{1,100}')]:
        if key in scope and not re.fullmatch(pattern, scope[key]):
            raise ValueError("invalid identity: " + key)
    for key in ['q', 'ortk', 'matter', 'kind', 'norm']:
        if key in scope and len(scope[key]) > (400 if key == 'q' else 200):
            raise ValueError("scope value too long: " + key)
    if (scope.get('at') or scope.get('known_at')) and (not scope.get('act_id') or any(scope.get(k) for k in ['q', 'plz', 'ags'])):
        raise ValueError("unsupported temporal request")
    if 'at' in scope:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', scope['at']):
            raise ValueError("at must be an ISO day")
        date.fromisoformat(scope['at'])
    if 'known_at' in scope:
        known = datetime.fromisoformat(scope['known_at'])
        if known.tzinfo is None:
            raise ValueError("known_at must include timezone")
    return {'limit': 10, **scope}


def research(*, scope, court, authority, legal=lexgraph_get):
    """Callable from HTTP, batch, tests and a future MCP adapter."""
    scope = validate_scope(scope)
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
    snapshots = {c.get('representation', {}).get('snapshot') for name, c in components.items()
                 if name.startswith('law_') and c.get('representation', {}).get('snapshot')}
    issues = [{'reason': 'mixed_snapshots', 'service': 'lexgraph', 'snapshots': sorted(snapshots)}] if len(snapshots) > 1 else []
    return {'schema_version': 1, 'status': 'ok' if not issues and all(c['status'] == 'ok' for c in components.values()) else 'partial',
            'issues': issues,
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
        scope = {k: v for k, v in dict(q=q, limit=limit, plz=plz, ortk=ortk, matter=matter,
                 ags=ags, kind=kind, act_id=act_id, norm=norm,
                 at=at.isoformat() if at else None, known_at=known_at.isoformat() if known_at else None).items() if v is not None}
        try:
            return research(scope=scope, court=court, authority=authority)
        except ValueError as exc:
            raise HTTPException(422, {'status': 'invalid_request', 'reason': str(exc)}) from exc
    return router
