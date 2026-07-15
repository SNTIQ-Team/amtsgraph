from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import build_db  # noqa: E402
import validate  # noqa: E402


class LandratsamtClassificationTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript((ROOT / "db" / "schema.sql").read_text())

    def tearDown(self):
        self.db.close()

    def add_authority(self, aid: int, name: str, kind: str = "sonstige",
                      valid_to: str | None = None) -> None:
        self.db.execute(
            """INSERT INTO authority
               (id,kind,name,name_norm,source,fetched_at,valid_to)
               VALUES (?,?,?,?, 'test','2026-07-15',?)""",
            (aid, kind, name, name.lower(), valid_to),
        )

    def add_parent(self, child: int, parent: int) -> None:
        self.db.execute(
            """INSERT INTO authority_edge
               (from_authority,to_authority,relation,matter,note,delta,
                trust,source,source_url)
               VALUES (?,?,'parent',NULL,'test',0.45,1.0,'test',NULL)""",
            (child, parent),
        )

    def test_only_active_exact_unparented_roots_are_promoted(self):
        self.add_authority(1, "Landratsamt Pfaffenhofen a.d.Ilm")
        self.add_authority(2, "Landratsamt IT")
        self.add_authority(3, "Landratsamt Pfaffenhofen a.d.Ilm - IT")
        self.add_authority(4, "Büro Landrat")
        self.add_authority(5, "Landratsamt Alt")
        self.add_authority(6, "Landratsamt Bereits typisiert",
                           "auslaenderbehoerde")
        self.add_authority(7, "Landratsamt Geschlossen",
                           valid_to="2026-07-01")
        self.add_parent(2, 1)
        self.add_parent(3, 1)
        self.add_parent(4, 1)

        self.assertEqual(2, build_db.classify_landratsamt_roots(self.db))
        kinds = dict(self.db.execute("SELECT id,kind FROM authority"))
        self.assertEqual("landratsamt", kinds[1])
        self.assertEqual("landratsamt", kinds[5])
        self.assertEqual("sonstige", kinds[2])
        self.assertEqual("sonstige", kinds[3])
        self.assertEqual("sonstige", kinds[4])
        self.assertEqual("auslaenderbehoerde", kinds[6])
        self.assertEqual("sonstige", kinds[7])
        errors = validate.check(self.db)
        self.assertFalse(
            any("unsafe landratsamt classifications" in error
                or "standalone Landratsamt roots left" in error
                for error in errors),
            errors,
        )

    def test_validator_rejects_department_promoted_as_landratsamt(self):
        self.add_authority(1, "Landratsamt Kreis")
        self.add_authority(2, "Landratsamt IT", "landratsamt")
        self.add_parent(2, 1)

        errors = validate.check(self.db)

        self.assertTrue(
            any("unsafe landratsamt classifications" in error
                for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
