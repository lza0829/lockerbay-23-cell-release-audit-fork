"""格口释放审计（实现）。

形态择一：本模块采用**独立 cell_release_audits 表**
（不采用回收边不可变 cell_release 快照列）。

失败闭择一：采用**关账同事务写边并写释放审计，缺一回滚**——
回收事务内先 INSERT 回收边，再 INSERT 本审计（此时占用仍为 occupied），
最后才 UPDATE 占用为 released。SQLite 触发器库级兜底：
  * 释放审计必须与回收边同源：edge_id（边键）指向同一条边，
    且 cell_id / released_at 与边、占用完全一致，写在仍开放的占用上；
  * 回收关账（occupied -> released, close_reason='reclaim'）前，
    必须已存在同源审计，否则 ABORT，整笔回滚；
  * 审计行不可更新、不可删除（不可变审计）；
  * 关账后若格口已无其他开放占用，触发器兜底把 cells 翻 free。

硬约束在本模块只读核证 `validate_releases()` 中全部可报：
  * 有回收边而无释放审计（missing_release_audit）；
  * 释放 cell_id 与边占用格口分叉（audit_cell_diverges）；
  * 释放时刻与边关闭时刻分叉（audit_released_at_diverges）；
  * 格口幽灵占用角标（cell_ghost_occupied）/ 假空闲（cell_false_free）；
  * 边键外键 + UNIQUE(occupancy_id)/UNIQUE(edge_id) 保证邻格审计无法被本笔串改。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

STATUS = "implemented"

CLOSE_REASON_RECLAIM = "reclaim"
STATUS_OCCUPIED = "occupied"
STATUS_RELEASED = "released"


class ReleaseAuditError(Exception):
    """释放审计写入被拒绝（随回收事务整体回滚，不留半截）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- write path


