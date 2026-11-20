"""本地回收夹具（SQLite）。

两类夹具：
  * reclaim_case.json       —— 一笔可超时回收的投柜（用于跑通回收）；
  * reclaim_missing_edge.json —— 故意缺边的遗留数据（用于「缺边即核证失败」演示）。

缺边夹具直接 INSERT 成已 released/reclaim 的历史行：库级触发器只守 UPDATE 路径，
因此这正是「历史遗留/绕过应用写库」的真实形态，校验接口必须把它识别为失败。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.db import FIXTURES_DIR
from app.modules import reclaim_proof_edge as edge_reclaim

CASE_FIXTURE = "reclaim_case.json"
MISSING_EDGE_FIXTURE = "reclaim_missing_edge.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(name: str) -> dict[str, Any] | None:
    path = FIXTURES_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_reclaim_case(conn) -> dict[str, Any]:
    """装载一笔超时未取的投柜；占用与占用核证同源生成。"""
    data = _load_json(CASE_FIXTURE)
    if data is None:
        return {"fixture": CASE_FIXTURE, "loaded": False, "message": "夹具不存在"}

    created: list[str] = []
    for ev in data.get("events", []):
        conn.execute(
            """
            INSERT OR IGNORE INTO parcel_events(
              id, cabinet_id, cell_id, event_type, event_ts, tracking_no, payload_json, channel
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                ev["id"],
                ev["cabinet_id"],
                ev.get("cell_id"),
                ev["event_type"],
                ev["event_ts"],
                ev.get("tracking_no"),
                json.dumps(ev.get("payload", {}), ensure_ascii=False),
                "primary",
            ),
        )
        if ev["event_type"] != "drop" or not ev.get("cell_id"):
            continue
        cell = conn.execute(
            "SELECT status FROM cells WHERE id=?", (ev["cell_id"],)
        ).fetchone()
        if cell is None:
            continue
        oid = f"OCC-{ev['cell_id']}-{ev['id']}"
        exists = conn.execute("SELECT 1 FROM occupancies WHERE id=?", (oid,)).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO occupancies(
              id, cell_id, tracking_no, opened_at, closed_at, status, last_event_id, close_reason
            ) VALUES (?,?,?,?,NULL,'occupied',?,NULL)
            """,
            (oid, ev["cell_id"], ev["tracking_no"], ev["event_ts"], ev["id"]),
        )
        conn.execute("UPDATE cells SET status='occupied' WHERE id=?", (ev["cell_id"],))
        conn.execute(
            """
            INSERT INTO occupancy_proofs(
              occupancy_id, cell_id, tracking_no, opened_at, closed_at, event_ids_json, created_at
            ) VALUES (?,?,?,?,NULL,?,?)
            """,
            (
                oid,
                ev["cell_id"],
                ev["tracking_no"],
                ev["event_ts"],
                json.dumps([ev["id"]], ensure_ascii=False),
                _now(),
            ),
        )
        created.append(oid)
    conn.commit()
    return {
        "fixture": CASE_FIXTURE,
        "loaded": True,
        "created_occupancies": created,
        "scan_as_of": data.get("scan_as_of"),
    }


def load_missing_edge(conn) -> dict[str, Any]:
    """装载「故意缺边」的历史夹具：按回收关账但没有 reclaim_edges。"""
    data = _load_json(MISSING_EDGE_FIXTURE)
    if data is None:
        return {"fixture": MISSING_EDGE_FIXTURE, "loaded": False, "message": "夹具不存在"}

    created: list[str] = []
    for occ in data.get("occupancies", []):
        row = conn.execute(
            "SELECT 1 FROM occupancies WHERE id=?", (occ["id"],)
        ).fetchone()
        if row:
            continue
        conn.execute(
            """
            INSERT INTO occupancies(
              id, cell_id, tracking_no, opened_at, closed_at, status, last_event_id, close_reason
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                occ["id"],
                occ["cell_id"],
                occ["tracking_no"],
                occ["opened_at"],
                occ["closed_at"],
                occ.get("status", "released"),
                occ.get("last_event_id"),
                occ.get("close_reason", "reclaim"),
            ),
        )
        created.append(occ["id"])
    for proof in data.get("proofs", []):
        row = conn.execute(
            "SELECT 1 FROM occupancy_proofs WHERE occupancy_id=? AND opened_at=?",
            (proof["occupancy_id"], proof["opened_at"]),
        ).fetchone()
        if row:
            continue
        conn.execute(
            """
            INSERT INTO occupancy_proofs(
              occupancy_id, cell_id, tracking_no, opened_at, closed_at, event_ids_json, created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                proof["occupancy_id"],
                proof["cell_id"],
                proof["tracking_no"],
                proof["opened_at"],
                proof.get("closed_at"),
                json.dumps(proof.get("event_ids", []), ensure_ascii=False),
                _now(),
            ),
        )
    conn.commit()
    return {"fixture": MISSING_EDGE_FIXTURE, "loaded": True, "created_occupancies": created}


def load(name: str, conn) -> dict[str, Any]:
    if name == "case":
        return load_reclaim_case(conn)
    if name == "missing-edge":
        return load_missing_edge(conn)
    return {"fixture": name, "loaded": False, "message": "未知夹具"}


def default_scan_as_of() -> str | None:
    data = _load_json(CASE_FIXTURE)
    return data.get("scan_as_of") if data else None
