#!/usr/bin/env python3
"""Export data/atlas.db as a Hugging Face dataset (JSONL per table).

Produces data/export/ with one JSONL file per logical table plus the
dataset card (README.md, HF YAML header included). JSONL keeps the export
dependency-free; HF's dataset viewer ingests it natively.

    python3 tools/export_hf.py [--out data/export]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "atlas.db"

TABLES: dict[str, str] = {
    # file stem -> SELECT
    "authorities": """
        SELECT a.id, a.kind, a.name, a.legal_form, a.street, a.plz, a.city,
               a.postal_address, a.phone, a.fax, a.email, a.web, a.hours,
               a.erv_note, a.lat, a.lon, a.source, a.source_url,
               a.fetched_at, a.valid_to,
               (SELECT json_group_object(scheme, value)
                FROM authority_external_id e
                WHERE e.authority_id = a.id) AS external_ids
        FROM authority a""",
    "court_chains": """
        SELECT plz, ortk, matter, role, position, authority_id, note
        FROM court_chain""",
    "competences": """
        SELECT authority_id, kind, level, area, rank FROM competence""",
    "edges": """
        SELECT from_authority, to_authority, relation, matter, note
        FROM authority_edge""",
    "places_courts": """
        SELECT plz, ortk, ort, gs_key, gemeinde_ags FROM jz_place""",
    "municipalities": """
        SELECT g.ags, g.ars, g.name, g.name_simple, g.kind, k.name AS kreis,
               k.kind AS kreis_kind, k.regierungsbezirk, l.name AS land,
               (SELECT json_group_array(plz) FROM gemeinde_plz p
                WHERE p.ags = g.ags) AS plz
        FROM gemeinde g
        JOIN kreis k ON k.ags = g.kreis_ags
        JOIN land l ON l.code = k.land_code""",
    "matters": "SELECT code, label_de, grp, core FROM matter",
    "caveats": """
        SELECT scope_level, scope_key, matter, severity, text_de, source
        FROM caveat""",
}

JSON_COLUMNS = {"external_ids", "plz"}


def export(out: Path) -> dict[str, int]:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for stem, sql in TABLES.items():
        n = 0
        with open(out / f"{stem}.jsonl", "w", encoding="utf-8") as f:
            for row in db.execute(sql):
                rec = dict(row)
                for col in JSON_COLUMNS & rec.keys():
                    v = rec[col]
                    # decode only actual JSON aggregates — plain scalar
                    # columns may share a name (authority.plz is a string)
                    if isinstance(v, str) and v[:1] in "[{":
                        rec[col] = json.loads(v)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        counts[stem] = n
        print(f"  {stem}.jsonl: {n} rows")
    return counts


CARD = """---
license: other
license_name: sntiq-dual-1.0
license_link: https://github.com/SNTIQ-Team/amtsgraph/blob/main/LICENSE
language:
  - de
pretty_name: "Amtsgraph — German public-authority competence graph"
tags:
  - germany
  - legal
  - government
  - courts
  - public-administration
  - graph
size_categories:
  - 100K<n<1M
configs:
{configs}
---

# Amtsgraph

**The open, validated graph of German public authorities — who is
competent, where, and for what.**

Built {today} from official registers (Orts- und Gerichtsverzeichnis,
PVOG/FITKO, Bundesagentur für Arbeit, Destatis, BayernPortal, OpenPLZ,
xRepository). Every build passes a validation gate (coverage, collision,
contiguity, benchmark checks) before release; every record carries
provenance (source, URL, fetch timestamp).

## Files

| File | Rows | Content |
|---|---|---|
{rows_table}

## Resolution logic

1. **Courts**: look up `places_courts` by postal code (+ locality when one
   code spans several court districts), then `court_chains` by
   `(plz, ortk, matter)` — rows ordered by `position` are the full
   instance chain (role `court`) plus prosecution offices (role
   `prosecution`). Join `authorities` for contacts and filing addresses.
2. **Agencies**: map the user's municipality to its `ags` via
   `municipalities`, then filter `competences` by `kind` and area
   (`gemeinde` = 8-digit AGS, `kreis` = 5-digit prefix, `land` = 2-digit).
   `rank 0` = the office to apply at; `rank 1` = supervisory body.
3. **Always join `caveats`** for the scope you resolved — they flag
   genuine ambiguities and register gaps. For legal use, an honest
   warning beats a confident wrong answer.
4. The organisational web (Bavaria): traverse `edges` with
   `relation='parent'` for department hierarchies, `'supervision'` for
   oversight, `'appeal'` for derived court appeal routes.

## Null semantics

Empty fields are deliberate, not dirty data:

- `legal_form` is only defined for Jobcenter (`gE` = joint federal-municipal
  institution, `zkT` = municipal provider); for every other kind it is null.
- For courts, the full visiting address (street + postal code + city) lives
  in `street` as published by the justice register, and the separate
  postal-box address in `postal_address`; the `plz`/`city` columns are
  filled where sources provide them as discrete fields (agencies), null
  otherwise.
- `valid_to` is null for active records; a timestamp marks retired ones
  (kept for traceability — e.g. offices replaced by a more precise
  department).
- `matter` on edges is null for non-court relations (parent, supervision).
- `external_ids` may be empty for organisational units that have no
  official register identity of their own.

## Important notes

- General information, **not legal advice**; statutory exceptions to
  competence rules exist that no dataset can capture.
- Department-level depth covers Bavaria's 96 Kreisverwaltungsbehörden
  (pilot); other Länder resolve at authority level.
- Underlying records originate from official public registers; their
  respective terms govern redistribution (see the source registry in the
  GitHub repository). Everything is under the SNTIQ Dual License v1.0: fully permissive for non-commercial / human-rights / research use (attribution required); commercial, corporate and governmental use requires free written permission with a non-harm commitment (see LICENSE in the GitHub repository).

## Citation

```bibtex
@misc{{amtsgraph2026,
  title   = {{Amtsgraph: an open validated graph of German public
             authorities, court competences and organisational structure}},
  author  = {{Glushkov, David and {{SNTIQ}}}},
  year    = {{2026}},
  url     = {{https://github.com/SNTIQ-Team/amtsgraph}}
}}
```

Source code, pipeline and full documentation:
**https://github.com/SNTIQ-Team/amtsgraph**
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "export"))
    args = ap.parse_args()
    out = Path(args.out)

    counts = export(out)

    desc = {
        "authorities": "all authorities with contacts, provenance, external IDs",
        "court_chains": "instance chains per (plz, ortk, matter)",
        "competences": "kind × area assignments (rank 0 = apply here)",
        "edges": "parent / supervision / appeal / successor relations",
        "places_courts": "official court-resolution place register",
        "municipalities": "AGS/ARS spine with postal codes (M:N)",
        "matters": "legal-matter taxonomy (14 core + special)",
        "caveats": "warnings that must be surfaced with answers",
    }
    rows_table = "\n".join(
        f"| `{stem}.jsonl` | {counts[stem]:,} | {desc[stem]} |"
        for stem in TABLES)
    configs = "\n".join(
        f"  - config_name: {stem}\n    data_files: {stem}.jsonl"
        for stem in TABLES)
    (out / "README.md").write_text(
        CARD.format(today=date.today().isoformat(),
                    rows_table=rows_table, configs=configs),
        encoding="utf-8")
    print(f"dataset card -> {out / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
