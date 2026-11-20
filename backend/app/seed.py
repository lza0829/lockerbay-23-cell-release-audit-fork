import json
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cabinets (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  site TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cells (
  id TEXT PRIMARY KEY,
  cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
  code TEXT NOT NULL,
  size_code TEXT NOT NULL DEFAULT 'M',
  status TEXT NOT NULL DEFAULT 'free',
  UNIQUE(cabinet_id, code)
);

CREATE TABLE IF NOT EXISTS parcel_events (
  id TEXT PRIMARY KEY,
  cabinet_id TEXT NOT NULL,
  cell_id TEXT,
  event_type TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  tracking_no TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  channel TEXT NOT NULL DEFAULT 'primary'
);

CREATE TABLE IF NOT EXISTS occupancies (
  id TEXT PRIMARY KEY,
  cell_id TEXT NOT NULL REFERENCES cells(id),
  tracking_no TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  status TEXT NOT NULL DEFAULT 'occupied',
  last_event_id TEXT,
  close_reason TEXT
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS occupancy_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occupancy_id TEXT NOT NULL,
  cell_id TEXT NOT NULL,
  tracking_no TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  event_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 回收边：与占用核证同源的不可变审计行（形态：独立 reclaim_edges 表）。
CREATE TABLE IF NOT EXISTS reclaim_edges (
  id TEXT PRIMARY KEY,
  occupancy_id TEXT NOT NULL REFERENCES occupancies(id),
  proof_id INTEGER NOT NULL REFERENCES occupancy_proofs(id),
  cell_id TEXT NOT NULL,
  tracking_no TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  status_before TEXT NOT NULL,
  status_after TEXT NOT NULL,
  closed_at TEXT NOT NULL,
  hour_bucket TEXT NOT NULL,
  snapshot_json TEXT NOT NULL DEFAULT '{}',
  overdue_hours INTEGER NOT NULL,
  deadline_at TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  decision TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(occupancy_id)
);

CREATE INDEX IF NOT EXISTS idx_reclaim_edges_decided_at ON reclaim_edges(decided_at);
CREATE INDEX IF NOT EXISTS idx_occupancies_status ON occupancies(status);
"""

# 失败闭：任何关账都要留关闭原因；回收关账必须已有同占用回收边；
# 边只能在占用开放时写入，且不可变。应用层已同事务写入，这里是库级兜底。
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_occ_close_requires_reason
BEFORE UPDATE OF status, closed_at ON occupancies
WHEN NEW.status = 'released' AND OLD.status <> 'released'
BEGIN
  SELECT CASE
    WHEN NEW.close_reason IS NULL OR NEW.close_reason = ''
    THEN RAISE(ABORT, '关闭占用必须写明 close_reason')
  END;
  SELECT CASE
    WHEN NEW.close_reason = 'reclaim'
     AND NOT EXISTS (SELECT 1 FROM reclaim_edges WHERE occupancy_id = NEW.id)
    THEN RAISE(ABORT, '回收关闭占用必须先写回收边')
  END;
END;

CREATE TRIGGER IF NOT EXISTS trg_occ_reclaim_requires_edge
BEFORE UPDATE OF close_reason ON occupancies
WHEN NEW.close_reason = 'reclaim'
BEGIN
  SELECT CASE
    WHEN NOT EXISTS (SELECT 1 FROM reclaim_edges WHERE occupancy_id = NEW.id)
    THEN RAISE(ABORT, '回收关闭占用必须先写回收边')
  END;
END;

CREATE TRIGGER IF NOT EXISTS trg_edge_requires_open_occupancy
BEFORE INSERT ON reclaim_edges
BEGIN
  SELECT CASE
    WHEN NOT EXISTS (
      SELECT 1 FROM occupancies
       WHERE id = NEW.occupancy_id AND status = 'occupied' AND closed_at IS NULL
    )
    THEN RAISE(ABORT, '回收边只能写在仍开放的占用上')
  END;
  SELECT CASE
    WHEN NOT EXISTS (
      SELECT 1 FROM occupancy_proofs
       WHERE id = NEW.proof_id
         AND occupancy_id = NEW.occupancy_id
         AND cell_id = NEW.cell_id
         AND tracking_no = NEW.tracking_no
         AND opened_at = NEW.opened_at
    )
    THEN RAISE(ABORT, '回收边必须与占用核证同源')
  END;
END;

CREATE TRIGGER IF NOT EXISTS trg_edge_no_update
BEFORE UPDATE ON reclaim_edges
BEGIN
  SELECT RAISE(ABORT, '回收边不可变');
END;

CREATE TRIGGER IF NOT EXISTS trg_edge_no_delete
BEFORE DELETE ON reclaim_edges
BEGIN
  SELECT RAISE(ABORT, '回收边不可删除');
END;

CREATE TRIGGER IF NOT EXISTS trg_edge_no_orphan
BEFORE DELETE ON occupancies
WHEN EXISTS (SELECT 1 FROM reclaim_edges WHERE occupancy_id = OLD.id)
BEGIN
  SELECT RAISE(ABORT, '有回收边的占用不可删除');
END;
"""


def overdue_hours(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='overdue_hours'").fetchone()
    return int(row["value"]) if row else 48


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(TRIGGERS)
    if not conn.execute("SELECT 1 FROM settings WHERE key='overdue_hours'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('overdue_hours', '48')")
    if conn.execute("SELECT COUNT(*) AS c FROM cabinets").fetchone()["c"] == 0:
        _seed(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """旧库补列：occupancies.close_reason（CREATE TABLE IF NOT EXISTS 不会加列）。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(occupancies)").fetchall()}
    if "close_reason" not in cols:
        conn.execute("ALTER TABLE occupancies ADD COLUMN close_reason TEXT")


def _seed(conn):
    path = FIXTURES_DIR / "seed_bay.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for c in data.get("cabinets", []):
        conn.execute(
            "INSERT INTO cabinets(id, label, site) VALUES (?,?,?)",
            (c["id"], c["label"], c["site"]),
        )
    for cell in data.get("cells", []):
        conn.execute(
            "INSERT INTO cells(id, cabinet_id, code, size_code, status) VALUES (?,?,?,?,?)",
            (cell["id"], cell["cabinet_id"], cell["code"], cell.get("size_code", "M"), "free"),
        )
    for ev in sorted(data.get("events", []), key=lambda e: (e["event_ts"], e["id"])):
        conn.execute(
            "INSERT INTO parcel_events(id, cabinet_id, cell_id, event_type, event_ts, tracking_no, payload_json, channel) VALUES (?,?,?,?,?,?,?,?)",
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
        if ev["event_type"] == "drop" and ev.get("cell_id"):
            oid = f"OCC-{ev['cell_id']}-{ev['id']}"
            conn.execute(
                "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status, last_event_id) VALUES (?,?,?,?,?,?,?)",
                (oid, ev["cell_id"], ev["tracking_no"], ev["event_ts"], None, "occupied", ev["id"]),
            )
            conn.execute("UPDATE cells SET status='occupied' WHERE id=?", (ev["cell_id"],))
            conn.execute(
                "INSERT INTO occupancy_proofs(occupancy_id, cell_id, tracking_no, opened_at, closed_at, event_ids_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    oid,
                    ev["cell_id"],
                    ev["tracking_no"],
                    ev["event_ts"],
                    None,
                    json.dumps([ev["id"]], ensure_ascii=False),
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
        elif ev["event_type"] == "pickup" and ev.get("cell_id"):
            row = conn.execute(
                "SELECT id FROM occupancies WHERE cell_id=? AND status='occupied' ORDER BY opened_at DESC LIMIT 1",
                (ev["cell_id"],),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE occupancies SET closed_at=?, status='released', close_reason='pickup', last_event_id=? WHERE id=?",
                    (ev["event_ts"], ev["id"], row["id"]),
                )
                conn.execute("UPDATE cells SET status='free' WHERE id=?", (ev["cell_id"],))