def insert_release_audit(conn: sqlite3.Connection, edge: sqlite3.Row) -> int:
    """在回收事务内、占用关账前写入格口释放审计。

    同源对撞（边键/格口/时刻、占用仍开放）由 trg_release_audit_matches_edge
    在库级强制；任何不一致抛 IntegrityError，由回收事务捕获后整笔回滚。
    返回审计行 id。
    """
    try:
        cur = conn.execute(
            """
            INSERT INTO cell_release_audits(
              occupancy_id, cell_id, edge_id, released_at, close_reason, created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                edge["occupancy_id"],
                edge["cell_id"],
                edge["id"],
                edge["closed_at"],
                CLOSE_REASON_RECLAIM,
                utc_now(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ReleaseAuditError(
            "release_audit_conflict", f"格口释放审计写入被拒绝：{exc}"
        ) from exc
    return int(cur.lastrowid)


# ---------------------------------------------------------------- read helpers


def _edge_row(conn: sqlite3.Connection, edge_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reclaim_edges WHERE id=?", (edge_id,)
    ).fetchone()


def _occ_row(conn: sqlite3.Connection, occupancy_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM occupancies WHERE id=?", (occupancy_id,)
    ).fetchone()


def _cell_row(conn: sqlite3.Connection, cell_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cells WHERE id=?", (cell_id,)).fetchone()


def _open_occupancy_count(conn: sqlite3.Connection, cell_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM occupancies "
        "WHERE cell_id=? AND status='occupied' AND closed_at IS NULL",
        (cell_id,),
    ).fetchone()["c"]


# ---------------------------------------------------------------- verification


def _audit_issues(
    audit: sqlite3.Row,
    edge: sqlite3.Row | None,
    occ: sqlite3.Row | None,
    cell: sqlite3.Row | None,
) -> list[dict[str, Any]]:
    """审计行自身的同源核对（边/占用）；格口 free 态核对需要 conn，见 _audit_issues_with_conn。"""
    issues: list[dict[str, Any]] = []

    def add(code: str, detail: str) -> None:
        issues.append(
            {
                "code": code,
                "occupancy_id": audit["occupancy_id"],
                "cell_id": audit["cell_id"],
                "edge_id": audit["edge_id"],
                "detail": detail,
            }
        )

    if edge is None:
        add("audit_edge_missing", "释放审计的边键找不到回收边")
    else:
        if edge["occupancy_id"] != audit["occupancy_id"]:
            add("audit_edge_occupancy_diverges", "释放审计与边键指向的占用不一致")
        if edge["cell_id"] != audit["cell_id"]:
            add("audit_cell_diverges",
                f"释放审计 cell {audit['cell_id']} 与回收边 cell {edge['cell_id']} 分叉")
        if edge["closed_at"] != audit["released_at"]:
            add("audit_released_at_diverges",
                f"释放时刻 {audit['released_at']} 与边关闭时刻 {edge['closed_at']} 分叉")

    if occ is None:
        add("audit_occupancy_missing", "释放审计指向的占用不存在")
    else:
        if occ["cell_id"] != audit["cell_id"]:
            add("audit_cell_diverges",
                f"释放审计 cell {audit['cell_id']} 与占用格口 {occ['cell_id']} 分叉")
        if occ["closed_at"] != audit["released_at"]:
            add("audit_released_at_diverges",
                f"释放时刻 {audit['released_at']} 与占用关闭时刻 {occ['closed_at']} 分叉")
        if occ["status"] != STATUS_RELEASED or occ["closed_at"] is None:
            add("audit_occupancy_not_released", "有释放审计但占用仍为开放态")
        if occ["close_reason"] != audit["close_reason"]:
            add("audit_close_reason_mismatch",
                f"审计关闭原因 {audit['close_reason']} 与占用 {occ['close_reason']} 不一致")

    return issues


def _audit_issues_with_conn(
    conn: sqlite3.Connection,
    audit: sqlite3.Row,
    edge: sqlite3.Row | None,
    occ: sqlite3.Row | None,
    cell: sqlite3.Row | None,
) -> list[dict[str, Any]]:
    issues = _audit_issues(audit, edge, occ, cell)
    if cell is not None:
        open_count = _open_occupancy_count(conn, audit["cell_id"])
        if cell["status"] == "occupied" and open_count == 0:
            issues.append(
                {
                    "code": "cell_ghost_occupied",
                    "occupancy_id": audit["occupancy_id"],
                    "cell_id": audit["cell_id"],
                    "edge_id": audit["edge_id"],
                    "detail": "占用已 released 且无其他开放占用，格口仍亮占用角标",
                }
            )
        if cell["status"] == "free" and open_count > 0:
            issues.append(
                {
                    "code": "cell_false_free",
                    "occupancy_id": audit["occupancy_id"],
                    "cell_id": audit["cell_id"],
                    "edge_id": audit["edge_id"],
                    "detail": "格口显示 free 但仍有开放占用",
                }
            )
    return issues


def reconcile_cells(conn: sqlite3.Connection) -> dict[str, Any]:
    """只读：格口态物化对账，抓幽灵占用角标 / 假空闲。"""
    rows = conn.execute("SELECT * FROM cells ORDER BY id").fetchall()
    cells: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for cell in rows:
        open_occs = conn.execute(
            "SELECT id FROM occupancies WHERE cell_id=? AND status='occupied' "
            "AND closed_at IS NULL ORDER BY opened_at, id",
            (cell["id"],),
        ).fetchall()
        latest = conn.execute(
            "SELECT id, status, close_reason, closed_at FROM occupancies "
            "WHERE cell_id=? ORDER BY opened_at DESC, id DESC LIMIT 1",
            (cell["id"],),
        ).fetchone()
        issues: list[str] = []
        if cell["status"] == "occupied" and not open_occs:
            issues.append("cell_ghost_occupied")
        if cell["status"] == "free" and open_occs:
            issues.append("cell_false_free")
        for code in issues:
            failures.append(
                {
                    "code": code,
                    "occupancy_id": open_occs[0]["id"] if open_occs else (latest["id"] if latest else None),
                    "cell_id": cell["id"],
                    "edge_id": None,
                    "detail": {
                        "cell_ghost_occupied": "格口亮占用角标但无开放占用（幽灵占用）",
                        "cell_false_free": "格口显示空闲但存在开放占用（假空闲）",
                    }[code],
                }
            )
        cells.append(
            {
                "cell_id": cell["id"],
                "cell_status": cell["status"],
                "open_occupancy_ids": [r["id"] for r in open_occs],
                "latest_occupancy_id": latest["id"] if latest else None,
                "latest_occupancy_status": latest["status"] if latest else None,
                "ok": not issues,
                "issues": issues,
            }
        )
    return {
        "ok": not failures,
        "cell_count": len(cells),
        "cell_mismatch_count": len(failures),
        "cells": cells,
        "failures": failures,
    }


def validate_releases(conn: sqlite3.Connection) -> dict[str, Any]:
    """只读核证：边、释放审计、格口 free 三联互证；不写任何行。"""
    audit_rows = conn.execute(
        "SELECT * FROM cell_release_audits ORDER BY released_at, id"
    ).fetchall()
    audits: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    # 同一（问题码、占用、格口）只报一次：边键有无不重复计数。
    seen: set[tuple[str, str | None, str | None]] = set()

    def add_failure(code: str, occupancy_id: str | None, cell_id: str | None,
                    edge_id: str | None, detail: str) -> None:
        key = (code, occupancy_id, cell_id)
        if key in seen:
            return
        seen.add(key)
        failures.append(
            {
                "code": code,
                "occupancy_id": occupancy_id,
                "cell_id": cell_id,
                "edge_id": edge_id,
                "detail": detail,
            }
        )

    for audit in audit_rows:
        edge = _edge_row(conn, audit["edge_id"])
        occ = _occ_row(conn, audit["occupancy_id"])
        cell = _cell_row(conn, audit["cell_id"])
        issues = _audit_issues_with_conn(conn, audit, edge, occ, cell)
        for issue in issues:
            add_failure(
                issue["code"], issue["occupancy_id"], issue["cell_id"],
                issue["edge_id"], issue["detail"],
            )
        audits.append(
            {
                **dict(audit),
                "edge_closed_at": edge["closed_at"] if edge else None,
                "occupancy_status": occ["status"] if occ else None,
                "occupancy_closed_at": occ["closed_at"] if occ else None,
                "cell_status": cell["status"] if cell else None,
                "ok": not issues,
                "issues": issues,
            }
        )

    # 反向检查：有回收边而无释放审计（硬约束：禁止有边无审计）。
    edge_rows = conn.execute("SELECT * FROM reclaim_edges ORDER BY decided_at, id").fetchall()
    audit_by_occ = {a["occupancy_id"]: a for a in audit_rows}
    missing_audit_occs: dict[str, dict[str, Any]] = {}
    for edge in edge_rows:
        if edge["occupancy_id"] not in audit_by_occ:
            item = {
                "code": "missing_release_audit",
                "occupancy_id": edge["occupancy_id"],
                "cell_id": edge["cell_id"],
                "edge_id": edge["id"],
                "detail": "回收边已落但格口释放审计缺行",
            }
            missing_audit_occs[edge["occupancy_id"]] = item
            add_failure(
                item["code"], item["occupancy_id"], item["cell_id"],
                item["edge_id"], item["detail"],
            )

    # 回收关账占用无审计（历史遗留/绕过应用直插同样必须报出；与上一检查按占用合并）。
    orphan_occ = conn.execute(
        """
        SELECT o.* FROM occupancies o
        LEFT JOIN cell_release_audits a ON a.occupancy_id = o.id
        WHERE o.close_reason = 'reclaim' AND a.id IS NULL
        ORDER BY o.closed_at, o.id
        """
    ).fetchall()
    for occ in orphan_occ:
        missing_audit_occs.setdefault(
            occ["id"],
            {
                "code": "missing_release_audit",
                "occupancy_id": occ["id"],
                "cell_id": occ["cell_id"],
                "edge_id": None,
                "detail": "占用按回收关账但无格口释放审计",
            },
        )
        add_failure(
            "missing_release_audit", occ["id"], occ["cell_id"], None,
            "占用按回收关账但无格口释放审计",
        )
    missing_audit_count = len(missing_audit_occs)

    cell_report = reconcile_cells(conn)
    for issue in cell_report["failures"]:
        add_failure(
            issue["code"], issue["occupancy_id"], issue["cell_id"],
            issue["edge_id"], issue["detail"],
        )

    reclaim_closed = conn.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE close_reason='reclaim'"
    ).fetchone()["c"]

    return {
        "ok": not failures,
        "generated_at": utc_now(),
        "audit_count": len(audits),
        "edge_count": len(edge_rows),
        "reclaim_closed": reclaim_closed,
        "missing_audit_count": missing_audit_count,
        "cell_mismatch_count": cell_report["cell_mismatch_count"],
        "audits": audits,
        "cells": cell_report["cells"],
        "failures": failures,
    }


def list_releases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """只读列表：逐行释放审计 + 与边/占用/格口的核对结论。"""
    report = validate_releases(conn)
    return report["audits"]


def trace_release(conn: sqlite3.Connection, occupancy_id: str) -> dict[str, Any] | None:
    """只读：由占用追到释放审计、回收边、占用关账行与格口态，并给出三联核对结论。"""
    occ = _occ_row(conn, occupancy_id)
    if occ is None:
        return None
    audit = conn.execute(
        "SELECT * FROM cell_release_audits WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()
    edge = conn.execute(
        "SELECT * FROM reclaim_edges WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()
    cell = _cell_row(conn, occ["cell_id"])
    proof = conn.execute(
        "SELECT * FROM occupancy_proofs WHERE occupancy_id=? ORDER BY id DESC LIMIT 1",
        (occupancy_id,),
    ).fetchone()

    issues: list[dict[str, Any]] = []
    if audit is not None:
        issues = _audit_issues_with_conn(conn, audit, edge, occ, cell)
    elif occ["close_reason"] == CLOSE_REASON_RECLAIM:
        issues = [
            {
                "code": "missing_release_audit",
                "occupancy_id": occupancy_id,
                "cell_id": occ["cell_id"],
                "edge_id": edge["id"] if edge else None,
                "detail": "回收边已落但格口释放审计缺行" if edge else "占用按回收关账但无释放审计",
            }
        ]

    return {
        "ok": not issues,
        "occupancy": dict(occ),
        "audit": dict(audit) if audit else None,
        "edge": dict(edge) if edge else None,
        "proof": dict(proof) if proof else None,
        "cell": dict(cell) if cell else None,
        "open_occupancies_on_cell": _open_occupancy_count(conn, occ["cell_id"]),
        "issues": issues,
    }
