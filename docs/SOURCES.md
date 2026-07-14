# Data sources (verified 2026-06-10)

Every endpoint below was tested live on 2026-06-10 unless noted.

## 0. EU institutional overlay — primary law + official institution pages

`pipeline/eu_institutions.yaml` is a small reviewed overlay, verified
2026-07-14.  Article 13 TEU supplies the seven-institution set; official
CURIA pages distinguish the CJEU institution from its two courts.  Each
relation cites the applicable Treaty provision or official specialist page
(including EDPS Regulation (EU) 2018/1725 scope).  The loader accepts only
HTTPS `europa.eu` provenance and rejects generic EU `supervision` edges.

- Article 13 TEU: https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:12016M013
- Preliminary rulings, Article 267 TFEU: https://eur-lex.europa.eu/eli/treaty/tfeu_2012/art_267/oj/deu
- CJEU structure: https://curia.europa.eu/site/jcms/d2_5390/de/ueber-den-gerichtshof-der-europaeischen-union
- EU institution overview: https://european-union.europa.eu/institutions-law-budget/institutions-and-bodies/types-institutions-and-bodies_de
- EDPS supervisory scope: https://www.edps.europa.eu/data-protection/our-role-supervisor_de

This layer is institutional orientation, not a competence resolver: it
creates no blanket supervisory relation from an EU body to a German office.
Treaty-defined preliminary rulings may bind the referring German court on
the EU-law question, while that court keeps and decides the national case;
the effect is documented rather than mislabelled as an appeal chain.

## 1. PVOG Suchdienst API — authorities & competences (the big one)

Official Portalverbund Online-Gateway search service (FITKO). Aggregates the
Zuständigkeitsfinder data of all 16 Länder: Leistungen (services, LeiKa-typed),
Organisationseinheiten (authorities with addresses, contacts, opening hours,
geo-coordinates), Zuständigkeiten (which office serves which area), and even
JZuFi (justice competence finder). **No API key required** for the search
endpoints. Objects carry `lastUpdate` → incremental sync.

- Base: `https://pvog.fitko.net/suchdienst/api` (old `pvog.fitko.de` dies 2026-08-01)
- OpenAPI: `https://stage.pvog.fitko.net/suchdienst/api/v3/api-docs/suchen`
- Docs: https://produktportal.pvog.fitko.de/api/suchdienst/ , https://docs.fitko.de/resources/pvog-suchdienst-api/

Tested calls (all HTTP 200):

```bash
# place search incl. disambiguation primitives (ARS, AGS, PLZ, zipCodeCount)
GET /v2/locations?q=Nördlingen          → [{ars: 097790194194, ags: 09779194, zip: 86720}]
GET /v2/locations/suggestions?q=...     → autocomplete

# services available at a place (q filter, size >= 5!)
GET /v7/servicedescriptions/{ars}?q=Aufenthaltserlaubnis&size=5

# responsible authority for service at place  ← the money shot
GET /v2/organisationunits/titles?ars=097790194194&lbId=L100042.LB.101954
   → [{id: "L100042.OE.2292", title: "Landratsamt Donau-Ries", role: "Zuständige Stelle"}]

# full authority record (addresses w/ geo, email, hours, web)
GET /v5/organisationunits/detail?q=L100042.OE.2292

# justice competence finder (v1 works without the XZuFiVersion header)
GET /v1/relations/jzufi-2-3?ars=...&lbids=...

# bulk: all services for an ARS as CSV (good for harvesting)
GET /v5/servicedescriptions/csv?...
```

Harvest strategy: iterate all ~11k Gemeinde-ARS over the LeiKa-IDs of our
target kinds (Aufenthalt → Ausländerbehörde, Personalausweis → Bürgeramt,
Sozialhilfe → Sozialamt, Bürgergeld → Jobcenter, …) → `organisationunits`.
For full dumps there is also the PVOG **Bereitstelldienst** (XZuFi, needs
API key, see docs.fitko.de) — apply for a key if harvesting proves slow.

## 2. OpenPLZ API — PLZ ↔ Gemeinde (AGS) ↔ Kreis ↔ Land

Open project, sourced from Deutsche Post (quarterly) + Destatis. Free, no key.
Max `pageSize=50`.

- Base: `https://openplzapi.org/de/`
- Tested: `GET /de/Localities?postalCode=86720` and `?name=Nördlingen`
  → `{postalCode, name, municipality{key: 09779194, …}, district{key: 09779}, federalState{key: 09}}`
- Also `/de/Municipalities`, `/de/Districts`, `/de/FederalStates`, `/de/Streets`
- Raw data pipeline: https://github.com/openpotato/openplzapi.data

This replaces v1's degraded `PLZOrtMapDE.csv` with a maintained M:N mapping
including the AGS key — the join we never had.

## 3. Destatis GV-ISys (AuszugGV) — canonical municipality register

