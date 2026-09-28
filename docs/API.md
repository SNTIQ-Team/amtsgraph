# Amtsgraph API

Amtsgraph is the competence graph of German public authorities: for any place
(PLZ / Gemeinde) and legal matter it resolves the *competent* court instance
chain, and for any Gemeinde it resolves the competent non-court authority
(Ausländerbehörde, Jobcenter, Sozialamt, …) together with its supervisory
chain. Every answer carries `caveats` and `provenance` — clients **must**
display both; for legal use an honest warning beats a confident wrong answer.

There are **two** ways to query the graph:

- **A) Production REST API** — `https://api.sntiq.com/v1` (FastAPI, JSON, CORS).
  This is what the `sntiq.com/dt/amtsgraph` frontend calls through a thin
  client. Source: [`api/main.py`](../api/main.py) (`title="Amtsgraph"`,
  `version="2.2"`).
- **B) Local dev browser** — `tools/browser.py` (port 8400, stdlib only,
  reads `data/atlas.db` read-only). A zero-dependency data browser with its
  own JSON API under `/api/`. Source: [`tools/browser.py`](../tools/browser.py).

Both read the same build artifact, `data/atlas.db`.

### Lexgraph snapshot preconditions at the shared proxy

The deployment config also proxies Lexgraph at `https://api.sntiq.com/lex/`.
Requests with a nonempty `If-Lexgraph-Snapshot` header always reach the live
Lexgraph worker: nginx neither reads nor writes its public cache for them.
The worker returns `409 stale_snapshot` if the requested generation differs
from its loaded corpus. A source outage remains an upstream error, even when
a cached representation is available. Requests without a snapshot retain the
ordinary public cache and Origin buckets.

Header presence is mapped to a boolean, including the literal value `0`.
Passing the raw header to nginx would incorrectly treat that value as false
for both [cache bypass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_cache_bypass)
and [cache writes](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_no_cache).

Run `python3 tests/test_nginx_snapshot_cache.py` on a host with nginx installed.
It starts an isolated nginx and synthetic upstream, exercises warm-cache
preconditions, a corpus switch, and an upstream outage, then removes its
temporary processes and files. No production service or dataset is used.
When applying `deploy/nginx-api.conf` to an existing host, preserve its other
virtual hosts and CORS settings; validate with `nginx -t` before reloading.

---

## Shared concepts

