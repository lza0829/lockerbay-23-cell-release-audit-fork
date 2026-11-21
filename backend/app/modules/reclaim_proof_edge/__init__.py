"""回收边与核证同源（实现）。

形态择一：本模块采用**独立 reclaim_edges 表**（不采用 occupancy_proofs 追加段）；
格口释放审计另由独立 cell_release_audits 表承载（见 cell_release_audit 模块）。

失败闭择一：本模块采用**关占用与写边、写释放审计同事务**——
先 INSERT 回收边（此时占用仍为 occupied），再 INSERT 格口释放审计
（cell_id、occupancy_id、released_at、边键与边对撞），最后 UPDATE 占用为 released，
同一事务内给占用核证 occupancy_proofs 盖 closed_at、把格口翻 free。
SQLite 触发器保证：
  * 任何 occupied -> released 的关账都必须带 close_reason；
  * close_reason='reclaim' 的关账必须已经存在同占用的回收边与同源释放审计；
  * 回收边 / 释放审计只能在占用仍开放时写入，且必须与核证/边同源；
  * 回收边与释放审计均不可更新、不可删除（不可变审计行）；
  * 关账后格口无其他开放占用时，触发器兜底翻 free，杜绝幽灵占用角标。

无边则核证校验失败：`validate_edges()` 会同时检查
  * 回收关闭的占用缺边（missing_edge）；
  * 有边但占用仍 occupied（occupancy_still_open）；
  * 边与占用状态 / 关闭时刻 / 核证 / 小时快照 对不上。
释放审计三联（边、审计、格口 free）由 cell_release_audit.validate_releases() 核证。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.modules import cell_release_audit

STATUS = "implemented"

REASON_OVERDUE = "overdue"
CLOSE_REASON_RECLAIM = "reclaim"
CLOSE_REASON_PICKUP = "pickup"
STATUS_OCCUPIED = "occupied"
STATUS_RELEASED = "released"


class ReclaimError(Exception):
    """回收被拒绝（不会留下半截写入）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- time helpers


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_ts(value: str | None) -> str:
    """把 ISO8601 时间统一成 `YYYY-MM-DDTHH:MM:SSZ`（便于字符串比较）。"""
    if not value:
        return utc_now()
    text = value.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReclaimError("bad_timestamp", f"无法解析时刻：{value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_bucket(value: str) -> str:
    """裁决时刻所在 UTC 小时快照，例如 2026-11-20T06:00:00Z。"""
    ts = normalize_ts(value)
    return ts[:13] + ":00:00Z"


def deadline_for(opened_at: str, overdue_hours: int) -> str:
    opened = datetime.strptime(normalize_ts(opened_at), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (opened + timedelta(hours=int(overdue_hours))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_overdue_hours(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='overdue_hours'").fetchone()
    return int(row["value"]) if row else 48


# ---------------------------------------------------------------- read helpers


def _occ_row(conn: sqlite3.Connection, occupancy_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM occupancies WHERE id=?", (occupancy_id,)
    ).fetchone()


def _edge_row(conn: sqlite3.Connection, occupancy_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reclaim_edges WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()


def _latest_proof(conn: sqlite3.Connection, occupancy_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM occupancy_proofs WHERE occupancy_id=? ORDER BY id DESC LIMIT 1",
        (occupancy_id,),
    ).fetchone()


def overdue_candidates(
    conn: sqlite3.Connection,
    as_of: str | None = None,
    overdue_hours: int | None = None,
) -> list[dict[str, Any]]:
    """只读：列出 opened_at + overdue_hours <= as_of 且仍开放的占用。"""
    at = normalize_ts(as_of)
    hours = int(overdue_hours if overdue_hours is not None else _read_overdue_hours(conn))
    rows = conn.execute(
        "SELECT * FROM occupancies WHERE status=? AND closed_at IS NULL ORDER BY opened_at, id",
        (STATUS_OCCUPIED,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        deadline = deadline_for(row["opened_at"], hours)
        if deadline <= at:
            proof = _latest_proof(conn, row["id"])
            out.append(
                {
                    "occupancy_id": row["id"],
                    "cell_id": row["cell_id"],
                    "tracking_no": row["tracking_no"],
                    "opened_at": row["opened_at"],
                    "deadline_at": deadline,
                    "overdue_hours": hours,
                    "proof_id": proof["id"] if proof else None,
                    "has_proof": proof is not None,
                }
            )
    return out


# ---------------------------------------------------------------- write path


def _insert_edge(
    conn: sqlite3.Connection,
    occ: sqlite3.Row,
    proof: sqlite3.Row,
    *,
    as_of: str,
    overdue_hours: int,
    reason: str,
) -> str:
    edge_id = f"REDGE-{occ['id']}"
    edge_closed_at = as_of
    bucket = hour_bucket(as_of)
    occupied_before = conn.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE status=?", (STATUS_OCCUPIED,)
    ).fetchone()["c"]
    reclaimed_at_hour = conn.execute(
        "SELECT COUNT(*) AS c FROM reclaim_edges WHERE hour_bucket=?", (bucket,)
    ).fetchone()["c"]
    snapshot = {
        "hour": bucket,
        "as_of": as_of,
        "occupied_before": occupied_before,
        "occupied_after": occupied_before - 1,
        "reclaimed_in_hour": reclaimed_at_hour + 1,
    }
    try:
        conn.execute(
            """
            INSERT INTO reclaim_edges(
              id, occupancy_id, proof_id, cell_id, tracking_no, opened_at,
              reason, status_before, status_after, closed_at, hour_bucket,
              snapshot_json, overdue_hours, deadline_at, decided_at, decision, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                edge_id,
                occ["id"],
                proof["id"],
                occ["cell_id"],
                occ["tracking_no"],
                occ["opened_at"],
                reason,
                STATUS_OCCUPIED,
                STATUS_RELEASED,
                edge_closed_at,
                bucket,
                json.dumps(snapshot, ensure_ascii=False),
                int(overdue_hours),
                deadline_for(occ["opened_at"], overdue_hours),
                as_of,
                REASON_OVERDUE,
                utc_now(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ReclaimError("edge_conflict", f"回收边写入被拒绝：{exc}") from exc
    return edge_id


def reclaim_occupancy(
    conn: sqlite3.Connection,
    occupancy_id: str,
    *,
    as_of: str | None = None,
    overdue_hours: int | None = None,
    reason: str = REASON_OVERDUE,
) -> dict[str, Any]:
    """同事务关账：写边 -> 写格口释放审计 -> 关占用 -> 核证盖关闭时刻 -> 格口 free。

    任一步失败整体回滚；不存在「占用已 released 但无回收边/释放审计」的提交态。
    """
    at = normalize_ts(as_of)
    hours = int(overdue_hours if overdue_hours is not None else _read_overdue_hours(conn))

    occ = _occ_row(conn, occupancy_id)
    if occ is None:
        raise ReclaimError("occupancy_missing", f"占用不存在：{occupancy_id}")
    if occ["status"] != STATUS_OCCUPIED or occ["closed_at"] is not None:
        raise ReclaimError("occupancy_not_open", f"占用已关闭：{occupancy_id}")
    if _edge_row(conn, occupancy_id) is not None:
        raise ReclaimError("edge_exists", f"占用已有回收边：{occupancy_id}")

    proof = _latest_proof(conn, occupancy_id)
    if proof is None:
        raise ReclaimError("proof_missing", f"占用缺少占用核证，无法同源：{occupancy_id}")

    deadline = deadline_for(occ["opened_at"], hours)
    if deadline > at:
        raise ReclaimError(
            "not_overdue", f"未到回收截止：{occupancy_id} deadline={deadline} as_of={at}"
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise ReclaimError("transaction_busy", f"无法开启回收事务：{exc}") from exc

    try:
        edge_id = _insert_edge(
            conn, occ, proof, as_of=at, overdue_hours=hours, reason=reason
        )
        edge_row = _edge_row(conn, occupancy_id)
        # 关账同事务写边并写释放审计：审计必须在占用仍 occupied 时与边对撞写入，
        # 随后的关账触发器会再核一遍同源关系，缺一回滚。
        try:
            release_audit_id = cell_release_audit.insert_release_audit(conn, edge_row)
        except cell_release_audit.ReleaseAuditError as exc:
            raise ReclaimError(exc.code, exc.message) from exc
        bucket = hour_bucket(at)
        snapshot = json.loads(
            conn.execute(
                "SELECT snapshot_json FROM reclaim_edges WHERE id=?", (edge_id,)
            ).fetchone()["snapshot_json"]
        )
        cur = conn.execute(
            """
            UPDATE occupancies
               SET status=?, closed_at=?, close_reason=?
             WHERE id=? AND status=? AND closed_at IS NULL
            """,
            (STATUS_RELEASED, at, CLOSE_REASON_RECLAIM, occupancy_id, STATUS_OCCUPIED),
        )
        if cur.rowcount != 1:
            raise ReclaimError("close_lost_race", f"占用在回收事务中被改动：{occupancy_id}")
        conn.execute(
            "UPDATE occupancy_proofs SET closed_at=? WHERE id=?",
            (at, proof["id"]),
        )
        # 应用层显式翻 free；trg_occ_release_frees_cell 同时做库级兜底，防幽灵角标。
        conn.execute("UPDATE cells SET status='free' WHERE id=?", (occ["cell_id"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "edge_id": edge_id,
        "release_audit_id": release_audit_id,
        "occupancy_id": occupancy_id,
        "cell_id": occ["cell_id"],
        "tracking_no": occ["tracking_no"],
        "reason": reason,
        "status_before": STATUS_OCCUPIED,
        "status_after": STATUS_RELEASED,
        "closed_at": at,
        "hour_bucket": bucket,
        "snapshot": snapshot,
        "overdue_hours": hours,
        "deadline_at": deadline,
        "proof_id": proof["id"],
    }


def scan_and_reclaim(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    overdue_hours: int | None = None,
    limit: int = 200,
    dry_run: bool = False,
) -> dict[str, Any]:
    """超时扫描：逐笔走同事务回收；单笔失败只记入 skipped，不污染其他笔。"""
    at = normalize_ts(as_of)
    hours = int(overdue_hours if overdue_hours is not None else _read_overdue_hours(conn))
    candidates = overdue_candidates(conn, as_of=at, overdue_hours=hours)[: int(limit)]
    reclaimed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if not dry_run:
        for cand in candidates:
            try:
                reclaimed.append(
                    reclaim_occupancy(
                        conn, cand["occupancy_id"], as_of=at, overdue_hours=hours
                    )
                )
            except ReclaimError as exc:
                skipped.append(
                    {"occupancy_id": cand["occupancy_id"], "code": exc.code, "message": exc.message}
                )

    return {
        "as_of": at,
        "overdue_hours": hours,
        "dry_run": bool(dry_run),
        "scanned": len(candidates),
        "reclaimed": reclaimed,
        "skipped": skipped,
        "candidates": candidates,
        "generated_at": utc_now(),
    }


# ---------------------------------------------------------------- verification


def _edge_issues(
    edge: sqlite3.Row,
    occ: sqlite3.Row | None,
    proof: sqlite3.Row | None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add(code: str, detail: str) -> None:
        issues.append(
            {"code": code, "occupancy_id": edge["occupancy_id"], "edge_id": edge["id"], "detail": detail}
        )

    if not edge["reason"]:
        add("reason_missing", "回收边缺少回收原因")
    if edge["status_before"] != STATUS_OCCUPIED:
        add("status_before_invalid", f"关闭前状态应为 occupied，实为 {edge['status_before']}")
    if edge["status_after"] != STATUS_RELEASED:
        add("status_after_invalid", f"关闭后状态应为 released，实为 {edge['status_after']}")
    if edge["hour_bucket"] != hour_bucket(edge["decided_at"]):
        add("hour_snapshot_mismatch", "小时快照与裁决时刻不在同一小时")
    if edge["closed_at"] != edge["decided_at"]:
        add("close_at_not_decided_at", "边的关闭时刻与裁决时刻不一致")
    try:
        snapshot = json.loads(edge["snapshot_json"] or "{}")
    except ValueError:
        snapshot = None
    if not isinstance(snapshot, dict) or not snapshot:
        add("snapshot_missing", "回收边缺少小时快照")
    else:
        if snapshot.get("hour") != edge["hour_bucket"]:
            add("snapshot_hour_mismatch", "小时快照的 hour 与边不一致")
        if snapshot.get("as_of") != edge["decided_at"]:
            add("snapshot_as_of_mismatch", "小时快照的 as_of 与裁决时刻不一致")

    if occ is None:
        add("occupancy_missing", "回收边指向的占用不存在")
        return issues

    if occ["status"] == STATUS_OCCUPIED or occ["closed_at"] is None:
        add("occupancy_still_open", "有回收边但占用仍为开放/occupied")
    elif occ["status"] != edge["status_after"]:
        add("status_mismatch", f"占用状态 {occ['status']} 与边记录 {edge['status_after']} 不一致")

    if occ["close_reason"] != CLOSE_REASON_RECLAIM:
        add("close_reason_mismatch", f"占用关闭原因应为 reclaim，实为 {occ['close_reason']}")
    if occ["closed_at"] != edge["closed_at"]:
        add("closed_at_mismatch", f"占用关闭时刻 {occ['closed_at']} 与边记录 {edge['closed_at']} 不一致")
    if occ["cell_id"] != edge["cell_id"] or occ["tracking_no"] != edge["tracking_no"]:
        add("occupancy_snapshot_mismatch", "边记录的格口/运单与占用不一致")

    if proof is None:
        add("proof_missing", "回收边指向的占用核证不存在")
    else:
        if (
            proof["occupancy_id"] != edge["occupancy_id"]
            or proof["cell_id"] != edge["cell_id"]
            or proof["tracking_no"] != edge["tracking_no"]
            or proof["opened_at"] != edge["opened_at"]
        ):
            add("proof_mismatch", "回收边与占用核证不同源（占用/格口/运单/开启时刻不一致）")
        if proof["closed_at"] != edge["closed_at"]:
            add("proof_close_mismatch", "占用核证的关闭时刻未与回收边同步")
    return issues


def validate_edges(conn: sqlite3.Connection) -> dict[str, Any]:
    """只读核证：返回全部回收边、逐边校验与汇总，不写任何行。"""
    edge_rows = conn.execute("SELECT * FROM reclaim_edges ORDER BY decided_at, id").fetchall()
    edges: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for edge in edge_rows:
        occ = _occ_row(conn, edge["occupancy_id"])
        proof = conn.execute(
            "SELECT * FROM occupancy_proofs WHERE id=?", (edge["proof_id"],)
        ).fetchone()
        issues = _edge_issues(edge, occ, proof)
        failures.extend(issues)
        edges.append(
            {
                **dict(edge),
                "occupancy_status": occ["status"] if occ else None,
                "occupancy_close_reason": occ["close_reason"] if occ else None,
                "occupancy_closed_at": occ["closed_at"] if occ else None,
                "proof_closed_at": proof["closed_at"] if proof else None,
                "ok": not issues,
                "issues": issues,
            }
        )

    # 反向检查：按回收关闭但缺边的占用（例如绕过应用直接写库的遗留夹具）。
    orphan_rows = conn.execute(
        """
        SELECT o.* FROM occupancies o
        LEFT JOIN reclaim_edges e ON e.occupancy_id = o.id
        WHERE o.close_reason = ? AND e.id IS NULL
        ORDER BY o.closed_at, o.id
        """,
        (CLOSE_REASON_RECLAIM,),
    ).fetchall()
    for occ in orphan_rows:
        issue = {
            "code": "missing_edge",
            "occupancy_id": occ["id"],
            "edge_id": None,
            "detail": "占用按回收关闭但无 reclaim_edges 行",
        }
        failures.append(issue)

    reclaim_closed = conn.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE close_reason=?",
        (CLOSE_REASON_RECLAIM,),
    ).fetchone()["c"]

    # 已关闭却没有任何关闭原因：审计断链，同样计入失败。
    unreasoned = conn.execute(
        """
        SELECT id FROM occupancies
         WHERE status = 'released'
           AND (close_reason IS NULL OR close_reason = '')
         ORDER BY closed_at, id
        """
    ).fetchall()
    for occ in unreasoned:
        failures.append(
            {
                "code": "close_reason_missing",
                "occupancy_id": occ["id"],
                "edge_id": None,
                "detail": "占用已关闭但未记录关闭原因，无法审计",
            }
        )

    return {
        "ok": not failures,
        "generated_at": utc_now(),
        "edge_count": len(edges),
        "reclaim_closed": reclaim_closed,
        "missing_edge_count": len(orphan_rows),
        "unreasoned_closed": len(unreasoned),
        "edges": edges,
        "failures": failures,
    }


def trace_occupancy(conn: sqlite3.Connection, occupancy_id: str) -> dict[str, Any] | None:
    """只读：由占用（尤其已关闭占用）追到回收边与核证，并给出校验结论。"""
    occ = _occ_row(conn, occupancy_id)
    if occ is None:
        return None
    edge = _edge_row(conn, occupancy_id)
    proof = _latest_proof(conn, occupancy_id)

    issues: list[dict[str, Any]] = []
    if edge is not None:
        edge_proof = conn.execute(
            "SELECT * FROM occupancy_proofs WHERE id=?", (edge["proof_id"],)
        ).fetchone()
        issues = _edge_issues(edge, occ, edge_proof)
    elif occ["close_reason"] == CLOSE_REASON_RECLAIM:
        issues = [
            {
                "code": "missing_edge",
                "occupancy_id": occupancy_id,
                "edge_id": None,
                "detail": "占用按回收关闭但无 reclaim_edges 行",
            }
        ]
    elif occ["status"] == STATUS_RELEASED and not occ["close_reason"]:
        issues = [
            {
                "code": "close_reason_missing",
                "occupancy_id": occupancy_id,
                "edge_id": None,
                "detail": "占用已关闭但未记录关闭原因，无法审计",
            }
        ]

    return {
        "ok": not issues,
        "occupancy": dict(occ),
        "edge": dict(edge) if edge else None,
        "proof": dict(proof) if proof else None,
        "reclaim": occ["close_reason"] == CLOSE_REASON_RECLAIM,
        "issues": issues,
    }