Quarterly XLSX with every Gemeinde, its AGS/ARS, type codes, population.
The authoritative spine; v1 already used it (`bac/MIN.REG.AMT/`), keep the
parser, output to staging instead of JSON.
https://www.destatis.de/DE/Themen/Laender-Regionen/Regionales/Gemeindeverzeichnis/

## 4. justizadressen.nrw.de — courts (Orts- und Gerichtsverzeichnis)

Joint federal/state court directory. **No official dump** (a FragDenStaat
FOI request was rejected: https://fragdenstaat.de/en/request/daten-des-orts-und-gerichtsverzeichnis-des-justizportal-des-bundes/),
**but** the site has clean HTTP endpoints — no Selenium needed
(both verified 2026-06-10, used by `pipeline/fetch_justiz.py`):

```bash
# place register (JSON): all Orte for a PLZ + structural court keys.
# One PLZ can hold 9 Orte in 9 different court districts (e.g. 25712)!
GET /de/justiz/orte?plzort=25712&filter=gericht
  → [{plz, ortk, ort, plzm, gsbl, gsregbez, gskreis, gsort, gebm}, ...]

# competent courts per (place × MATTER): full instance chain as HTML
# (h6 cards: name, addresses, phone, XJustiz-ID, ERV status).
# `ang` = matter code; the form lists ~37 (Mahnverfahren, both insolvency
# types, Grundbuch, Nachlass, family, registers, ...) — see matter table.
GET /de/justiz/gericht?plz=10115&ort=Berlin&ang=zivil → AG Mitte … KG
GET /de/justiz/gericht?plz=10115&ort=Berlin&ang=mahn
  → Amtsgericht Wedding — Zentrales Mahngericht Berlin-Brandenburg
```

Notes: `ort` must match the register spelling exactly (UTF-8, case
sensitive); `size`-independent; "Ihre Suche ergibt leider keinen Treffer"
marks a miss. Places sharing the gs key tuple share courts — harvested once
per (gs_key × matter) with random spot-verification.

XJustiz court codelist (IDs for re-identification):
https://www.xrepository.de/ (Codeliste GDS.Gerichte).

### PVOG harvest lessons (pilot, Saarland 2026-06-10)

- Probe terms must be **renaming-proof**: Bürgergeld is being renamed back
  to (Neue) Grundsicherung, so SGB-II probes carry old + new + statutory
  names (`pvog_leika.yaml`).
- Service descriptions are long HTML and produce false friends (Wohngeld
  pages mention Bürgergeld) → unit-title `deny` lists + `prefer` ranking.
- **gE Jobcenter are structurally underrepresented in PVOG** (they are
  BA-run; Land editorial systems often don't model them). zkT Jobcenter
  resolve fine. Verdict: PVOG is *not* the authoritative source for
  Jobcenter — the BA Trägerliste (source 5) is; PVOG JC hits are kept only
  where they name an actual Jobcenter unit, everything else is an honest
  miss until fetch_ba.py fills it.
- **AsylbLG**: competence practice varies by Kreis (Sozialamt / dedicated
  office / department); harvested with `multi: true` — all plausible units
  are stored and the API returns candidates + a "confirm locally" caveat
  instead of guessing. Coverage is partial (editorial systems often don't
  publish the service) — a known, honest gap.

## 5. Bundesagentur für Arbeit — Jobcenter / Arbeitsagenturen / Familienkassen

- v1 approach still valid: arbeitsagentur.de "Dienststellen vor Ort" scrape
  (`bac/JC_AFA/`) + per-site zkT/gE detection. Gap found in v1: 43 (mostly
  eastern) districts had no Jobcenter — these are zkT, which the BA site
  underrepresents.
- Fix: BA publishes the authoritative **list of all Jobcenter incl. zkT**
  ("Trägerinformationen / Liste der Jobcenter", SGB II) as downloadable file —
  fetch from statistik.arbeitsagentur.de (SGB-II-Trägerliste) and merge.
- bund.dev documents related BA APIs: https://bund.dev/

## 6. BAMF / xRepository — Ausländerbehörden

Codelist "ABH-Kennung" (all valid Ausländerbehörden incl. IDs); v1 already
has a copy (`bac/Datasets/abhkennung_62.0.json`). Refresh from
https://www.xrepository.de/ (urn:de:xauslaender:codelist:abhkennung).
PLZ mapping comes from PVOG (kind=auslaenderbehoerde), the codelist is for
identity/completeness checks.

## Cadence summary

| Source | What | Cadence | Mechanism |
|---|---|---|---|
| PVOG Suchdienst | Behörden + competences + hours | weekly (incremental via lastUpdate) | REST, no key |
| OpenPLZ | PLZ↔AGS | quarterly | REST, no key |
| Destatis AuszugGV | Gemeinde register | quarterly | XLSX download |
| justizadressen | courts | monthly | Selenium scrape per PLZ |
| BA Trägerliste + vor-Ort | Jobcenter/AA | monthly | download + scrape |
| xRepository | codelists (courts, ABH) | on release | XML/JSON download |
