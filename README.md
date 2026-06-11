# ⚖️ Amtsgraph

**The open, validated graph of German public authorities — who is competent,
where, and for what.**

Amtsgraph answers the question every administrative or court procedure in
Germany starts with: *which authority is responsible for my matter at my
location?* It covers:

- **Courts** — the exact instance chain (first instance → appeal → highest
  court, plus prosecution offices) for **14 legal matters** at every one of
  **14,057 places**: civil claims, family law, dunning procedures
  (Mahnverfahren), both insolvency types, land register, probate,
  guardianship, commercial registers, labour, social and administrative
  law. With filing addresses, XJustiz IDs and electronic-filing (ERV)
  status.
- **Agencies** — Ausländerbehörden, Jobcenter (with gE/zkT legal form from
  the official BA register), Sozialämter, AsylbLG benefit offices,
  Bürgerämter, Standesämter, Gewerbeämter, Jugendämter, Wohngeldstellen —
  resolved per municipality (AGS), with supervisory bodies ranked
  separately from the offices you actually apply at.
- **The organisational web** *(Bavaria, pilot)* — every Abteilung,
  Fachbereich and Sachgebiet of all 96 Kreisverwaltungsbehörden as a
  navigable parent/child graph, down to department-level contacts
  (`asyl@landratsamt-roth.de` instead of a generic switchboard).

| | |
|---|---|
| Active authorities | **19,463** |
| Court-chain links (place × matter × instance) | **338,873** |
| Competence assignments | **79,630** |
| Organisational parent edges (Bavaria) | **7,195** |
| Derived appeal edges (cross-checked) | **1,065** |
| Municipalities / postal codes covered | **10,950 / 8,205** |
| Honest-gap caveats served with answers | **5,369** |

## Why this dataset is different

**1. No grouping assumptions.** German court competence resolves per
*(postal code, locality, matter)* — one rural postal code can contain nine
villages in nine different court districts, and Berlin's 10115 and 12555
share every structural register key yet belong to different Amtsgerichte.
Amtsgraph queried the official register **once per place and matter**
(196,798 combinations) instead of extrapolating.

**2. A validation gate, not a scraper dump.** Every build must pass
coverage, collision, contiguity and cross-check tests before it can
replace the previous database. Builds that fail stay on disk for
inspection and never go live. The gate caught real bugs during
development — including a grouping assumption that nationwide
spot-verification disproved (20/60 mismatches) and was removed entirely.

**3. Honesty over confidence.** When the official source itself is
ambiguous (AsylbLG practice varies by district) or empty (0.36 % of
place×matter combinations), the answer carries an explicit `caveat`
instead of a silent guess or a silent 404. Supervisory bodies
(Regierungen, ministries) are ranked `1` and never served as *the* office
to apply at.

**4. Full provenance.** Every authority row records its source, source
URL and fetch timestamp. All XJustiz IDs in the dataset verify against
the official GDS.Gerichte codelist (0 outliers).

## Data model

```
GEO plane                AUTHORITY plane             COURT TRUTH
─────────                ───────────────             ───────────
land → kreis → gemeinde  authority (typed, with      court_chain
        ▲    ▲             provenance + validity)     (plz × ortk × matter ×
 gemeinde_plz (M:N)        ▲         ▲                 position → authority)
        │                  │         │                exact harvested
       plz            competence  authority_edge      instance chains
        │             (kind × area  parent / appeal /
   jz_place            × rank)      supervision / successor
   official court
   resolution register   matter (14+ legal-matter taxonomy)
                         caveat  (warnings the API must surface)
```

Key invariants (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):

- Courts are answered **verbatim from harvested chains**; the edge graph
  is derived and cross-checked, never the primary answer.
- Authority identity is **(XJustiz-ID, name)** — departments share their
  parent court's ID at different filing addresses (Insolvenzgericht!),
  and homonym courts (two Amtsgerichte Fürth) share a name with
  different IDs. Neither may be merged.
- `competence.kind` lives on the relation, not the authority — one
  Landratsamt legitimately wears the ABH, Sozialamt and AsylbLG hats.
- `competence.rank`: `0` = the office to apply at, `1` = übergeordnete /
  supervisory body (also linked via `authority_edge: supervision`).

## Quickstart

