# Architecture (v2.1 — correctness-first)

## Why correctness drives the design

This data feeds legal action. A wrong court means a filing lands in the wrong
place, a deadline (Frist) is missed, a case is lost. Three findings shaped v2.1:

1. **Court competence is per (PLZ, Ort), not per PLZ.** Official register:
   PLZ 25712 contains 9 villages belonging to *different* Amtsgericht
   districts. Any PLZ-keyed model answers wrongly for such places.
2. **Competence depends on the legal matter.** Mahnverfahren go to one
   central Mahngericht per Land (Berlin 10115: zivil → AG Mitte, mahn →
   AG Wedding/Zentrales Mahngericht). Family appeals skip the Landgericht
   (AG → OLG). Insolvency and register matters are concentrated at selected
   courts. A model with one "responsible court" per place is structurally
   wrong.
3. **v1 failed silently** (name-string joins, hand-edited CSVs, one PLZ per
   city). Silent degradation is the enemy; every failure mode is now either
   prevented by schema or caught by a validation gate.

## Data model: three planes + matter dimension

```
GEO plane                AUTHORITY plane             COURT TRUTH
─────────                ───────────────             ───────────
land → kreis → gemeinde  authority (typed, with      court_chain
        ▲    ▲             provenance columns)        (gs_key × matter ×
 gemeinde_plz (M:N)        ▲         ▲                 position → authority)
        │                  │         │                exact harvested
       plz            competence  authority_edge      instance chains
        │             (non-court   (derived appeal/
   jz_place            kind × area) supervision graph,
   (plz × ortk × gs_key)            cross-checked)
   official court
   resolution register   matter (taxonomy of legal matters = `ang` codes)
                         caveat (ambiguity warnings the API must surface)
```

Key decisions:

- **Courts: serve the harvested chain verbatim.** The official
  Orts- und Gerichtsverzeichnis returns the full instance chain
  (AG → LG → OLG (+ StA) with XJustiz-IDs) per (place, matter). We store it
  exactly as harvested (`court_chain`) and answer from it. The
  `authority_edge` graph is *derived* from chains and cross-checked by
  validation — graph walking is never the primary answer, so a
  reconstruction bug cannot reach a client.
- **gs_key grouping with spot-verification.** Places sharing the register's
  structural court key share chains; the harvester re-fetches a random
  sample of grouped places directly each run and aborts on any mismatch.
  Assumptions are cheap; unverified assumptions are not.
- **Caveats are first-class.** When the source says "cannot be determined
  from PLZ/Ort alone" (street-level splits in big cities), that becomes a
  `caveat` row, and the API must attach it to the answer. An honest "verify
  by street" beats a confident wrong court.
- **Provenance on every authority row** (source, source_url, fetched_at,
  source_updated_at) — any answer served to a client is traceable to an
  official source and a date.
- **Non-courts** (Ausländerbehörde, Jobcenter, Sozialamt …) resolve by
  `competence` (kind × Gemeinde-AGS), harvested from the federal PVOG
  Zuständigkeitsfinder; granularity can drop to PLZ where needed.

## Resolution flow

```
user types city ──► GET /places?q=...        (gemeinde + jz_place search,
                                              umlaut-folded; >1 hit → picker
                                              with Land/Kreis context)
user picks kind ──► courts:    (plz, ortk) → gs_key → court_chain[matter]
                    non-court: AGS → competence[kind]
                    + caveats for the scope    ◄── always attached
answer          ──► authority cards + full instance chain + provenance line
                    ("Quelle: Orts- und Gerichtsverzeichnis, Stand 2026-06-10")
```

For court kinds the UI asks for the **matter** (Mahnverfahren? Familiensache?)
— not just "which court", because that question has no answer without it.

## Update automation (no single source of truth exists)

Pipeline per source: `fetch → snapshot (immutable) → build → validate → swap`.

- `pipeline/sources.yaml` registers every source with cadence.
- `build_db.py` writes to `atlas.db.building` and only replaces the live DB
  when `validate.py` passes — a broken refresh can never go live.
- Manual fixes live exclusively in `pipeline/overrides/*.yaml` (with reason,
  source, expiry); they survive rebuilds and show up in diffs.
- Harvesters are resumable (checkpoint files) and polite (rate-limited).
- Diff report between builds = the change log Germany never publishes.

## Stack

SQLite (FTS5) + Python ETL + FastAPI. Scale is small (~11k Gemeinden, ~8k
PLZ, few thousand authorities, ~9k chains); a single reproducible file beats
a server. JSON export per place possible for static/CDN serving.
