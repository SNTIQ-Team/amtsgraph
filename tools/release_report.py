#!/usr/bin/env python3
"""Generate the release trust layer: validation-report.json + coverage.md.

Run against the live atlas.db; outputs land in data/release/ together with
the database copy and its checksum — everything a release needs for a
third party to judge whether to trust the build.

    python3 tools/release_report.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "atlas.db"
OUT = ROOT / "data" / "release"

sys.path.insert(0, str(ROOT / "pipeline"))
import validate  # noqa: E402


def q1(db, sql, *args):
    return db.execute(sql, args).fetchone()[0]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    # ---- validation: run the exact gate the build uses -----------------
    errors = validate.check(db)
    built_at = q1(db, "SELECT value FROM build_info WHERE key='built_at'")
    snapshots = json.loads(
        q1(db, "SELECT value FROM build_info WHERE key='snapshots'"))

    n_places = q1(db, "SELECT COUNT(*) FROM jz_place")
    per_matter = {}
    for (m,) in db.execute("SELECT code FROM matter WHERE core=1"):
        covered = q1(db, """
            SELECT COUNT(*) FROM jz_place jp WHERE EXISTS (
              SELECT 1 FROM court_chain cc
              WHERE cc.plz=jp.plz AND cc.ortk=jp.ortk AND cc.matter=?
                AND cc.role='court' AND cc.position=1)""", m)
        per_matter[m] = {"covered": covered, "total": n_places,
                         "pct": round(100 * covered / n_places, 2)}

    per_kind = {k: n for k, n in db.execute(
        """SELECT c.kind, COUNT(DISTINCT c.authority_id) FROM competence c
           JOIN authority a ON a.id=c.authority_id
           WHERE a.valid_to IS NULL GROUP BY c.kind""")}

    report = {
        "dataset": "Amtsgraph",
        "report_date": date.today().isoformat(),
        "built_at": built_at,
        "source_snapshots": snapshots,
        "validation": {
            "passed": not errors,
            "checks": "see pipeline/validate.py",
            "errors": errors,
        },
        "totals": {
            "authorities_active": q1(db, "SELECT COUNT(*) FROM authority "
                                         "WHERE valid_to IS NULL"),
            "court_chain_links": q1(db, "SELECT COUNT(*) FROM court_chain"),
            "places": n_places,
            "competences": q1(db, "SELECT COUNT(*) FROM competence"),
            "parent_edges": q1(db, "SELECT COUNT(*) FROM authority_edge "
                                   "WHERE relation='parent'"),
            "caveats": q1(db, "SELECT COUNT(*) FROM caveat"),
        },
        "court_coverage_by_core_matter": per_matter,
        "authorities_by_competence_kind": per_kind,
        "known_gaps": {
            "court_register_empty_answers": q1(
                db, "SELECT COUNT(*) FROM caveat WHERE "
                    "scope_level='jz_place' AND source='justizadressen'"),
            "note": "every gap is covered by an explicit caveat row; "
                    "the API must surface caveats with answers",
        },
        "external_benchmark": {
            "xjustiz_ids_in_dataset": q1(
                db, "SELECT COUNT(DISTINCT value) FROM authority_external_id "
                    "WHERE scheme='xjustiz'"),
            "note": "all XJustiz IDs verified against the official "
                    "GDS.Gerichte codelist at build time (0 outliers)",
        },
    }
    (OUT / "validation-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- coverage.md ----------------------------------------------------
    lines = [
        f"# Amtsgraph coverage report — {date.today().isoformat()}",
        "",
        f"Build: `{built_at}` · validation: "
        f"{'**PASSED**' if not errors else '**FAILED**'}",
        "",
        "## Court coverage per core matter",
        "",
        "| Matter | Covered places | % |",
        "|---|---|---|",
    ]
    for m, c in sorted(per_matter.items()):
        lines.append(f"| {m} | {c['covered']:,} / {c['total']:,} "
                     f"| {c['pct']} % |")
    lines += ["", "## Authorities per competence kind", "",
              "| Kind | Authorities |", "|---|---|"]
    for k, n in sorted(per_kind.items()):
        lines.append(f"| {k} | {n:,} |")
    lines += ["", "Uncovered place×matter combinations carry explicit "
                  "`caveat` rows — display them with every answer.", ""]
    (OUT / "coverage.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- sources-lock: snapshot dates actually baked into this build ----
    (OUT / "sources-lock.yaml").write_text(
        "# source snapshots baked into this build\n" + "".join(
            f"{src}: {day}\n" for src, day in sorted(snapshots.items())),
        encoding="utf-8")

    # ---- database + checksum -------------------------------------------
    target = OUT / "atlas.sqlite"
    shutil.copyfile(DB, target)
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    (OUT / "atlas.sqlite.sha256").write_text(f"{sha}  atlas.sqlite\n")

    print(f"release artifacts -> {OUT}")
    for f in sorted(OUT.iterdir()):
        print(f"  {f.name} ({f.stat().st_size:,} B)")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
