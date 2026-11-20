"""回收边同源：核心账务测试（stdlib unittest，SQLite 本地夹具）。

运行：
    cd backend
    DATA_DIR=/tmp/lb-test FIXTURES_DIR=../fixtures python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="lb-test-")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault(
    "FIXTURES_DIR", str(Path(__file__).resolve().parents[2] / "fixtures")
)

from app import db, seed  # noqa: E402
from app.db import connect  # noqa: E402
from app.modules import reclaim_proof_edge as reclaim  # noqa: E402
from app.services import reclaim_fixtures  # noqa: E402

AS_OF = "2026-11-20T06:00:00Z"


class ReclaimEdgeTest(unittest.TestCase):
    def setUp(self):
        # 每笔测试独立 SQLite 文件：回收边不可删除，不能用 DELETE 做隔离。
        self.tmpdir = Path(tempfile.mkdtemp(prefix="lb-case-", dir=_TMP))
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.tmpdir / "lockerbay.db"
        seed.init_db()
        self.conn = connect()
        self.occ_id = self._open_occupancy("CAB-A-03", "TN-T1", "2026-11-10T08:00:00Z")

    def tearDown(self):
        self.conn.close()
        db.DB_PATH = self._orig_db_path

    # ------------------------------------------------------------ helpers

    def _open_occupancy(self, cell_id: str, tracking_no: str, opened_at: str) -> str:
        oid = f"OCC-{cell_id}-{tracking_no}"
        self.conn.execute(
            """
            INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status,
                                    last_event_id, close_reason)
            VALUES (?,?,?,?,NULL,'occupied',NULL,NULL)
            """,
            (oid, cell_id, tracking_no, opened_at),
        )
        self.conn.execute("UPDATE cells SET status='occupied' WHERE id=?", (cell_id,))
        self.conn.execute(
            """
            INSERT INTO occupancy_proofs(occupancy_id, cell_id, tracking_no, opened_at,
                                         closed_at, event_ids_json, created_at)
            VALUES (?,?,?,?,NULL,'[]',?)
            """,
            (oid, cell_id, tracking_no, opened_at, reclaim.utc_now()),
        )
        self.conn.commit()
        return oid

    # ------------------------------------------------------------ 正常回收

    def test_reclaim_writes_edge_and_closes_same_transaction(self):
        result = reclaim.reclaim_occupancy(self.conn, self.occ_id, as_of=AS_OF)
        edge = self.conn.execute(
            "SELECT * FROM reclaim_edges WHERE occupancy_id=?", (self.occ_id,)
        ).fetchone()
        occ = self.conn.execute(
            "SELECT * FROM occupancies WHERE id=?", (self.occ_id,)
        ).fetchone()
        proof = self.conn.execute(
            "SELECT * FROM occupancy_proofs WHERE occupancy_id=?", (self.occ_id,)
        ).fetchone()

        self.assertIsNotNone(edge)
        self.assertEqual(edge["occupancy_id"], self.occ_id)
        self.assertEqual(edge["reason"], "overdue")
        self.assertEqual(edge["status_before"], "occupied")
        self.assertEqual(edge["status_after"], "released")
        self.assertEqual(edge["hour_bucket"], "2026-11-20T06:00:00Z")
        self.assertEqual(edge["closed_at"], AS_OF)
        self.assertIn('"occupied_before"', edge["snapshot_json"])
        self.assertEqual(edge["proof_id"], proof["id"])

        self.assertEqual(occ["status"], "released")
        self.assertEqual(occ["close_reason"], "reclaim")
        self.assertEqual(occ["closed_at"], AS_OF)
        self.assertEqual(proof["closed_at"], AS_OF)
        self.assertEqual(result["edge_id"], edge["id"])

        cell = self.conn.execute(
            "SELECT status FROM cells WHERE id=?", (occ["cell_id"],)
        ).fetchone()
        self.assertEqual(cell["status"], "free")

    def test_verify_and_trace_pass_after_reclaim(self):
        reclaim.reclaim_occupancy(self.conn, self.occ_id, as_of=AS_OF)
        report = reclaim.validate_edges(self.conn)
        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["edge_count"], 1)
        self.assertEqual(report["reclaim_closed"], 1)
        self.assertTrue(report["edges"][0]["ok"])

        trace = reclaim.trace_occupancy(self.conn, self.occ_id)
        self.assertTrue(trace["ok"])
        self.assertEqual(trace["edge"]["id"], f"REDGE-{self.occ_id}")
        self.assertEqual(trace["proof"]["closed_at"], AS_OF)

    def test_scan_reclaims_overdue_and_skips_fresh(self):
        fresh = self._open_occupancy("CAB-A-04", "TN-FRESH", "2026-11-20T05:00:00Z")
        result = reclaim.scan_and_reclaim(self.conn, as_of=AS_OF)
        ids = [r["occupancy_id"] for r in result["reclaimed"]]
        self.assertIn(self.occ_id, ids)
        self.assertNotIn(fresh, ids)
        self.assertEqual(result["skipped"], [])

    # ------------------------------------------------------------ 失败闭

    def test_not_overdue_rejected_without_writes(self):
        fresh = self._open_occupancy("CAB-A-04", "TN-FRESH", "2026-11-20T05:00:00Z")
        with self.assertRaises(reclaim.ReclaimError) as ctx:
            reclaim.reclaim_occupancy(self.conn, fresh, as_of=AS_OF)
        self.assertEqual(ctx.exception.code, "not_overdue")
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM reclaim_edges WHERE occupancy_id=?", (fresh,)
            ).fetchone()
        )
        occ = self.conn.execute("SELECT status FROM occupancies WHERE id=?", (fresh,)).fetchone()
        self.assertEqual(occ["status"], "occupied")

    def test_close_without_edge_aborts_transaction(self):
        """应用层漏写边时，库级触发器也必须拒绝关账，不留半截。"""
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute(
                """
                UPDATE occupancies SET status='released', closed_at=?, close_reason='reclaim'
                 WHERE id=?
                """,
                (AS_OF, self.occ_id),
            )
        self.conn.rollback()
        self.assertIn("回收边", str(ctx.exception))
        occ = self.conn.execute(
            "SELECT * FROM occupancies WHERE id=?", (self.occ_id,)
        ).fetchone()
        self.assertEqual(occ["status"], "occupied")
        self.assertIsNone(occ["closed_at"])

    def test_close_without_reason_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE occupancies SET status='released', closed_at=? WHERE id=?",
                (AS_OF, self.occ_id),
            )
        self.conn.rollback()

    def test_edge_on_closed_occupancy_rejected(self):
        """边只能写在仍开放的占用上；关闭后补写边会被触发器拒绝。"""
        reclaim.reclaim_occupancy(self.conn, self.occ_id, as_of=AS_OF)
        self.conn.execute("SAVEPOINT probe")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute(
                """
                INSERT INTO reclaim_edges(
                  id, occupancy_id, proof_id, cell_id, tracking_no, opened_at, reason,
                  status_before, status_after, closed_at, hour_bucket, snapshot_json,
                  overdue_hours, deadline_at, decided_at, decision, created_at
                )
                SELECT 'DUP', occupancy_id, proof_id, cell_id, tracking_no, opened_at, reason,
                       status_before, status_after, closed_at, hour_bucket, snapshot_json,
                       overdue_hours, deadline_at, decided_at, decision, created_at
                  FROM reclaim_edges WHERE occupancy_id=?
                """,
                (self.occ_id,),
            )
        self.conn.execute("ROLLBACK TO probe")
        self.conn.execute("RELEASE probe")
        self.assertIn("仍开放", str(ctx.exception))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM reclaim_edges").fetchone()["c"], 1
        )

    def test_missing_edge_fixture_fails_verification(self):
        reclaim_fixtures.load("missing-edge", self.conn)
        report = reclaim.validate_edges(self.conn)
        self.assertFalse(report["ok"])
        self.assertGreaterEqual(report["missing_edge_count"], 1)
        codes = {f["code"] for f in report["failures"]}
        self.assertIn("missing_edge", codes)

        trace = reclaim.trace_occupancy(self.conn, "OCC-MISSING-EDGE-1")
        self.assertFalse(trace["ok"])
        self.assertIsNone(trace["edge"])
        self.assertEqual(trace["issues"][0]["code"], "missing_edge")

    def test_pickup_close_has_no_reclaim_edge_and_does_not_fail(self):
        self.conn.execute(
            """
            UPDATE occupancies SET status='released', closed_at=?, close_reason='pickup'
             WHERE id=?
            """,
            (AS_OF, self.occ_id),
        )
        self.conn.commit()
        report = reclaim.validate_edges(self.conn)
        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["edge_count"], 0)
        trace = reclaim.trace_occupancy(self.conn, self.occ_id)
        self.assertTrue(trace["ok"])
        self.assertFalse(trace["reclaim"])

    def test_closed_without_reason_fails_audit(self):
        """历史遗留：已关闭但无关闭原因，等于无边的关账，校验必须失败。"""
        # 触发器守 UPDATE 关账路径；历史遗留绕过应用直插，只能用 INSERT 形态模拟。
        self.conn.execute(
            """
            INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status,
                                    last_event_id, close_reason)
            VALUES ('OCC-LEGACY-NOREASON', 'CAB-A-02', 'TN-LEGACY-2',
                    '2026-11-01T02:00:00Z', ?, 'released', NULL, NULL)
            """,
            (AS_OF,),
        )
        self.conn.commit()
        report = reclaim.validate_edges(self.conn)
        self.assertFalse(report["ok"])
        self.assertIn(
            "close_reason_missing", {f["code"] for f in report["failures"]}
        )
        trace = reclaim.trace_occupancy(self.conn, "OCC-LEGACY-NOREASON")
        self.assertFalse(trace["ok"])
        self.assertEqual(trace["issues"][0]["code"], "close_reason_missing")

    def test_edges_are_immutable_and_not_deletable(self):
        reclaim.reclaim_occupancy(self.conn, self.occ_id, as_of=AS_OF)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE reclaim_edges SET reason='tampered'")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM reclaim_edges")
        self.conn.rollback()
        self.assertEqual(
            self.conn.execute("SELECT reason FROM reclaim_edges").fetchone()["reason"],
            "overdue",
        )

    def test_idempotent_second_reclaim_rejected(self):
        reclaim.reclaim_occupancy(self.conn, self.occ_id, as_of=AS_OF)
        with self.assertRaises(reclaim.ReclaimError) as ctx:
            reclaim.reclaim_occupancy(self.conn, self.occ_id, as_of=AS_OF)
        self.assertEqual(ctx.exception.code, "occupancy_not_open")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM reclaim_edges").fetchone()["c"], 1
        )


if __name__ == "__main__":
    unittest.main()