| Term | Meaning |
|------|---------|
| **AGS** | *Amtlicher Gemeindeschlüssel* — the 8-digit key identifying a Gemeinde (e.g. `11000000` = Berlin, `01001000` = Flensburg). The first 2 digits are the Land, the first 5 the Kreis. |
| **Matter code** | Short code for a legal matter (`zivil`, `familie`, `mahn`, …). Court competence is matter-specific: the same place routes to different courts depending on the matter. See [Matters](#get-matters). |
| **rank** | On non-court resolution: `rank == 0` is the competent office itself; `rank > 0` is a supervisory / superordinate body (*Aufsicht*). |
| **role** | On a court chain row: `court` for a court, `prosecution` for a Staatsanwaltschaft. |
| **caveat** | A scoped warning (e.g. "this PLZ spans two court districts", "source is non-authoritative"). Each caveat has `severity`, `text_de`, `source`. |
| **provenance** | Per-authority origin: `{source, url, fetched_at}`. |

---

## Evidence envelope and stable identities (API 3.0)

The operations an agent relies on — `place_resolve`, `competence_resolve`,
`court_chain`, `authority_resolution`, `authority_detail` and the court
registry — answer with one envelope. The legacy fields the web viewer reads
(`chain`, `caveats`, `resolved`, `candidates`, `supervisory`, `needs_ort`,
`options`, …) are kept next to it.

```json
{
  "schema_version": 2,
  "operation": "court_chain",
  "status": "ok",
  "request_scope": {"place_id": "place:jz:10115:BERLIN", "matter": "zivil"},
  "applied_constraints": {"place_id": "place:jz:10115:BERLIN", "matter": "zivil"},
  "ignored_arguments": [],
  "snapshot_id": "amtsgraph-20260928T072529Z-dd16a74face0",
  "temporal": "current_only",
  "temporal_scope": {"mode": "current_only", "historical_supported": false, "basis": "…"},
  "searched": [{"source": "court_chain (justizadressen register observation)", "status": "ok", "count": 5}],
  "evidence": [{"kind": "register_observation", "observed_on": "2026-06-10", "source_url": "https://www.justizadressen.nrw.de/de/justiz/gericht?plz=10115&ort=Berlin&ang=zivil", "…": "…"}],
  "warnings": ["[warn] …", "Register observation only: no statutory basis …"],
  "failed_components": [],
  "total": 5,
  "…operation payload…": "…"
}
```

### Status vocabulary

| `status` | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | Exactly the requested identity, fully served. |
| `partial` | 200 | Served with a gap (e.g. `reason: no_primary_assignment` — only supervisory bodies known). |
| `ambiguous_identity` | 200 | Several candidates (`candidates` / `options`); nothing is selected implicitly. `ambiguity` says whether the place or the competent authority is ambiguous. |
| `not_found_in_searched_scope` | 404 | The complete search of the listed `searched` sources found nothing. Register-gap caveats are still returned. |
| `not_in_corpus` | 404 | The identifier (authority id, court id) is unknown to this build. |
| `invalid_argument` | 400 | Unknown, repeated, missing or malformed arguments — named in `unknown_arguments`, `repeated_arguments`, `missing_arguments`, `invalid_arguments`. |
| `unsupported_temporal_request` | 400 | A date/history argument (`at`, `as_of`, `valid_at`, `known_at`, …). The resolver is `current_only`; dates are rejected, never ignored. |
| `stale_snapshot` | 409 | `snapshot_id=` pins a build that is not the one served (`current_snapshot_id` is returned). |
| `unavailable` | 503 | A store the operation needs is absent (`failed_components`). |

Every route rejects undeclared query arguments with HTTP 400 and their
names (also behind the `/v1` and `/amtsgraph` prefixes). `total` is `0` only
after a complete search; it is `null` when nothing was counted.

### Identities

| Id | Meaning | Stable across builds |
|---|---|---|
| `place:jz:<PLZ>:<ORTK>` | a place of the official court register — the unit of **court** competence | yes (register key) |
| `place:ags:<AGS>` | a municipality — the unit of **office** competence | yes (official key) |
| `court:de:<XJustiz-ID>` | a German court (GDS.Gerichte codelist) | yes (official code) |
| `court:eu:<ECLI court code>` | a court of the EU (`C`, `T`) | yes |
| `authority_id` (integer) | an Amtsgraph row | **no** — local to one build |

### `GET /resolve/place` — `place_resolve`

| Param | Notes |
|---|---|
| `purpose` | required: `court` (returns `place:jz:…`) or `authority` (returns `place:ags:…`) |
| `q` | a PLZ, `"PLZ Ort"`, or a name (exact normalized match, else prefix) |
| `plz`, `ort` | structured alternative to `q`; `ort` only narrows a `plz` |
| `ags` | `purpose=authority` only |
| `limit` | 1–100, default 20; `truncated` and `total` show what was cut |

`status: ok` → `place_id` + `place`; `ambiguous_identity` → `candidates`
(each with `place_id`; court places carry `municipality_id` with
`municipality_link_basis: plz_and_name_match` where the build could link
them). A place name never selects one of several register places.

### `GET /resolve/competence` — `competence_resolve`

`place_id` plus exactly one of `matter` (court matter, needs a `place:jz:`
id) or `kind` (office kind, needs a `place:ags:` id). Court answers are the
`court_chain` payload with `subject: court_matter` and `resolved` = the
first-instance court; office answers are the `authority_resolution` payload
with `subject: authority_kind`.

### `GET /resolve/court` — `court_chain`

Accepts `place_id` **or** the legacy selectors `plz` [+ `ort`] [+ `ortk`]
(never both). An ambiguous place returns `options`/`candidates` with
`place_id`s (`needs_place_id: true`; `needs_ort: true` is kept for older
clients). An unknown matter is `invalid_argument`; a place without a register
chain for the matter is `not_found_in_searched_scope` **with** its caveats
(the register-gap caveat used to be dropped with the bare 404). Every chain
entry carries `court_id` and `assignment_id`.

### Assignments — relation-level evidence

`assignments[]` describes the competence relation itself, not the entity:

| Field | Content |
|---|---|
| `assignment_id` | `assignment:court:<PLZ>:<ORTK>:<matter>:<role>:<position>` or `assignment:competence:<authority_id>:<kind>:<level>:<area>` |
| `relation`, `function` | `competent_for`; `first_instance_listed`, `higher_instance_listed`, `prosecution_listed`, `primary_office`, `higher_level_or_supervisory` |
| `matter`, `territory`, `conditions` | matter code/label, place or area, register notes (e.g. "Kammer für Handelssachen ist eingerichtet.") |
| `temporal` | `current_only`; `valid_from`/`valid_until_exclusive` are `null` with `interval_status: not_recorded`; `observed_on` is the register snapshot day |
| `assignment_basis` | `observed` (register states it), `inferred` (see `derivations`: e.g. `bayernportal_department_name_classification`, `asylblg_title_filter`), `curated` (manual override with `review_due`) |
| `exceptions` | the aggregated caveats that target this assignment, its authority or its court |
| `unresolved_facts` | what the evidence does not settle (case facts, AG/LG by value in `zivil`, **appeal availability** for higher instances) |
| `evidence[]` | typed items: `register_observation` (register, `observed_on`, reproducible `source_url`/query, `record_locator`, `does_not_establish`), `curated_source_review`, `statutory_assignment`, `official_publication` |
| `statutory_basis` | `{status: not_recorded}` unless curated statutory evidence (`pipeline/assignment_evidence.yaml`) matches; it is listed **next to** the register observation, never instead of it |

`legal_effect_verified` is `false` throughout: an observation that a
register listed a court is not a verified legal conclusion.

### Caveat aggregation

Each answer aggregates caveats of every applicable scope — place
(`jz_place`, `plz`), municipality (`gemeinde`), district (`kreis`), `land`,
`matter`, every `authority` and `court` in the answer, every `assignment`, and
`global` — deduplicated, with `applies_via` naming the scope and role. A
caveat narrowed to a matter or office kind applies only to that matter/kind.
Caveats outside their validity interval move to `inactive_caveats`
(`inactive_reason: expired | not_yet_valid`). `critical_warning` is one line
with every active `block`/`warn` caveat; `warnings` lists all active caveats.
`/authorities/{id}` shows the entity's caveats of all matters.

### Courts — `/courts`, `/courts/{court_id}`, `/courts/lookup`, `/courts/mapping`

Mapping record (bulk: `GET /courts/mapping?limit=5000&offset=0`):

```json
{
  "court_id": "court:de:A1000",
  "official_name": "Bundesgerichtshof Karlsruhe",
  "xjustiz_id": "A1000",
  "ecli_court_code": "BGH", "ecli_prefix": "ECLI:DE:BGH:", "ecli_status": "known",
  "ecli_basis": "federal_portal_convention",
  "jurisdiction": "DE", "level": "federal", "land_code": null, "kind": "bundesgericht",
  "status": "active", "unit_type": "court", "parent_court_id": null,
  "aliases": [{"alias": "BGH", "alias_type": "rii_gertyp"}],
  "authority_ids": [],
  "source": "xrepository:gds.gerichte", "observed_on": "2026-06-11"
}
```

- Node set: all courts of the official GDS.Gerichte codelist (prosecutors,
  prisons and other justice bodies are excluded), including dissolved courts
  (`status: dissolved`) and departments (`unit_type: department`,
  `parent_court_id`). Register codes missing from the codelist become nodes
  with `source: justizadressen`.
- `ecli_court_code` is known for the federal courts and the two EU courts.
  Land court codes are `null` (`ecli_status: unknown`); they are never derived
  from a name.
- `authority_ids` link the register cards (contact data, court chains). They
  are build-local; `court_id` is the durable join key for decision corpora
  (decision fallback id: `court:de:<XJustiz>/<YYYY-MM-DD>/<first Az>`).
- `GET /courts/lookup?name=BGH 9. Zivilsenat` → `court:de:A1000`; also
  `ecli=`, `xjustiz=`, `authority_id=`, optional `land=`. Homonyms (two
  `Amtsgericht Fürth`) are `ambiguous_identity`.

### Display heuristics

`/graph` node fields `land`/`kreis` are grouping inferred for display
(`visualization_metadata.legal_effect: false`). Graph edges — including
`appeal` edges derived from chain adjacency — carry no competence;
`/authorities/{id}` marks each edge with `edge_basis` and
`legal_effect_verified: false`.

---

# A) Production REST API — `https://api.sntiq.com/v1`

JSON in, JSON out. CORS is enabled. All examples below use the base URL
`https://api.sntiq.com/v1`; when running the API locally the default base is
`http://127.0.0.1:8000` (`uvicorn api.main:app`).

The database path can be overridden with the `AMTSGRAPH_DB` environment
variable (default: `<repo>/data/atlas.db`).

## Endpoint summary

| Method & path | Purpose |
|---------------|---------|
| `GET /health` | Liveness probe. |
| `GET /version` | Dataset build + source-snapshot dates. |
| `GET /stats` | Corpus counts. |
| `GET /sources` | Record count per data source. |
| `GET /places?q=&limit=` | Place / PLZ typeahead search. |
| `GET /matters` | Legal-matter catalog. |
| `GET /graph` | Full authority graph export (compact arrays). |
| `GET /capabilities` | Machine-readable contract (statuses, identities, current_only). |
| `GET /resolve/place?purpose=&q=` | Stable place ids or explicit ambiguity. |
| `GET /resolve/competence?place_id=&matter=\|kind=` | Competence with relation-level evidence and aggregated caveats. |
| **`GET /resolve/court?place_id=\|plz=&matter=`** | **Primary endpoint** — instance chain for a place × matter. |
| `GET /resolve/authority?place_id=\|ags=&kind=` | Non-court authority resolution for a Gemeinde. |
| `GET /authorities/{id}` | Identity/contact card of one authority (+ court id). |
| `GET /courts`, `/courts/{court_id}`, `/courts/lookup`, `/courts/mapping` | Courts as first-class nodes. |

---

## `GET /health`

Liveness. Returns `200 {"status":"ok"}` when the database is reachable, else
`503`.

```bash
curl -s https://api.sntiq.com/v1/health
```
```json
{ "status": "ok" }
```

---

## `GET /version`

Build metadata and the per-source snapshot dates baked into the dataset.

```bash
curl -s https://api.sntiq.com/v1/version
```
```json
{
  "dataset": "Amtsgraph",
  "built_at": "2026-07-06T08:09:33+00:00",
  "source_snapshots": {
    "openplz": "2026-06-10",
    "justiz":  "2026-06-10",
    "pvog":    "2026-06-10",
    "ba":      "2026-06-10"
  }
}
```

---

## `GET /stats`

Corpus counts. All counts are over **active** authorities (those with
`valid_to IS NULL`) where applicable.

```bash
curl -s https://api.sntiq.com/v1/stats
```
```json
{
  "authorities_active": 19238,
  "court_chain_links":  338873,
  "places":             14057,
  "competences":        77125,
  "parent_edges":       7195,
  "eu_authorities":     11,
  "eu_edges":           31,
  "caveats":            5385
}
```

| Field | Meaning |
|-------|---------|
| `authorities_active` | Active authority records. |
| `court_chain_links` | Rows in `court_chain` (place × matter × instance). |
| `places` | Rows in `jz_place` (justiz place register). |
| `competences` | Competence assignments (authority × kind × area). |
| `parent_edges` | `authority_edge` rows with `relation='parent'`. |
| `eu_authorities` | Curated EU institutions, bodies and courts. |
| `eu_edges` | Evidence-backed relations wholly inside the EU overlay. |
| `caveats` | Total caveats. |

> The **local browser**'s `/api/stats` returns a *different* set
> (`authorities`, `chain_links`, `places`, `competences`, `caveats`,
> `gemeinden`, `built_at`). See section B.

---

## `GET /sources`

Record count contributed by each data source (active authorities only,
descending).

```bash
curl -s https://api.sntiq.com/v1/sources
```
```json
[
  { "source": "destatis",       "authorities": 11042 },
  { "source": "justizadressen", "authorities": 1130 },
  { "source": "ba",             "authorities": 401 },
  { "source": "pvog",           "authorities": 300 }
]
```

> This endpoint carries **counts only** — no trust levels or snapshot dates.
> Snapshot dates are exposed by `GET /version` (`source_snapshots`), and
> per-source trust weights by the browser's `GET /api/provenance` (section B).

---

## `GET /places`

Place / authority typeahead search — the disambiguation picker that feeds
the court flow.

Query parameters:

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `q` | string | *(required)* | City name or PLZ. A 3–5 digit query is treated as a **PLZ prefix**; otherwise it matches Gemeinde names by prefix → substring → fuzzy (difflib, catches typos like `erdnig` → Erding). |
| `limit` | int | `10` | Max matches. |

```bash
curl -s 'https://api.sntiq.com/v1/places?q=Flensburg&limit=5'
```
```json
{
  "query": "Flensburg",
  "matches": [
    {
      "ags": "01001000",
      "name": "Flensburg",
      "kind": "Kreisfreie Stadt",
      "kreis": "Flensburg",
      "land": "Schleswig-Holstein",
      "plz_count": 5,
      "plz": "24937"
    }
  ],
  "court_register_only": []
}
```

Notes:
- Fuzzy matches carry `"fuzzy": true`.
- A digit query returns the PLZ that actually matched on each row (not the
  Gemeinde's lowest PLZ).
- `court_register_only` holds `jz_place` rows (`plz`, `ort`, `ortk`) that
  have no linked Gemeinde — places known only to the court register.

---

## `GET /matters`

The legal-matter catalog, ordered `core DESC, grp`. Fields per row:
`code`, `label_de`, `grp` (group: `ordentliche` / `fach` / `sonder`),
`core` (`1` = one of the everyday matters, `0` = specialized).

```bash
curl -s https://api.sntiq.com/v1/matters
```
```json
[
  { "code": "zivil",   "label_de": "Allgemeiner Gerichtsstand (Zivil)", "grp": "ordentliche", "core": 1 },
  { "code": "familie", "label_de": "Familienrechtssachen",              "grp": "ordentliche", "core": 1 },
  { "code": "arbeit",  "label_de": "Arbeitsgerichtssachen",             "grp": "fach",        "core": 1 }
]
```

The **14 core matters** (`core = 1`) — these are what the frontend offers by
default:

| Group | Codes |
|-------|-------|
| `ordentliche` | `zivil`, `familie`, `mahn`, `insolv`, `insolvver`, `zvg`, `zwangsvoll`, `grundbuch`, `nachlass`, `betreu`, `handelsreg` |
| `fach` | `arbeit`, `sozial`, `verwaltung` |

The full catalog is larger (≈36 rows). Non-core (`core = 0`) matters cover
specialized jurisdictions such as `finanz`, `kfh` (Kammer für Handelssachen),
`landwirt`, `urheber`, `versich`, `ecsccj`/`ecsccae` (Small Claims),
`vornagesch`, `zentvollst`, and the register matters `gesell`, `verein`,
`partner`.

---

## `GET /graph`

The full organisational-web export for visual exploration: every **active**
authority plus every edge between active authorities. Compact arrays; the
response is cached in-process and marked `Cache-Control: public, max-age=3600`.

```bash
curl -s https://api.sntiq.com/v1/graph
```
```json
{
  "nodes": [
    [1024, "amtsgericht", "Amtsgericht Mitte", "11", "11000"],
    [1025, "landgericht", "Landgericht Berlin II", "11", "11000"]
  ],
  "edges": [
    [1024, 1025, "appeal"],
    [20001, 20002, "judicial_review"]
  ],
  "edge_meta": [{
    "from": 20001, "to": 20002, "relation": "judicial_review",
    "matter": null, "note": "Gerichtliche Kontrolle nur im Rahmen …",
    "source": "eu_curated", "source_url": "https://eur-lex.europa.eu/…"
  }],
  "kreise": { "11000": "Berlin" }
}
```

- **`nodes`**: `[id, kind, name, land, kreis]`. `land` is a 2-digit and
  `kreis` a 5-digit AGS prefix, derived from competence areas / postal codes
  and propagated across edges.
- **`edges`**: uniformly `[from_id, to_id, relation]` for backward-compatible
  streaming/destructuring.
- **`edge_meta`**: sparse objects for the curated EU edges, keyed by
  `from`/`to`/`relation`, with `matter`, the legally limiting `note`, and
  `source`/`source_url`. This keeps the precise scope without multiplying the
  payload for thousands of German edges.
  EU relation types are `institutional_part`, `political_accountability`,
  `judicial_review`, `co_legislation`, `reporting_accountability`,
  `financial_audit`, `maladministration_review`, `sectoral_oversight` and the
  judicial `appeal`.
- **`kreise`**: `{ags → name}` lookup for the 5-digit Kreis codes.

> This endpoint is deliberately compact. The **weighted** graph (edge `delta`,
> `trust`, per-node source trust) and shortest-trusted-path traversal are
> exposed only by the browser's `/api/graph/node` and `/api/graph/traverse`
> (section B).

---

## `GET /resolve/court` — primary endpoint

Resolves the competent **court instance chain** (plus prosecution chain) for a
place and matter.

Query parameters:

| Param | Type | Notes |
|-------|------|-------|
| `plz` | string | *(required)* Postal code. |
| `matter` | string | *(required)* Matter code (see `/matters`). |
| `ort` | string | *(optional)* Disambiguates when one PLZ spans several court districts. |

### Case 1 — one PLZ, several court districts

If the PLZ maps to more than one court district (`ortk`) and no `ort` was
given, the response asks the client to pick:

```json
{
  "needs_ort": true,
  "options": [
    { "ort": "Neustadt an der Weinstraße", "ortk": "NEUSTADT_WEINSTR" },
    { "ort": "Neustadt in Holstein",       "ortk": "NEUSTADT_HOLSTEIN" }
  ]
}
```

Re-issue the request with the chosen `ort`.

### Case 2 — resolved chain

```bash
curl -s 'https://api.sntiq.com/v1/resolve/court?plz=10115&matter=zivil'
```
```json
{
  "place": { "plz": "10115", "ort": "Berlin" },
  "matter": "zivil",
  "chain": [
    {
      "position": 1,
      "role": "court",
      "note": null,
      "id": 1024,
      "kind": "amtsgericht",
      "name": "Amtsgericht Mitte",
      "legal_form": null,
      "street": "Littenstraße 12-17",
      "plz": "10179",
      "city": "Berlin",
      "postal_address": "10556 Berlin",
      "phone": "+49 30 90171-0",
      "fax": null,
      "email": null,
      "web": "https://www.berlin.de/gerichte/amtsgericht-mitte/",
      "hours": null,
      "erv_note": "ERV: beA/EGVP",
      "lat": 52.516,
      "lon": 13.410,
      "fetched_at": "2026-06-10",
      "valid_from": "2026-06-10",
      "valid_to": null,
      "external_ids": { "xjustiz": "DEBEXXXXXX" },
      "provenance": {
        "source": "justizadressen",
        "url": "https://www.justizadressen.nrw.de/...",
        "fetched_at": "2026-06-10"
      }
    }
  ],
  "caveats": []
}
```

The `chain` is ordered by instance (`position`) with the prosecution rows
first (`role='prosecution'`). For matter `zivil` at Berlin `10115` the chain
is, for example: Staatsanwaltschaft Berlin → Generalstaatsanwaltschaft Berlin
(prosecution), then Amtsgericht Mitte → Landgericht Berlin II → Kammergericht
(court).

**Chain-row shape:** each row is `{position, role, note}` merged with the full
**authority card** (see [Authority card](#authority-card)).

Errors (envelope, see "Status vocabulary"): `400 invalid_argument` for a
malformed PLZ or an unknown matter; `404 not_found_in_searched_scope` if no
register place matches, or if the register lists no chain for that matter at
the place (the body still carries the place's caveats).

---

## `GET /resolve/authority`

Resolves the competent **non-court** authority of a given `kind` for a
Gemeinde (by AGS), together with any supervisory bodies.

Query parameters:

| Param | Type | Notes |
|-------|------|-------|
| `ags` | string | *(required)* 8-digit AGS. |
| `kind` | string | *(required)* Authority kind (see below). |

Resolution walks competence rows at Gemeinde → Kreis → Land → PLZ level and
splits hits by `rank`: `rank == 0` are candidate offices, `rank > 0` are
supervisory bodies.

**Non-court kinds** include:
`auslaenderbehoerde`, `jobcenter`, `sozialamt`, `asylblg_behoerde`,
`buergeramt`, `standesamt`, `gewerbeamt`, `jugendamt`, `wohngeldstelle`,
`arbeitsagentur`, `familienkasse`, `aufsichtsbehoerde`, `ministerium`,
`sonstige`.

**Court kinds are rejected** here (`400`) — resolve them via `/resolve/court`,
because court competence is matter-specific. Rejected kinds:
`amtsgericht`, `landgericht`, `oberlandesgericht`, `sozialgericht`,
`verwaltungsgericht`, `arbeitsgericht`, `finanzgericht`.

```bash
curl -s 'https://api.sntiq.com/v1/resolve/authority?ags=01001000&kind=auslaenderbehoerde'
```
```json
{
  "gemeinde": "Flensburg",
  "kind": "auslaenderbehoerde",
  "resolved": {
    "id": 5567,
    "kind": "auslaenderbehoerde",
    "name": "Stadt Flensburg – Einwanderungsbüro / Immigration Office",
    "legal_form": "Kommunalbehörde",
    "street": "Rathausplatz 1",
    "plz": "24937",
    "city": "Flensburg",
    "phone": "+49 461 85-0",
    "email": "einwanderung@flensburg.de",
    "web": "https://www.flensburg.de/...",
    "external_ids": { "pvog_oe": "..." },
    "provenance": { "source": "pvog", "url": "...", "fetched_at": "2026-06-10" }
  },
  "candidates": [],
  "supervisory": [],
  "caveats": []
}
```

Second example — an AsylbLG benefits authority (relevant for asylum cases):

```bash
curl -s 'https://api.sntiq.com/v1/resolve/authority?ags=01002000&kind=asylblg_behoerde'
```
```json
{
  "gemeinde": "Kiel",
  "kind": "asylblg_behoerde",
  "resolved": null,
  "candidates": [
    { "id": 6001, "name": "Amt für Wohnen und Grundsicherung – Sachbereich Leistungen nach dem AsylbLG", "city": "Kiel", "...": "…" }
  ],
  "supervisory": [],
  "caveats": []
}
```

Response shape:

| Field | Meaning |
|-------|---------|
| `gemeinde` | Name of the resolved Gemeinde. |
| `kind` | Echoed kind. |
| `resolved` | The single competent office **card**, or `null` when ambiguous. |
| `candidates` | Array of office cards when more than one `rank==0` office matches (`resolved` is then `null`). |
| `supervisory` | Cards of superordinate / supervisory bodies (`rank > 0`). |
| `caveats` | Gemeinde-scoped caveats. |

Errors (envelope): `404 not_found_in_searched_scope` for an unknown AGS or
when no authority of that kind is known; `400 invalid_argument` for a court
kind or an unknown kind. Several rank-0 offices are `ambiguous_identity`
(`ambiguity: competent_authority`); only supervisory bodies is `partial`
(`reason: no_primary_assignment`).

---

## `GET /authorities/{authority_id}`

Full card for one authority plus its outgoing and incoming edges.

```bash
curl -s https://api.sntiq.com/v1/authorities/1024
```

Returns the [authority card](#authority-card) with three extra keys:

- `related`: outgoing edges
  `[{relation, matter, note, source, source_url, id, name, kind}]`.
- `related_incoming`: the same shape for incoming edges; `id`/`name`/`kind`
  identify the source node, so consumers must not reverse the relation label.
- `caveats`: authority-scoped caveats.

`404` if the id is unknown.

---

## Authority card

The reusable authority object embedded in court chains, non-court resolutions,
and `/authorities/{id}`. All columns except the raw `source*` columns are
exposed; `xjustiz` and other external ids move under `external_ids`, and origin
moves under `provenance`.

| Field | Type | Notes |
|-------|------|-------|
| `id` | int | Authority id. |
| `kind` | string | e.g. `amtsgericht`, `auslaenderbehoerde`, `landratsamt`. |
| `name` | string | Official name. |
| `name_norm` | string | Normalized name (search key). |
| `legal_form` | string \| null | e.g. `Körperschaft des öffentlichen Rechts`. |
| `street` | string \| null | Street / house address. |
| `plz` | string \| null | Postal code of the address. |
| `city` | string \| null | City. |
| `postal_address` | string \| null | Postfach / postal address (may differ from `street`). |
| `phone` / `fax` / `email` / `web` | string \| null | Contact channels. |
| `hours` | string \| null | Opening hours. |
| `erv_note` | string \| null | Electronic legal traffic note (beA/EGVP/De-Mail …). |
| `lat` / `lon` | number \| null | Coordinates. |
| `fetched_at` | string | Ingestion date. |
| `valid_from` / `valid_to` | string \| null | Validity window (`valid_to = null` ⇒ active). |
| `external_ids` | object | `{scheme → value}`. Schemes: `xjustiz`, `ba_traeger`, `bayernportal_oe`, `pvog_oe`. |
| `provenance` | object | `{source, url, fetched_at}`. |

---

# B) Local dev browser — `tools/browser.py`

A single-file, zero-dependency data browser. It serves `data/atlas.db`
**read-only** and exposes its own JSON API under `/api/`, plus a 4-tab HTML UI
(*Wiki & Realtime*, *Git-Log* (provenance), *Hierarchie*, *Graph*) and the
brand mark at `/sntiq.svg`.

```bash
python3 tools/browser.py            # http://127.0.0.1:8400/
python3 tools/browser.py --port 8400
```

## `/api/` endpoints

| Path | Purpose |
|------|---------|
| `GET /api/lands` | 16 Länder with Kreis counts. |
| `GET /api/kreise?land=<code>` | Kreise of a Land (with kind, Regierungsbezirk, Gemeinde count). |
| `GET /api/gemeinden?kreis=<ags>` | Gemeinden of a Kreis. |
| `GET /api/gemeinde?ags=<ags>` | One Gemeinde: PLZ list, competent authorities (with `competence_kinds` and `rank`), caveats, linked `jz_places`. |
| `GET /api/search?q=<str>` | Instant search → `{gemeinden, authorities, plz}`. |
| `GET /api/matters` | Matter catalog (`code`, `label_de`, `grp`, `core`). |
| `GET /api/court?plz=&ortk=&matter=` | Court chain for a place × matter → `{chain, caveats}`. Note this takes `ortk` (court-district key), not `ort`. |
| `GET /api/stats` | `{authorities, chain_links, places, competences, caveats, gemeinden, built_at}`. |
| `GET /api/provenance` | Build / ingestion log: one "commit" per `(source, kind)` with count, snapshot `date`, and `trust`; plus `snapshots`, `trust`, `superseded` (deduped-away record count), `built_at`, `sources`. |
| `GET /api/hierarchy` | Land → Kreis tree with live authority counts (`{lands, total_authorities}`). |
| `GET /api/seed` | Top-degree authorities (default graph seed). |
| `GET /api/graph/node?id=<id>` | One authority + its edges + competences, for fractal graph expansion. Carries edge `delta`/`trust` and per-node source `trust`. |
| `GET /api/graph/traverse?src=&dst=` | Dijkstra **most-trusted path** between two authorities (cost = `-log(conductance × trust)`). |

Example — court chain via the browser API:

```bash
curl -s 'http://127.0.0.1:8400/api/court?plz=10115&ortk=BERLIN&matter=zivil'
```
```json
{
  "chain": [
    {
      "position": 1, "role": "court",
      "name": "Amtsgericht Mitte", "kind": "amtsgericht",
      "address": "Littenstraße 12-17", "postal_address": "10556 Berlin",
      "phone": "+49 30 90171-0", "fax": null, "email": null,
      "web": "https://www.berlin.de/gerichte/amtsgericht-mitte/",
      "erv_note": "ERV: beA/EGVP", "xjustiz_id": "DEBEXXXXXX"
    }
  ],
  "caveats": []
}
```

> The browser is a **local testing tool**, not a public API. Its stats,
> provenance and weighted-graph shapes differ from the production REST API
> above.

---

## Licensing

Amtsgraph is **dual-licensed by audience** (see
[`LICENSING.md`](../LICENSING.md)):

- **Non-commercial** (individuals, human-rights / non-profit, research,
  journalism, education): **code** under
  [Apache-2.0](../LICENSE-APACHE-2.0); **database contents, documentation and
  media** under [CC BY-NC-SA 4.0](../LICENSE-CC-BY-NC-SA-4.0).
- **Commercial / corporate / governmental**: code and media alike under the
  [SNTIQ-CM License v1.0](../LICENSE-SNTIQ-CM) — free written permission
  required, with a non-harm covenant, attribution, and case-by-case
  reciprocity.

Attribution string: `Contains material from Amtsgraph by SNTIQ —
https://github.com/SNTIQ-Team/amtsgraph`.

The dataset is general information, **not legal advice**. Underlying records
originate from official German public registers whose own terms govern the raw
records — see [`docs/SOURCES.md`](SOURCES.md).

The public API powers **[sntiq.com/dt/amtsgraph](https://sntiq.com/dt/amtsgraph)**.
