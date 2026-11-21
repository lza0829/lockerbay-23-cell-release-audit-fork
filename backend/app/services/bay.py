def overview(conn):
    occupied = conn.execute(
        "SELECT COUNT(*) AS c FROM cells WHERE status='occupied'"
    ).fetchone()["c"]
    free = conn.execute(
        "SELECT COUNT(*) AS c FROM cells WHERE status='free'"
    ).fetchone()["c"]
    # 格口态与占用账对账：幽灵占用角标 / 假空闲计数。
    ghost = conn.execute(
        """
        SELECT COUNT(*) AS c FROM cells c
         WHERE c.status='occupied'
           AND NOT EXISTS (
             SELECT 1 FROM occupancies o
              WHERE o.cell_id = c.id AND o.status='occupied' AND o.closed_at IS NULL
           )
        """
    ).fetchone()["c"]
    false_free = conn.execute(
        """
        SELECT COUNT(*) AS c FROM cells c
         WHERE c.status='free'
           AND EXISTS (
             SELECT 1 FROM occupancies o
              WHERE o.cell_id = c.id AND o.status='occupied' AND o.closed_at IS NULL
           )
        """
    ).fetchone()["c"]
    return {
        "cabinets": conn.execute("SELECT COUNT(*) AS c FROM cabinets").fetchone()["c"],
        "cells": conn.execute("SELECT COUNT(*) AS c FROM cells").fetchone()["c"],
        "occupied": occupied,
        "free": free,
        "events": conn.execute("SELECT COUNT(*) AS c FROM parcel_events").fetchone()["c"],
        "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
        "open_occupancies": conn.execute(
            "SELECT COUNT(*) AS c FROM occupancies WHERE status='occupied'"
        ).fetchone()["c"],
        "cell_reconciled": ghost == 0 and false_free == 0,
        "ghost_occupied_cells": ghost,
        "false_free_cells": false_free,
    }


def cabinet_matrix(conn):
    cabinets = [dict(r) for r in conn.execute("SELECT * FROM cabinets ORDER BY id").fetchall()]
    out = []
    for cab in cabinets:
        cells = [
            dict(r)
            for r in conn.execute(
                "SELECT id, code, size_code, status FROM cells WHERE cabinet_id=? ORDER BY code",
                (cab["id"],),
            ).fetchall()
        ]
        for cell in cells:
            occ = conn.execute(
                "SELECT id, tracking_no, opened_at, status FROM occupancies WHERE cell_id=? ORDER BY opened_at DESC LIMIT 1",
                (cell["id"],),
            ).fetchone()
            open_count = conn.execute(
                "SELECT COUNT(*) AS c FROM occupancies "
                "WHERE cell_id=? AND status='occupied' AND closed_at IS NULL",
                (cell["id"],),
            ).fetchone()["c"]
            # 角标与占用账对账：occupied 却无开放占用=幽灵角标；free 却有开放占用=假空闲。
            drift = []
            if cell["status"] == "occupied" and open_count == 0:
                drift.append("cell_ghost_occupied")
            if cell["status"] == "free" and open_count > 0:
                drift.append("cell_false_free")
            cell["open_occupancy_count"] = open_count
            cell["drift"] = drift
            cell["reconciled"] = not drift
            cell["occupancy"] = dict(occ) if occ else None
        out.append({**cab, "cells": cells})
    return out


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

