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
  `version="2.1"`).
- **B) Local dev browser** — `tools/browser.py` (port 8400, stdlib only,
  reads `data/atlas.db` read-only). A zero-dependency data browser with its
  own JSON API under `/api/`. Source: [`tools/browser.py`](../tools/browser.py).

Both read the same build artifact, `data/atlas.db`.

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
| **`GET /resolve/court?plz=&matter=&ort=`** | **Primary endpoint** — instance chain for a place × matter. |
| `GET /resolve/authority?ags=&kind=` | Non-court authority resolution for a Gemeinde. |
| `GET /authorities/{id}` | Full detail of one authority. |

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
  "authorities_active": 12873,
  "court_chain_links":  41250,
  "places":             8412,
  "competences":        30120,
  "parent_edges":       9004,
  "caveats":            215
}
```

| Field | Meaning |
|-------|---------|
| `authorities_active` | Active authority records. |
| `court_chain_links` | Rows in `court_chain` (place × matter × instance). |
| `places` | Rows in `jz_place` (justiz place register). |
| `competences` | Competence assignments (authority × kind × area). |
| `parent_edges` | `authority_edge` rows with `relation='parent'`. |
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
    [1024, 1025, "appeal"]
  ],
  "kreise": { "11000": "Berlin" }
}
```

- **`nodes`**: `[id, kind, name, land, kreis]`. `land` is a 2-digit and
  `kreis` a 5-digit AGS prefix, derived from competence areas / postal codes
  and propagated across edges.
- **`edges`**: `[from_id, to_id, relation]`. `relation` is one of `parent`,
  `appeal`, `supervision`, `successor`.
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

Errors: `404` if the PLZ is unknown, or if no chain exists for that
matter at the place.

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

Errors: `404` for an unknown AGS or when no authority of that kind is known;
`400` for a court kind.

---

## `GET /authorities/{authority_id}`

Full card for one authority plus its outgoing edges.

```bash
curl -s https://api.sntiq.com/v1/authorities/1024
```

Returns the [authority card](#authority-card) with two extra keys:

- `related`: outgoing edges `[{relation, matter, id, name, kind}]`.
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
| `kind` | string | e.g. `amtsgericht`, `auslaenderbehoerde`. |
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