The database is **not committed to git** — download it from the latest
[GitHub Release](https://github.com/SNTIQ-Team/amtsgraph/releases)
(`atlas.sqlite` + SHA-256 + validation report) or take the JSONL export
from the [Hugging Face dataset](https://huggingface.co/datasets/SNTIQ-Team/amtsgraph).

```bash
pip install -r requirements.txt

# get the database (verify the checksum!)
gh release download -p 'atlas.sqlite*' -D data/
sha256sum -c data/atlas.sqlite.sha256 && mv data/atlas.sqlite data/atlas.db

# explore
python3 tools/browser.py            # hierarchical browser + search on :8400
sqlite3 data/atlas.db               # or query directly — see examples/
uvicorn api.main:app                # API: /resolve/court, /stats, /health …

# or rebuild from sources (full Germany, ~4–6 h polite crawl, resumable)
cd pipeline && ./run_full.sh
python3 monitor.py                  # live btop-style harvest dashboard
```

Every release ships `validation-report.json`, `coverage.md` and
`sources-lock.yaml` — the machine-readable trust layer: what was checked,
what passed, which source snapshots the build was made from, and where
the known gaps are.

Worked queries (SQL, Python, HTTP) live in [examples/](examples/).

## Sources & refresh

No single upstream source exists; every source is registered in
[pipeline/sources.yaml](pipeline/sources.yaml) with its own resumable
fetcher and cadence. Highlights (full list and verified endpoints in
[docs/SOURCES.md](docs/SOURCES.md)):

| Source | Provides | Cadence |
|---|---|---|
| Orts- und Gerichtsverzeichnis (justizadressen.nrw.de) | court chains per place × matter | monthly |
| PVOG Suchdienst (FITKO) | agencies, competences, contacts, hours | weekly |
| BA Gebietsstruktur SGB II | all 404 Jobcenter incl. gE/zkT legal form | monthly |
| BayernPortal organigrams | Bavaria's departmental web (7,217 units) | monthly |
| Destatis Gemeindeverzeichnis | AGS/ARS municipal spine | quarterly |
| OpenPLZ | postal code ↔ municipality (M:N) | quarterly |
| xRepository (GDS.Gerichte, ABH-Kennung) | identity benchmarks | on release |

Pipeline discipline: `fetch → immutable snapshot → build → validate →
atomic swap`. Manual corrections live exclusively in
[pipeline/overrides/](pipeline/overrides/README.md) as reviewable,
expiring YAML patches that survive every rebuild.

## Limitations — read before relying on it

- **This is general information, not legal advice.** Competence rules
  (örtliche/sachliche Zuständigkeit) have statutory exceptions the
  dataset cannot capture; always confirm before filing anything with a
  deadline. Caveat rows mark known ambiguities — display them.
- Department-level depth currently covers **Bavaria's 96
  Kreisverwaltungsbehörden**; other Länder resolve to the authority
  level. Metropolitan cities (München, Nürnberg) maintain their own
  portals and appear at the depth the Land portal publishes.
- AsylbLG coverage reflects what Land editorial systems publish —
  partial by nature, marked with caveats.
- Data was harvested from official public registers; their respective
  terms apply to the underlying records (see
  [docs/SOURCES.md](docs/SOURCES.md)).

## License

Dual-licensed **by audience** — see [LICENSING.md](LICENSING.md):

- **Non-commercial** (individuals, NGOs, human-rights work, research,
  journalism, education): code under
  [Apache-2.0](LICENSE-APACHE-2.0), data/docs/media under
  [CC BY-NC-SA 4.0](LICENSE-CC-BY-NC-SA-4.0).
- **Commercial / corporate / governmental**: everything under the
  [SNTIQ-CM License v1.0](LICENSE-SNTIQ-CM) — free written permission
  with a non-harm covenant, attribution and case-by-case reciprocity
  (contribution or human-rights / open-source donation).

## Repository layout

| Path | Purpose |
|---|---|
| `data/atlas.db` | the built SQLite database (single file, FTS5 search) |
| `pipeline/` | fetchers, validation gate, build, overrides, monitor |
| `api/` | FastAPI resolution service (matter-aware) |
| `tools/browser.py` | zero-dependency hierarchical data browser |
| `tools/export_hf.py` | Hugging Face dataset export (JSONL per table) |
| `docs/` | architecture, source registry, design decisions |
| `examples/` | runnable SQL / Python / HTTP examples |
| `db/schema.sql` | the schema, heavily annotated |

## Citation

```bibtex
@misc{amtsgraph2026,
  title   = {Amtsgraph: an open validated graph of German public
             authorities, court competences and organisational structure},
  author  = {Glushkov, David and {SNTIQ}},
  year    = {2026},
  url     = {https://github.com/SNTIQ-Team/amtsgraph}
}
```

Built by [SNTIQ](https://sntiq.com/) — infrastructure for navigating
large bureaucratic systems. 50+ supported proceedings, documented
administrative-practice changes in German municipalities.
