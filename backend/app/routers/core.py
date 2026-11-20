from fastapi import APIRouter, Body, HTTPException, Query

from app.db import connect
from app.modules import reclaim_proof_edge as edge_reclaim
from app.seed import overdue_hours
from app.services import bay as bay_svc
from app.services import ingest as ingest_svc
from app.services import reclaim_fixtures

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = bay_svc.overview(conn)
        data["overdue_hours"] = overdue_hours(conn)
        return data
    finally:
        conn.close()


@router.get("/bay")
def bay_matrix():
    conn = connect()
    try:
        return bay_svc.cabinet_matrix(conn)
    finally:
        conn.close()


@router.get("/occupancies")
def list_occupancies(status: str | None = None):
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM occupancies WHERE status=? ORDER BY opened_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM occupancies ORDER BY opened_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/events")
def list_events():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, cabinet_id, cell_id, event_type, event_ts, tracking_no, channel FROM parcel_events ORDER BY event_ts, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/occupancy-proofs")
def occupancy_proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM occupancy_proofs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"overdue_hours": overdue_hours(conn)}
    finally:
        conn.close()


@router.put("/settings/overdue-hours")
def put_overdue(body: dict = Body(...)):
    hours = int(body.get("overdue_hours", 48))
    if hours < 1:
        raise HTTPException(400, "超时回收至少 1 小时")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', ?)",
            (str(hours),),
        )
        conn.commit()
        return {"overdue_hours": hours}
    finally:
        conn.close()


# ---------------------------------------------------------------- 回收边与占用核证同源


@router.get("/reclaim/candidates")
def reclaim_candidates(as_of: str | None = Query(default=None)):
    """只读：超时未取、可回收的占用；不写库。"""
    conn = connect()
    try:
        hours = overdue_hours(conn)
        at = edge_reclaim.normalize_ts(as_of)
        return {
            "as_of": at,
            "overdue_hours": hours,
            "candidates": edge_reclaim.overdue_candidates(conn, as_of=at, overdue_hours=hours),
        }
    except edge_reclaim.ReclaimError as exc:
        raise HTTPException(400, f"{exc.code}: {exc.message}")
    finally:
        conn.close()


@router.post("/reclaim/scan")
def reclaim_scan(body: dict = Body(default={})):
    """超时扫描自动回收：每笔同事务写边 + 关占用 + 核证盖关闭时刻。"""
    conn = connect()
    try:
        result = edge_reclaim.scan_and_reclaim(
            conn,
            as_of=body.get("as_of"),
            overdue_hours=body.get("overdue_hours"),
            limit=int(body.get("limit", 200)),
            dry_run=bool(body.get("dry_run", False)),
        )
        return result
    except edge_reclaim.ReclaimError as exc:
        raise HTTPException(400, f"{exc.code}: {exc.message}")
    finally:
        conn.close()


@router.post("/reclaim/occupancies/{occupancy_id}")
def reclaim_one(occupancy_id: str, body: dict = Body(default={})):
    conn = connect()
    try:
        return edge_reclaim.reclaim_occupancy(
            conn,
            occupancy_id,
            as_of=body.get("as_of"),
            overdue_hours=body.get("overdue_hours"),
        )
    except edge_reclaim.ReclaimError as exc:
        status = 404 if exc.code in {"occupancy_missing"} else 409
        raise HTTPException(status, f"{exc.code}: {exc.message}")
    finally:
        conn.close()


@router.get("/reclaim/edges")
def reclaim_edges():
    """只读核证接口：回收边列表 + 逐边校验 + 汇总；边有缺漏时 ok=false。"""
    conn = connect()
    try:
        return edge_reclaim.validate_edges(conn)
    finally:
        conn.close()


@router.get("/occupancies/{occupancy_id}/reclaim-trace")
def reclaim_trace(occupancy_id: str):
    """由关闭占用追到回收边与占用核证；无边则核证校验失败。"""
    conn = connect()
    try:
        trace = edge_reclaim.trace_occupancy(conn, occupancy_id)
        if trace is None:
            raise HTTPException(404, f"占用不存在：{occupancy_id}")
        return trace
    finally:
        conn.close()


# ---------------------------------------------------------------- 本地夹具


@router.post("/reclaim/fixtures/{name}")
def load_reclaim_fixture(name: str):
    """装载本地夹具：case（可回收投柜）或 missing-edge（故意缺边）。"""
    conn = connect()
    try:
        result = reclaim_fixtures.load(name, conn)
        if not result.get("loaded"):
            raise HTTPException(404, result.get("message", "夹具装载失败"))
        result["verify"] = edge_reclaim.validate_edges(conn)
        return result
    finally:
        conn.close()

# --- plant routes ---
from app.modules import cell_release_audit as release_audit

@router.get("/cell-release-audits")
def cell_release_audits():
    conn = connect()
    try:
        return release_audit.list_releases(conn)
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "occupancy_id" in out and "occ_id" not in out:
        out["occ_id"] = out.get("occupancy_id")
    if "cell_id" in out and "locker_cell" not in out:
        out["locker_cell"] = out.get("cell_id")
    if "tracking_no" in out and "parcel_key" not in out:
        out["parcel_key"] = out.get("tracking_no")
    if "verified" not in out:
        out["verified"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True}
    if extra:
        data.update(extra)
    return data

