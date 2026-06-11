"""Validation gate: a freshly built DB must pass before replacing the live one.

For legal use a silent wrong answer is fatal (wrong court = missed deadline),
so every failure mode observed in v1 is a hard check here. build_db.py runs
this automatically; a failing build never touches data/atlas.db.
"""
from __future__ import annotations

import sqlite3
import sys

# matters that every place must resolve for (when justiz snapshot is loaded)
CORE_MATTERS = ["zivil", "familie", "mahn", "insolv", "insolvver",
                "sozial", "arbeit", "verwaltung"]

# non-court kinds where exactly one office must be competent per Gemeinde
EXCLUSIVE_KINDS = ["auslaenderbehoerde", "jobcenter"]


def q(db, sql, *args):
    return db.execute(sql, args).fetchall()


def has_rows(db, table) -> bool:
    return bool(q(db, f"SELECT 1 FROM {table} LIMIT 1"))


def check(db: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    justiz_loaded = has_rows(db, "jz_place")
    pvog_loaded = has_rows(db, "competence")

    # ---- GEO spine ------------------------------------------------------
    # uninhabited gemeindefreie Gebiete (forests, lakes, the German-
    # Luxembourgish condominium) legitimately have no PLZ
    for (n,) in q(db, """
        SELECT COUNT(*) FROM gemeinde g
        WHERE NOT EXISTS (SELECT 1 FROM gemeinde_plz p WHERE p.ags = g.ags)
          AND COALESCE(g.kind, '') NOT LIKE '%emeindefreies%'"""):
        if n:
            errors.append(f"geo: {n} inhabited Gemeinden without any PLZ")

    # ---- court chains ---------------------------------------------------
    if justiz_loaded:
        # every place x core matter resolves to a chain with a 1st instance.
        # Up to 1% of places may legitimately lack an answer in the official
        # register (covered by explicit caveats at build time); more than
        # that means the harvest is broken.
        n_places = q(db, "SELECT COUNT(*) FROM jz_place")[0][0]
        for matter in CORE_MATTERS:
            uncovered = q(db, """
                SELECT COUNT(*) FROM jz_place jp WHERE NOT EXISTS (
                  SELECT 1 FROM court_chain cc
                  WHERE cc.plz = jp.plz AND cc.ortk = jp.ortk
                    AND cc.matter = ?
                    AND cc.role = 'court' AND cc.position = 1)""", matter)[0][0]
            without_caveat = q(db, """
                SELECT COUNT(*) FROM jz_place jp WHERE NOT EXISTS (
                  SELECT 1 FROM court_chain cc
                  WHERE cc.plz = jp.plz AND cc.ortk = jp.ortk
                    AND cc.matter = ? AND cc.role='court' AND cc.position=1)
                AND NOT EXISTS (
                  SELECT 1 FROM caveat cv
                  WHERE cv.scope_level='jz_place'
                    AND cv.scope_key = jp.plz || '|' || jp.ortk
                    AND cv.matter = ?)""", matter, matter)[0][0]
            if without_caveat:
                errors.append(f"courts[{matter}]: {without_caveat} uncovered "
                              f"places WITHOUT caveat (gate breach)")
            if n_places and uncovered / n_places > 0.01:
                errors.append(f"courts[{matter}]: {uncovered}/{n_places} places "
                              f"uncovered (> 1% — harvest broken)")

        # chain positions are contiguous (no gap means parser dropped a card)
        rows = q(db, """
            SELECT plz, ortk, matter, role, COUNT(*), MAX(position)
            FROM court_chain GROUP BY plz, ortk, matter, role
            HAVING COUNT(*) <> MAX(position)""")
        if rows:
            errors.append(f"courts: {len(rows)} chains with position gaps, "
                          f"e.g. {rows[:3]}")

        # (homonym courts — two Amtsgerichte Fürth in Hessen and Bayern —
        # legitimately share a name with different XJustiz-IDs; identity is
        # (xjustiz_id, name), checked at build time, so no name-based check)

        # derived appeal edges must reproduce the chains exactly
        rows = q(db, """
            SELECT cc1.plz, cc1.ortk, cc1.matter FROM court_chain cc1
            JOIN court_chain cc2 ON cc2.plz = cc1.plz AND cc2.ortk = cc1.ortk
              AND cc2.matter = cc1.matter AND cc2.role = 'court'
              AND cc2.position = cc1.position + 1
            WHERE cc1.role = 'court' AND NOT EXISTS (
              SELECT 1 FROM authority_edge e
              WHERE e.from_authority = cc1.authority_id
                AND e.to_authority = cc2.authority_id
                AND e.relation = 'appeal' AND e.matter = cc1.matter)
            LIMIT 5""")
        if rows:
            errors.append(f"courts: appeal edges missing for chains {rows}")

        # contact sanity: a first-instance court without any address is
        # unusable for filings
        rows = q(db, """
            SELECT DISTINCT a.name FROM court_chain cc
            JOIN authority a ON a.id = cc.authority_id
            WHERE cc.position = 1 AND cc.role = 'court'
              AND a.street IS NULL AND a.postal_address IS NULL LIMIT 5""")
        if rows:
            errors.append(f"courts: first-instance courts without address: "
                          f"{[r[0] for r in rows]}")

    # ---- non-court competences -----------------------------------------
    if pvog_loaded:
        for kind in EXCLUSIVE_KINDS:
            rows = q(db, """
                SELECT c.area, COUNT(DISTINCT c.authority_id) n
                FROM competence c
                WHERE c.kind = ? AND c.level = 'gemeinde'
                GROUP BY c.area HAVING n > 1""", kind)
            if rows:
                errors.append(f"[{kind}] {len(rows)} Gemeinden with >1 "
                              f"authority, e.g. {rows[:3]}")

    # ---- orphans ---------------------------------------------------------
    # structural units of the organisational web (Abteilungen etc.) carry
    # no competence but are linked via parent edges — not orphans
    orphan_filter = """
        a.valid_to IS NULL
          AND NOT EXISTS (SELECT 1 FROM competence c WHERE c.authority_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM court_chain cc WHERE cc.authority_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM authority_edge e
                          WHERE e.from_authority = a.id
                             OR e.to_authority = a.id)"""
    rows = q(db, f"SELECT a.id, a.name FROM authority a WHERE {orphan_filter} "
                 f"LIMIT 5")
    if rows:
        n = q(db, f"SELECT COUNT(*) FROM authority a WHERE {orphan_filter}")[0][0]
        errors.append(f"{n} active authorities serve no area, "
                      f"e.g. {[r[1] for r in rows]}")

    return errors


def main(path: str = "data/atlas.db") -> int:
    db = sqlite3.connect(path)
    errors = check(db)
    if errors:
        print(f"VALIDATION FAILED ({len(errors)} problems):")
        for e in errors:
            print("  ✗", e)
        return 1
    print("validation OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
