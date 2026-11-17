"""Evidence envelope and strict request contract (REST now, MCP next).

Every read operation that an agent may rely on answers with one envelope:

    {schema_version, operation, status, request_scope, applied_constraints,
     ignored_arguments: [], snapshot_id, temporal, searched, evidence,
     warnings, failed_components, total, ...operation payload}

``status`` is one of STATUSES — a typed outcome, never an HTTP guess:

* ``ok``                          exactly the requested identity, fully served
* ``partial``                     served, but a component is missing
* ``ambiguous_identity``          several candidates; the caller must choose
* ``not_found_in_searched_scope`` the complete search found nothing
* ``not_in_corpus``               the identifier is unknown to this build
* ``invalid_argument``            unknown/repeated/malformed arguments
* ``unsupported_temporal_request`` a date/history argument: the resolver is
                                  ``current_only`` and never ignores dates
* ``stale_snapshot``              the caller pinned another snapshot_id
* ``unavailable``                 a store the operation needs is missing

Unknown query arguments are rejected with their names. They are never
silently dropped: an undeclared ``as_of`` changes what a user believes was
answered (paper §9.5).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Iterable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

SCHEMA_VERSION = 2

STATUSES = frozenset({
    "ok", "partial", "unavailable", "not_in_corpus",
    "not_found_in_searched_scope", "ambiguous_identity",
    "temporally_unresolved", "unsupported_temporal_request", "stale_snapshot",
    "integrity_check_failed", "upstream_representation_unusable",
    "invalid_argument",
})

HTTP_STATUS = {
    "ok": 200, "partial": 200, "ambiguous_identity": 200,
    "not_found_in_searched_scope": 404, "not_in_corpus": 404,
    "invalid_argument": 400, "unsupported_temporal_request": 400,
    "temporally_unresolved": 422, "stale_snapshot": 409,
    "unavailable": 503, "integrity_check_failed": 503,
    "upstream_representation_unusable": 502,
}

# Names that ask for a point in time. None is implemented: rejecting them
# (instead of answering with the current register) is the whole point.
TEMPORAL_ARGUMENTS = frozenset({
    "at", "as_of", "asof", "valid_at", "known_at", "date", "on",
    "valid_from", "valid_to", "valid_until", "historical", "history",
    "stichtag", "point_in_time", "timestamp", "observed_at", "version",
})

TEMPORAL_SCOPE = {
    "mode": "current_only",
    "historical_supported": False,
    "basis": ("latest register observations of this build; no dated "
              "assignment intervals are stored, so no historical competence "
              "is inferred"),
}


class ContractError(Exception):
    """A typed, client-visible failure (rendered as an envelope)."""

    def __init__(self, status: str, message: str, **details: Any):
        assert status in STATUSES, status
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


# ------------------------------------------------------------- snapshot

_SNAPSHOT_CACHE: dict[tuple, dict] = {}


def snapshot_info(conn: sqlite3.Connection) -> dict:
    """Build identity of the dataset behind ``conn`` (cached per build)."""
    try:
        rows = dict(conn.execute("SELECT key, value FROM build_info"))
    except sqlite3.Error:
        return {"snapshot_id": None, "built_at": None, "source_snapshots": {},
                "schema_version": None}
    key = tuple(sorted(rows.items()))
    hit = _SNAPSHOT_CACHE.get(key)
    if hit is None:
        digest = hashlib.sha256(json.dumps(
            rows, sort_keys=True).encode()).hexdigest()[:12]
        # URL-safe: callers echo it back as a query argument ('+' would
        # decode to a space)
        built = re.sub(r"[^0-9T]", "", rows.get("built_at") or "")[:15]
        try:
            sources = json.loads(rows.get("snapshots") or "{}")
        except ValueError:
            sources = {}
        hit = {"snapshot_id": f"amtsgraph-{built or 'unknown'}Z-{digest}",
               "built_at": rows.get("built_at"),
               "source_snapshots": sources,
               "schema_version": rows.get("schema_version")}
        _SNAPSHOT_CACHE.clear()
        _SNAPSHOT_CACHE[key] = hit
    return hit


def check_snapshot(conn: sqlite3.Connection, requested: str | None) -> dict:
    info = snapshot_info(conn)
    if requested is not None and requested != info["snapshot_id"]:
        raise ContractError(
            "stale_snapshot",
            "the requested snapshot is not the one this service serves",
            requested_snapshot_id=requested,
            current_snapshot_id=info["snapshot_id"])
    return info


# ------------------------------------------------------------- envelope

def envelope(*, operation: str, status: str, request_scope: dict,
             snapshot: dict | None = None,
             applied_constraints: dict | None = None,
             searched: Iterable[dict] = (), evidence: Iterable[dict] = (),
             warnings: Iterable[str] = (), failed_components: Iterable = (),
             total: int | None = None, **payload: Any) -> dict:
    if status not in STATUSES:
        raise ValueError(f"untyped status {status!r}")
    body = {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "status": status,
        "request_scope": request_scope,
        "applied_constraints": applied_constraints or {},
        "ignored_arguments": [],
        "snapshot_id": (snapshot or {}).get("snapshot_id"),
        "temporal": "current_only",
        "temporal_scope": TEMPORAL_SCOPE,
        "searched": list(searched),
        "evidence": list(evidence),
        "warnings": list(warnings),
        "failed_components": list(failed_components),
        "total": total,
    }
    clash = set(payload) & set(body)
    if clash:
        raise ValueError(f"payload overrides envelope fields {sorted(clash)}")
    body.update(payload)
    return body


def respond(body: dict) -> JSONResponse:
    return JSONResponse(body, status_code=HTTP_STATUS.get(body["status"], 200))


def error_body(request: Request, status: str, message: str,
               **details: Any) -> dict:
    operation = getattr(request.scope.get("route"), "name", None) or "request"
    return envelope(
        operation=operation, status=status,
        request_scope=request_scope(request),
        warnings=[message], total=None, error={"message": message},
        **details)


def request_scope(request: Request) -> dict:
    scope = {k: (v if len(request.query_params.getlist(k)) == 1
                 else request.query_params.getlist(k))
             for k, v in request.query_params.items()}
    scope.update(request.path_params)
    return scope


# ------------------------------------------------------- strict schemas

def _declared_query_names(route) -> frozenset | None:
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return None
    names = getattr(route, "_amtsgraph_query_names", None)
    if names is None:
        aliases = set()
        pending = [dependant]
        while pending:
            current = pending.pop()
            aliases.update(param.alias for param in current.query_params)
            pending.extend(current.dependencies)
        names = frozenset(aliases)
        route._amtsgraph_query_names = names
    return names


async def strict_query(request: Request) -> None:
    """App-wide dependency: reject undeclared and repeated query arguments.

    Runs after routing, so it sees exactly the parameters the matched
    endpoint declares — also behind a mount prefix (/v1, /amtsgraph).
    """
    declared = _declared_query_names(request.scope.get("route"))
    if declared is None:
        return
    names = [k for k, _ in request.query_params.multi_items()]
    unknown = sorted(set(names) - declared)
    repeated = sorted({k for k in names if names.count(k) > 1})
    if not unknown and not repeated:
        return
    temporal = sorted(set(unknown) & TEMPORAL_ARGUMENTS)
    if temporal:
        raise ContractError(
            "unsupported_temporal_request",
            "this resolver is current_only; historical arguments are not "
            "implemented and are never ignored: " + ", ".join(temporal),
            unknown_arguments=unknown, repeated_arguments=repeated,
            temporal_arguments=temporal)
    parts = []
    if unknown:
        parts.append("unknown arguments: " + ", ".join(unknown))
    if repeated:
        parts.append("repeated arguments: " + ", ".join(repeated))
    raise ContractError("invalid_argument", "; ".join(parts),
                        unknown_arguments=unknown, repeated_arguments=repeated,
                        allowed_arguments=sorted(declared))


def install(app: FastAPI) -> None:
    """Register the typed error rendering on an app (sub-apps included)."""

    @app.exception_handler(ContractError)
    async def _contract_error(request: Request, exc: ContractError):
        return respond(error_body(request, exc.status, exc.message,
                                  **exc.details))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        problems = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", ()) if p not in ("query", "path")]
            problems.append({"name": ".".join(loc) or "request",
                             "reason": err.get("msg", "invalid"),
                             "type": err.get("type")})
        missing = sorted({p["name"] for p in problems if p["type"] == "missing"})
        invalid = sorted({p["name"] for p in problems if p["type"] != "missing"})
        message = "; ".join(
            ([f"missing arguments: {', '.join(missing)}"] if missing else []) +
            ([f"invalid arguments: {', '.join(invalid)}"] if invalid else []))
        return respond(error_body(
            request, "invalid_argument", message or "invalid request",
            missing_arguments=missing, invalid_arguments=invalid,
            problems=problems))
