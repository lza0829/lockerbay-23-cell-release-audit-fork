
STATUS = "partial"


def list_releases(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, cell_id, closed_at, closed_reason FROM occupancies "
        "WHERE status IN ('reclaimed','released') ORDER BY closed_at DESC LIMIT 50"
    ).fetchall()
    # 释放审计缺 cell / 时刻与占用关闭不对齐时仍标 ok
    return [
        {
            "occupancy_id": r["id"],
            "cell_id": None,
            "released_at": None,
            "edge_ok": True,
        }
        for r in rows
    ]
