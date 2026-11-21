"""格口释放审计：三联（回收边、释放审计、格口 free）核证测试（stdlib unittest）。

运行：
    cd backend
    DATA_DIR=/tmp/lb-test FIXTURES_DIR=../fixtures python3 -m unittest tests.test_cell_release_audit -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="lb-test-")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault(
    "FIXTURES_DIR", str(Path(__file__).resolve().parents[2] / "fixtures")
)

from app import db, seed  # noqa: E402
from app.db import connect  # noqa: E402
from app.modules import cell_release_audit as audit_mod  # noqa: E402
from app.modules import reclaim_proof_edge as reclaim  # noqa: E402

AS_OF = "2026-11-20T06:00:00Z"
LATER = "2026-11-20T07:30:00Z"


class CellReleaseAuditTest(unittest.TestCase):
    def setUp(self):
        # 每笔测试独立 SQLite 文件：边与审计均不可删除，不能用 DELETE 做隔离。
        self.tmpdir = Path(tempfile.mkdtemp(prefix="lb-rel-", dir=_TMP))
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

    def _reclaim(self, occ_id: str | None = None, as_of: str = AS_OF) -> dict:
        return reclaim.reclaim_occupancy(self.conn, occ_id or self.occ_id, as_of=as_of)

    def _audit_row(self, occ_id: str | None = None) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT * FROM cell_release_audits WHERE occupancy_id=?",
            (occ_id or self.occ_id,),
        ).fetchone()

    def _edge_row(self, occ_id: str | None = None) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT * FROM reclaim_edges WHERE occupancy_id=?",
            (occ_id or self.occ_id,),
        ).fetchone()

    def _codes(self, report: dict) -> set[str]:
        return {f["code"] for f in report["failures"]}

    # ------------------------------------------------------------ 正常三联

    def test_reclaim_writes_release_audit_in_same_transaction(self):
        result = self._reclaim()
        audit = self._audit_row()
        edge = self._edge_row()
        occ = self.conn.execute(
            "SELECT * FROM occupancies WHERE id=?", (self.occ_id,)
        ).fetchone()
        cell = self.conn.execute(
            "SELECT * FROM cells WHERE id=?", (occ["cell_id"],)
        ).fetchone()

        # 四要素：cell_id、occupancy_id、released_at、边键。
        self.assertIsNotNone(audit)
        self.assertEqual(audit["occupancy_id"], self.occ_id)
        self.assertEqual(audit["cell_id"], edge["cell_id"])
        self.assertEqual(audit["edge_id"], edge["id"])
        self.assertEqual(audit["released_at"], edge["closed_at"])
        self.assertEqual(audit["released_at"], AS_OF)
        self.assertEqual(audit["close_reason"], "reclaim")
        self.assertEqual(result["release_audit_id"], audit["id"])

        # 三联之一：占用关账行同源。
        self.assertEqual(occ["status"], "released")
        self.assertEqual(occ["cell_id"], audit["cell_id"])
        self.assertEqual(occ["closed_at"], audit["released_at"])

        # 三联之二：回收边同源。
        self.assertEqual(edge["cell_id"], audit["cell_id"])
        self.assertEqual(edge["closed_at"], audit["released_at"])

        # 三联之三：格口 free 态。
        self.assertEqual(cell["status"], "free")

    def test_verify_three_way_passes_after_reclaim(self):
        self._reclaim()
        report = audit_mod.validate_releases(self.conn)
        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["audit_count"], 1)
        self.assertEqual(report["edge_count"], 1)
        self.assertEqual(report["missing_audit_count"], 0)
        self.assertEqual(report["cell_mismatch_count"], 0)
        self.assertTrue(report["audits"][0]["ok"])

        cell_report = audit_mod.reconcile_cells(self.conn)
        self.assertTrue(cell_report["ok"])
        self.assertEqual(cell_report["cell_mismatch_count"], 0)

        trace = audit_mod.trace_release(self.conn, self.occ_id)
        self.assertTrue(trace["ok"], trace["issues"])
        self.assertEqual(trace["audit"]["edge_id"], trace["edge"]["id"])
        self.assertEqual(trace["cell"]["status"], "free")
        self.assertEqual(trace["open_occupancies_on_cell"], 0)

    def test_list_releases_returns_verified_rows_not_stub(self):
        self._reclaim()
        rows = audit_mod.list_releases(self.conn)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # 基线桩返回 cell_id=None/released_at=None/edge_ok=True 的假数据；
        # 实现后必须给真实四要素与核对结论。
        self.assertIsNotNone(row["cell_id"])
        self.assertIsNotNone(row["released_at"])
        self.assertEqual(row["edge_id"], f"REDGE-{self.occ_id}")
        self.assertTrue(row["ok"])
        self.assertEqual(row["issues"], [])

    def test_re_reused_cell_keeps_its_own_audit_line(self):
        """同格口先回收、再投新占用、再回收：两笔审计各归各的占用，不串。"""
        first = self._reclaim()
        occ2 = self._open_occupancy("CAB-A-03", "TN-T2", "2026-11-12T08:00:00Z")
        second = reclaim.reclaim_occupancy(self.conn, occ2, as_of=LATER)

        rows = self.conn.execute(
            "SELECT * FROM cell_release_audits ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["occupancy_id"] for r in rows}, {self.occ_id, occ2})
        self.assertEqual({r["cell_id"] for r in rows}, {"CAB-A-03"})
        self.assertEqual(rows[0]["edge_id"], first["edge_id"])
        self.assertEqual(rows[1]["edge_id"], second["edge_id"])
        self.assertEqual(rows[0]["released_at"], AS_OF)
        self.assertEqual(rows[1]["released_at"], LATER)

        report = audit_mod.validate_releases(self.conn)
        self.assertTrue(report["ok"], report["failures"])

    # ------------------------------------------------------------ 失败闭：缺一回滚

    def test_missing_audit_aborts_close_at_db_trigger(self):
        """库级兜底：写了边但没有释放审计就关账，必须 ABORT，整笔回滚。"""
        edge = self._edge_row  # noqa: F841（保持读路径可用）
        # 手工模拟应用漏写审计：只插边（占用仍 occupied，触发器允许），再关账。
        self.conn.execute(
            """
            INSERT INTO reclaim_edges(
              id, occupancy_id, proof_id, cell_id, tracking_no, opened_at, reason,
              status_before, status_after, closed_at, hour_bucket, snapshot_json,
              overdue_hours, deadline_at, decided_at, decision, created_at
            )
            SELECT 'REDGE-' || id, id,
                   (SELECT id FROM occupancy_proofs WHERE occupancy_id=occupancies.id),
                   cell_id, tracking_no, opened_at, 'overdue',
                   'occupied', 'released', ?, '2026-11-20T06:00:00Z', '{}',
                   48, opened_at, ?, 'overdue', ?
              FROM occupancies WHERE id=?
            """,
            (AS_OF, AS_OF, reclaim.utc_now(), self.occ_id),
        )
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute(
                "UPDATE occupancies SET status='released', closed_at=?, close_reason='reclaim' "
                "WHERE id=?",
                (AS_OF, self.occ_id),
            )
        self.conn.rollback()
        self.assertIn("释放审计", str(ctx.exception))
        occ = self.conn.execute(
            "SELECT * FROM occupancies WHERE id=?", (self.occ_id,)
        ).fetchone()
        self.assertEqual(occ["status"], "occupied")
        self.assertIsNone(occ["closed_at"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM cell_release_audits").fetchone()["c"], 0
        )

    def test_half_failure_rolls_back_whole_reclaim(self):
        """审计写入被触发器拒绝时，边、占用、格口全部回到回收前。"""
        # 预先把占用偷偷置为 released（绕过路径：直接改 cells 不影响），
        # 让审计 INSERT 因「占用必须仍开放」失败；更直接的做法是破坏边键对撞。
        # 这里用重复边键制造冲突：先正常回收一笔邻格，再让本笔审计撞 UNIQUE(edge_id)。
        neighbour = self._open_occupancy("CAB-A-04", "TN-N1", "2026-11-10T09:00:00Z")
        reclaim.reclaim_occupancy(self.conn, neighbour, as_of=AS_OF)
        neighbour_audit_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM cell_release_audits WHERE occupancy_id=?",
            (neighbour,),
        ).fetchone()["c"]
        self.assertEqual(neighbour_audit_count, 1)

        # 模拟半截失败：在事务内插边后，强行让审计 INSERT 撞 UNIQUE(edge_id) /
        # 同源触发器，事务必须整体回滚，本占用保持 occupied，邻格审计不得被动。
        with self.assertRaises(sqlite3.IntegrityError):
            conn2 = connect()
            try:
                conn2.execute("BEGIN IMMEDIATE")
                # 复用应用的写边函数拿一条真边
                occ = conn2.execute(
                    "SELECT * FROM occupancies WHERE id=?", (self.occ_id,)
                ).fetchone()
                proof = conn2.execute(
                    "SELECT * FROM occupancy_proofs WHERE occupancy_id=? ORDER BY id DESC LIMIT 1",
                    (self.occ_id,),
                ).fetchone()
                reclaim._insert_edge(
                    conn2, occ, proof, as_of=AS_OF,
                    overdue_hours=48, reason="overdue",
                )
                # 故意把 edge_id 改成邻格已用的边键 → 同源/UNIQUE 冲突
                conn2.execute(
                    """
                    INSERT INTO cell_release_audits(
                      occupancy_id, cell_id, edge_id, released_at, close_reason, created_at
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (
                        self.occ_id, occ["cell_id"],
                        f"REDGE-{neighbour}", AS_OF, "reclaim", reclaim.utc_now(),
                    ),
                )
                conn2.commit()
            except Exception:
                conn2.rollback()
                raise
            finally:
                conn2.close()

        # 本笔全部回滚
        self.assertIsNone(self._edge_row())
        self.assertIsNone(self._audit_row())
        occ = self.conn.execute(
            "SELECT status FROM occupancies WHERE id=?", (self.occ_id,)
        ).fetchone()
        self.assertEqual(occ["status"], "occupied")
        # 邻格审计未被串改
        still_one = self.conn.execute(
            "SELECT COUNT(*) AS c FROM cell_release_audits WHERE occupancy_id=?",
            (neighbour,),
        ).fetchone()["c"]
        self.assertEqual(still_one, 1)
        neighbour_audit = self.conn.execute(
            "SELECT * FROM cell_release_audits WHERE occupancy_id=?", (neighbour,)
        ).fetchone()
        self.assertEqual(neighbour_audit["occupancy_id"], neighbour)
        self.assertEqual(neighbour_audit["cell_id"], "CAB-A-04")

    # ------------------------------------------------------------ 人为改歪必失败

    def test_audit_with_wrong_cell_rejected_by_trigger(self):
        """人为把释放审计写到邻格 cell：边键对撞触发器直接拒绝。"""
        self._reclaim()  # 先造一条合法边供邻格场景参考
        fresh = self._open_occupancy("CAB-A-02", "TN-T3", "2026-11-10T10:00:00Z")
        fresh_edge = None
        occ = self.conn.execute(
            "SELECT * FROM occupancies WHERE id=?", (fresh,)
        ).fetchone()
        proof = self.conn.execute(
            "SELECT * FROM occupancy_proofs WHERE occupancy_id=? ORDER BY id DESC LIMIT 1",
            (fresh,),
        ).fetchone()
        fresh_edge = reclaim._insert_edge(
            self.conn, occ, proof, as_of=LATER, overdue_hours=48, reason="overdue"
        )
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute(
                """
                INSERT INTO cell_release_audits(
                  occupancy_id, cell_id, edge_id, released_at, close_reason, created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                # cell_id 故意写邻格 CAB-A-03，与边上的 CAB-A-02 分叉
                (fresh, "CAB-A-03", fresh_edge, LATER, "reclaim", reclaim.utc_now()),
            )
        self.conn.rollback()
        self.assertIn("同源", str(ctx.exception))

    def test_audit_with_wrong_released_at_rejected_by_trigger(self):
        """人为把释放时刻改成与边关闭时刻不一致：触发器拒绝。"""
        occ = self.conn.execute(
            "SELECT * FROM occupancies WHERE id=?", (self.occ_id,)
        ).fetchone()
        proof = self.conn.execute(
            "SELECT * FROM occupancy_proofs WHERE occupancy_id=? ORDER BY id DESC LIMIT 1",
            (self.occ_id,),
        ).fetchone()
        edge_id = reclaim._insert_edge(
            self.conn, occ, proof, as_of=AS_OF, overdue_hours=48, reason="overdue"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO cell_release_audits(
                  occupancy_id, cell_id, edge_id, released_at, close_reason, created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (self.occ_id, occ["cell_id"], edge_id, LATER, "reclaim", reclaim.utc_now()),
            )
        self.conn.rollback()
        self.assertIsNone(self._audit_row())

    def test_audit_immutable_and_not_deletable(self):
        self._reclaim()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE cell_release_audits SET cell_id='CAB-A-04'"
            )
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM cell_release_audits")
        self.conn.rollback()
        row = self._audit_row()
        self.assertEqual(row["cell_id"], "CAB-A-03")

    def test_audit_requires_open_occupancy(self):
        """关闭后补写审计（时序倒插）必须被触发器拒绝。"""
        self._reclaim()
        edge = self._edge_row()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute(
                """
                INSERT INTO cell_release_audits(
                  occupancy_id, cell_id, edge_id, released_at, close_reason, created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                # 换一个新 edge_id 绕过 UNIQUE，但占用已 released
                (self.occ_id, edge["cell_id"], "REDGE-FAKE-LATE",
                 AS_OF, "reclaim", reclaim.utc_now()),
            )
        self.conn.rollback()
        self.assertIn("回收边", str(ctx.exception))

    def test_tampered_closed_cell_is_flagged_as_ghost(self):
        """占用已 released，人为把格口拨回 occupied：核证必须报幽灵占用角标。"""
        self._reclaim()
        self.conn.execute(
            "UPDATE cells SET status='occupied' WHERE id='CAB-A-03'"
        )
        self.conn.commit()

        report = audit_mod.validate_releases(self.conn)
        self.assertFalse(report["ok"])
        self.assertIn("cell_ghost_occupied", self._codes(report))
        self.assertEqual(report["cell_mismatch_count"], 1)

        cell_report = audit_mod.reconcile_cells(self.conn)
        self.assertFalse(cell_report["ok"])
        bad = [c for c in cell_report["cells"] if not c["ok"]]
        self.assertEqual(bad[0]["cell_id"], "CAB-A-03")
        self.assertIn("cell_ghost_occupied", bad[0]["issues"])

        trace = audit_mod.trace_release(self.conn, self.occ_id)
        self.assertFalse(trace["ok"])
        self.assertIn("cell_ghost_occupied", {i["code"] for i in trace["issues"]})

    def test_trigger_auto_frees_cell_on_close(self):
        """即使应用漏翻 cells，关账触发器也兜底翻 free，不留幽灵角标。"""
        # 手工走边+审计，但不 UPDATE cells，模拟应用漏翻；触发器应兜底。
        occ = self.conn.execute(
            "SELECT * FROM occupancies WHERE id=?", (self.occ_id,)
        ).fetchone()
        proof = self.conn.execute(
            "SELECT * FROM occupancy_proofs WHERE occupancy_id=? ORDER BY id DESC LIMIT 1",
            (self.occ_id,),
        ).fetchone()
        edge_id = reclaim._insert_edge(
            self.conn, occ, proof, as_of=AS_OF, overdue_hours=48, reason="overdue"
        )
        edge_row = self._edge_row()
        audit_mod.insert_release_audit(self.conn, edge_row)
        self.conn.execute(
            "UPDATE occupancies SET status='released', closed_at=?, close_reason='reclaim' "
            "WHERE id=?",
            (AS_OF, self.occ_id),
        )
        self.conn.commit()
        cell = self.conn.execute(
            "SELECT status FROM cells WHERE id=?", (occ["cell_id"],)
        ).fetchone()
        self.assertEqual(cell["status"], "free")
        self.assertEqual(edge_id, f"REDGE-{self.occ_id}")

    # ------------------------------------------------------------ 缺行核证

    def test_missing_edge_fixture_fails_release_audit(self):
        """遗留缺边夹具：释放审计核证必须报 missing_release_audit。"""
        from app.services import reclaim_fixtures

        reclaim_fixtures.load("missing-edge", self.conn)
        report = audit_mod.validate_releases(self.conn)
        self.assertFalse(report["ok"])
        self.assertIn("missing_release_audit", self._codes(report))
        self.assertGreaterEqual(report["missing_audit_count"], 1)

        trace = audit_mod.trace_release(self.conn, "OCC-MISSING-EDGE-1")
        self.assertFalse(trace["ok"])
        self.assertIsNone(trace["audit"])
        self.assertEqual(trace["issues"][0]["code"], "missing_release_audit")

    def test_pickup_close_needs_no_release_audit(self):
        """取件关闭不走回收边，不要求释放审计，核证仍通过。"""
        self.conn.execute(
            """
            UPDATE occupancies SET status='released', closed_at=?, close_reason='pickup'
             WHERE id=?
            """,
            (AS_OF, self.occ_id),
        )
        self.conn.execute("UPDATE cells SET status='free' WHERE id='CAB-A-03'")
        self.conn.commit()
        report = audit_mod.validate_releases(self.conn)
        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["audit_count"], 0)
        self.assertEqual(report["reclaim_closed"], 0)

    # ------------------------------------------------------------ 并发：邻格不串键

    def test_concurrent_reclaims_keep_neighbour_audits_isolated(self):
        """两线程并发回收相邻格口：各写各的边键/审计，互不串改，失败方整体回滚。"""
        neighbour = self._open_occupancy("CAB-A-04", "TN-N1", "2026-11-10T09:00:00Z")
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def worker(occ_id: str) -> None:
            conn = connect()
            try:
                barrier.wait(timeout=10)
                reclaim.reclaim_occupancy(conn, occ_id, as_of=AS_OF)
            except Exception as exc:  # 锁竞争允许失败，但必须干净回滚
                errors.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=(self.occ_id,))
        t2 = threading.Thread(target=worker, args=(neighbour,))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # SQLite 写锁串行化：可能两笔都成功，也可能一笔 busy 失败；
        # 但任何成功笔都必须三联齐全且不碰邻格。
        for occ_id, cell_id in ((self.occ_id, "CAB-A-03"), (neighbour, "CAB-A-04")):
            occ = self.conn.execute(
                "SELECT status FROM occupancies WHERE id=?", (occ_id,)
            ).fetchone()
            audit = self.conn.execute(
                "SELECT * FROM cell_release_audits WHERE occupancy_id=?", (occ_id,)
            ).fetchone()
            edge = self.conn.execute(
                "SELECT * FROM reclaim_edges WHERE occupancy_id=?", (occ_id,)
            ).fetchone()
            if occ["status"] == "released":
                self.assertIsNotNone(edge, f"{occ_id} 已关账却缺边")
                self.assertIsNotNone(audit, f"{occ_id} 已关账却缺释放审计")
                self.assertEqual(audit["cell_id"], cell_id)
                self.assertEqual(audit["edge_id"], edge["id"])
                self.assertEqual(audit["released_at"], edge["closed_at"])
                self.assertEqual(
                    self.conn.execute(
                        "SELECT status FROM cells WHERE id=?", (cell_id,)
                    ).fetchone()["status"],
                    "free",
                )
            else:
                # 失败笔必须干净：无边、无审计、仍 occupied
                self.assertIsNone(edge)
                self.assertIsNone(audit)

        # 审计行绝不可能指向别人的占用/格口
        for row in self.conn.execute("SELECT * FROM cell_release_audits").fetchall():
            edge = self.conn.execute(
                "SELECT * FROM reclaim_edges WHERE id=?", (row["edge_id"],)
            ).fetchone()
            self.assertIsNotNone(edge)
            self.assertEqual(edge["occupancy_id"], row["occupancy_id"])
            self.assertEqual(edge["cell_id"], row["cell_id"])

        report = audit_mod.validate_releases(self.conn)
        self.assertTrue(report["ok"], report["failures"])


if __name__ == "__main__":
    unittest.main()
