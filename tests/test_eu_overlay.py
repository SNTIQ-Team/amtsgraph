from __future__ import annotations

import json
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import build_db  # noqa: E402
import validate  # noqa: E402


class EUOverlayTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript((ROOT / "db" / "schema.sql").read_text())
        self.verified = build_db.load_eu_overlay(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_core_entities_and_relation_vocabulary(self):
        self.assertEqual("2026-07-15", self.verified)
        self.assertEqual(
            [("eu_body", 2), ("eu_court", 2), ("eu_institution", 7)],
            self.db.execute(
                """SELECT kind,COUNT(*) FROM authority
                   WHERE source='eu_curated' GROUP BY kind ORDER BY kind"""
            ).fetchall(),
        )
        relations = dict(self.db.execute(
            """SELECT relation,COUNT(*) FROM authority_edge
               WHERE source='eu_curated' GROUP BY relation"""))
        self.assertEqual(31, sum(relations.values()))
        self.assertEqual(1, relations["co_legislation"])
        self.assertEqual(1, relations["financial_audit"])
        self.assertEqual(8, relations["maladministration_review"])
        self.assertEqual(8, relations["sectoral_oversight"])
        self.assertNotIn("supervision", relations)
        self.assertNotIn("cooperation", relations)

    def test_all_eu_entities_publish_real_contact_cards(self):
        rows = self.db.execute(
            """SELECT e.value,a.street,a.plz,a.city,a.postal_address,
                      a.phone,a.fax,a.email,a.source_url
               FROM authority a
               JOIN authority_external_id e ON e.authority_id=a.id
               WHERE a.source='eu_curated' AND e.scheme='eu_official'"""
        ).fetchall()
        self.assertEqual(11, len(rows))
        for ext, street, plz, city, postal, phone, fax, email, source in rows:
            with self.subTest(external_id=ext):
                self.assertTrue(city)
                self.assertTrue(postal)
                self.assertTrue(phone)
                self.assertTrue(source.startswith("https://"))
                self.assertIn("europa.eu", source)
        by_id = {row[0]: row for row in rows}
        self.assertEqual("epbrussels@europarl.europa.eu", by_id["EP"][7])
        self.assertEqual("+32 2 281 69 34", by_id["EUCO"][6])
        self.assertEqual("+32 2 281 69 34", by_id["CONSIL"][6])
        self.assertEqual("ECJ.Registry@curia.europa.eu", by_id["CJEU-CJ"][7])
        self.assertEqual("GC.Registry@curia.europa.eu", by_id["CJEU-GC"][7])
        self.assertEqual("edps@edps.europa.eu", by_id["EDPS"][7])
        # These official contact pages intentionally offer a form instead of
        # a general-purpose mailbox; never synthesize an e-mail address.
        for ext in ("EUCO", "CONSIL", "COM", "CJEU", "ECB", "ECA", "EO"):
            self.assertIsNone(by_id[ext][7])
        # CURIA explicitly says fax is no longer accepted for either court.
        self.assertIsNone(by_id["CJEU-CJ"][6])
        self.assertIsNone(by_id["CJEU-GC"][6])

    def test_no_eu_to_german_edge_and_all_edges_have_evidence(self):
        self.db.execute(
            """INSERT INTO authority
               (kind,name,name_norm,source,fetched_at)
               VALUES ('ministerium','Testministerium','testministerium',
                       'override','2026-07-14')""")
        cross = self.db.execute(
            """SELECT COUNT(*) FROM authority_edge e
               JOIN authority a ON a.id=e.from_authority
               JOIN authority b ON b.id=e.to_authority
               WHERE (a.source='eu_curated' OR b.source='eu_curated')
                 AND (a.source<>'eu_curated' OR b.source<>'eu_curated')"""
        ).fetchone()[0]
        missing = self.db.execute(
            """SELECT COUNT(*) FROM authority_edge
               WHERE source='eu_curated'
                 AND (note IS NULL OR source_url NOT LIKE
                      'https://%europa.eu/%')"""
        ).fetchone()[0]
        self.assertEqual(0, cross)
        self.assertEqual(0, missing)

    def test_sectoral_edges_exclude_german_authority_jurisdiction(self):
        bad = self.db.execute(
            """SELECT COUNT(*) FROM authority_edge
               WHERE relation='sectoral_oversight'
                 AND lower(note) NOT LIKE
                     '%keine zuständigkeit für deutsche behörden%'"""
        ).fetchone()[0]
        self.assertEqual(0, bad)

    def test_validation_gate_accepts_overlay(self):
        self.assertEqual([], validate.check(self.db))

    def test_validation_gate_rejects_self_loop(self):
        authority_id = self.db.execute(
            "SELECT id FROM authority ORDER BY id LIMIT 1"
        ).fetchone()[0]
        self.db.execute(
            """INSERT INTO authority_edge
               (from_authority,to_authority,relation,matter,note,delta,
                trust,source,source_url)
               VALUES (?,?,'parent',NULL,'invalid',0.0,1.0,
                       'override',NULL)""",
            (authority_id, authority_id),
        )
        errors = validate.check(self.db)
        self.assertTrue(
            any(error.startswith("graph: 1 self-loop authority edge")
                for error in errors),
            errors,
        )

    def test_builder_drops_self_loop_before_validation(self):
        authority_id = self.db.execute(
            "SELECT id FROM authority ORDER BY id LIMIT 1"
        ).fetchone()[0]
        self.db.execute(
            """INSERT INTO authority_edge
               (from_authority,to_authority,relation,matter,note,delta,
                trust,source,source_url)
               VALUES (?,?,'parent',NULL,'merge artefact',0.0,1.0,
                       'override',NULL)""",
            (authority_id, authority_id),
        )
        self.assertEqual(1, build_db.drop_self_loops(self.db))
        self.assertEqual([], validate.check(self.db))

    @unittest.skipUnless(importlib.util.find_spec("fastapi"),
                         "FastAPI optional dependency not installed")
    def test_graph_api_keeps_uniform_edges_and_separate_eu_metadata(self):
        # Exercise the production serialization contract without TestClient.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "atlas.db"
            disk = sqlite3.connect(path)
            self.db.backup(disk)
            disk.execute(
                """INSERT INTO authority
                   (kind,name,name_norm,source,fetched_at)
                   VALUES ('ministerium','A','a','override','2026-07-14')""")
            a = disk.execute("SELECT last_insert_rowid()").fetchone()[0]
            disk.execute(
                """INSERT INTO authority
                   (kind,name,name_norm,source,fetched_at)
                   VALUES ('ministerium','B','b','override','2026-07-14')""")
            b = disk.execute("SELECT last_insert_rowid()").fetchone()[0]
            disk.execute(
                """INSERT INTO authority_edge
                   (from_authority,to_authority,relation,matter,note,delta,
                    trust,source,source_url)
                   VALUES (?,?,'parent',NULL,'test',0.45,0.8,
                           'override',NULL)""", (a, b))
            # A malformed self-parent must not escape the API even if a stale
            # database predates the validation gate.
            disk.execute(
                """INSERT INTO authority_edge
                   (from_authority,to_authority,relation,matter,note,delta,
                    trust,source,source_url)
                   VALUES (?,?,'parent',NULL,'invalid',0.0,1.0,
                           'override',NULL)""", (a, a))
            disk.commit()
            disk.close()

            from api import main as api_main
            api_main.DB_PATH = path
            api_main._GRAPH_CACHE = None
            api_main._GRAPH_JSON = None
            original_db = api_main.db
            build_calls = 0

            def counted_db():
                nonlocal build_calls
                build_calls += 1
                return original_db()

            api_main.db = counted_db
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    bodies = list(pool.map(lambda _: api_main.graph().body,
                                           range(16)))
            finally:
                api_main.db = original_db
            self.assertEqual(1, build_calls)
            self.assertTrue(all(body == bodies[0] for body in bodies))
            payload = json.loads(bodies[0])
            self.assertFalse(
                any(edge[0] == edge[1] for edge in payload["edges"]),
                payload["edges"],
            )
            german = next(e for e in payload["edges"] if e[2] == "parent")
            eu = next(e for e in payload["edges"]
                      if e[2] == "sectoral_oversight")
            self.assertEqual(3, len(german))
            self.assertEqual(3, len(eu))
            meta = next(e for e in payload["edge_meta"]
                        if e["relation"] == "sectoral_oversight")
            self.assertEqual("eu_curated", meta["source"])
            self.assertTrue(meta["note"])
            self.assertTrue(meta["source_url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
