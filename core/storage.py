import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from contextlib import contextmanager

from . import DATA_DIR, logger


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    release_type TEXT NOT NULL,
    summary TEXT,
    applicant TEXT NOT NULL,
    state TEXT NOT NULL,
    hotfix_reason TEXT,
    pre_check_result TEXT,
    pre_check_score REAL,
    current_phase TEXT,
    gray_traffic_percent INTEGER DEFAULT 0,
    from_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    extra TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    stage_name TEXT NOT NULL,
    approver_role TEXT NOT NULL,
    approver TEXT,
    status TEXT NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    FOREIGN KEY (release_id) REFERENCES releases(id)
);

CREATE TABLE IF NOT EXISTS state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT,
    extra TEXT,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    threshold REAL NOT NULL,
    is_breach INTEGER NOT NULL DEFAULT 0,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollbacks (
    id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    trigger_metric TEXT,
    trigger_value REAL,
    trigger_threshold REAL,
    consecutive_windows INTEGER,
    trigger_time TEXT NOT NULL,
    complete_time TEXT,
    duration_seconds INTEGER,
    from_version TEXT,
    to_version TEXT,
    impact_scope TEXT,
    actions TEXT,
    status TEXT NOT NULL,
    report_path TEXT
);

CREATE TABLE IF NOT EXISTS drills (
    id TEXT PRIMARY KEY,
    drill_type TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    duration_minutes INTEGER,
    result_summary TEXT,
    issues TEXT,
    improvements TEXT,
    report_path TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT,
    rollback_id TEXT,
    drill_id TEXT,
    channel TEXT NOT NULL,
    template_name TEXT NOT NULL,
    recipients TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    sent_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_releases_state ON releases(state);
CREATE INDEX IF NOT EXISTS idx_approvals_release ON approvals(release_id);
CREATE INDEX IF NOT EXISTS idx_state_history_entity ON state_history(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_metric_windows_release ON metric_windows(release_id);
CREATE INDEX IF NOT EXISTS idx_rollbacks_release ON rollbacks(release_id);
"""


class Storage:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: Optional[Path] = None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init(db_path)
        return cls._instance

    def _init(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (DATA_DIR / "releases.db")
        self._local = threading.local()
        self._ensure_schema()
        logger.info(f"数据存储已初始化: {self.db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False, timeout=30
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    @contextmanager
    def transaction(self):
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _ensure_schema(self):
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    @staticmethod
    def _to_json(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, default=str) if data is not None else ""

    @staticmethod
    def _from_json(text: str) -> Any:
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    def create_release(self, release_data: Dict[str, Any]) -> str:
        now = datetime.now().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO releases (
                    id, version, release_type, summary, applicant, state,
                    hotfix_reason, from_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    release_data["id"],
                    release_data["version"],
                    release_data["release_type"],
                    release_data.get("summary", ""),
                    release_data["applicant"],
                    release_data["state"],
                    release_data.get("hotfix_reason", ""),
                    release_data.get("from_version", ""),
                    now,
                    now,
                ),
            )
        return release_data["id"]

    def get_release(self, release_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM releases WHERE id = ?", (release_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        for key in ["pre_check_result", "extra", "impact_scope"]:
            if key in data:
                data[key] = self._from_json(data.get(key, ""))
        return data

    def update_release(self, release_id: str, updates: Dict[str, Any]) -> bool:
        if not updates:
            return False
        updates["updated_at"] = datetime.now().isoformat()
        fields = []
        values = []
        for k, v in updates.items():
            if k in ["pre_check_result", "extra"] and isinstance(v, (dict, list)):
                v = self._to_json(v)
            fields.append(f"{k} = ?")
            values.append(v)
        values.append(release_id)
        sql = f"UPDATE releases SET {', '.join(fields)} WHERE id = ?"
        with self.transaction() as conn:
            cur = conn.execute(sql, values)
            return cur.rowcount > 0

    def list_releases(
        self,
        state: Optional[str] = None,
        release_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM releases WHERE 1=1"
        params = []
        if state:
            sql += " AND state = ?"
            params.append(state)
        if release_type:
            sql += " AND release_type = ?"
            params.append(release_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def add_state_history(self, event: Dict[str, Any]):
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO state_history (
                    entity_type, entity_id, from_state, to_state,
                    operator, reason, extra, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.get("entity_type", "release"),
                    event["entity_id"],
                    event.get("from_state"),
                    event["to_state"],
                    event.get("operator", "system"),
                    event.get("reason", ""),
                    self._to_json(event.get("extra")),
                    event.get("timestamp", datetime.now().isoformat()),
                ),
            )

    def add_approval(self, approval_data: Dict[str, Any]) -> int:
        now = datetime.now().isoformat()
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO approvals (
                    release_id, stage_key, stage_name, approver_role,
                    approver, status, comment, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval_data["release_id"],
                    approval_data["stage_key"],
                    approval_data["stage_name"],
                    approval_data["approver_role"],
                    approval_data.get("approver", ""),
                    approval_data["status"],
                    approval_data.get("comment", ""),
                    now,
                ),
            )
            return cur.lastrowid

    def update_approval(
        self,
        approval_id: int,
        status: str,
        approver: str = "",
        comment: str = "",
    ) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE approvals
                   SET status = ?, approver = ?, comment = ?, approved_at = ?
                   WHERE id = ?""",
                (status, approver, comment, datetime.now().isoformat(), approval_id),
            )
            return cur.rowcount > 0

    def get_approvals(self, release_id: str) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM approvals WHERE release_id = ? ORDER BY id",
            (release_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_metric_window(self, data: Dict[str, Any]) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO metric_windows (
                    release_id, phase, metric_name, metric_value,
                    threshold, is_breach, window_start, window_end
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["release_id"],
                    data["phase"],
                    data["metric_name"],
                    data["metric_value"],
                    data["threshold"],
                    1 if data.get("is_breach") else 0,
                    data["window_start"],
                    data["window_end"],
                ),
            )
            return cur.lastrowid

    def get_recent_metric_windows(
        self, release_id: str, metric_name: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM metric_windows
               WHERE release_id = ? AND metric_name = ?
               ORDER BY id DESC LIMIT ?""",
            (release_id, metric_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def create_rollback(self, rollback_data: Dict[str, Any]) -> str:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO rollbacks (
                    id, release_id, trigger_metric, trigger_value,
                    trigger_threshold, consecutive_windows, trigger_time,
                    from_version, to_version, impact_scope, actions, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rollback_data["id"],
                    rollback_data["release_id"],
                    rollback_data.get("trigger_metric"),
                    rollback_data.get("trigger_value"),
                    rollback_data.get("trigger_threshold"),
                    rollback_data.get("consecutive_windows"),
                    rollback_data["trigger_time"],
                    rollback_data.get("from_version"),
                    rollback_data.get("to_version"),
                    self._to_json(rollback_data.get("impact_scope")),
                    self._to_json(rollback_data.get("actions", [])),
                    rollback_data["status"],
                ),
            )
        return rollback_data["id"]

    def update_rollback(self, rollback_id: str, updates: Dict[str, Any]) -> bool:
        if not updates:
            return False
        fields = []
        values = []
        for k, v in updates.items():
            if k in ["impact_scope", "actions"] and isinstance(v, (dict, list)):
                v = self._to_json(v)
            fields.append(f"{k} = ?")
            values.append(v)
        values.append(rollback_id)
        sql = f"UPDATE rollbacks SET {', '.join(fields)} WHERE id = ?"
        with self.transaction() as conn:
            cur = conn.execute(sql, values)
            return cur.rowcount > 0

    def get_rollback(self, rollback_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM rollbacks WHERE id = ?", (rollback_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        for key in ["impact_scope", "actions"]:
            data[key] = self._from_json(data.get(key, ""))
        return data

    def create_drill(self, drill_data: Dict[str, Any]) -> str:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO drills (
                    id, drill_type, name, status, started_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    drill_data["id"],
                    drill_data["drill_type"],
                    drill_data["name"],
                    drill_data["status"],
                    drill_data.get("started_at", datetime.now().isoformat()),
                ),
            )
        return drill_data["id"]

    def update_drill(self, drill_id: str, updates: Dict[str, Any]) -> bool:
        if not updates:
            return False
        fields = []
        values = []
        for k, v in updates.items():
            if k in ["result_summary", "issues", "improvements"] and isinstance(
                v, (dict, list)
            ):
                v = self._to_json(v)
            fields.append(f"{k} = ?")
            values.append(v)
        values.append(drill_id)
        sql = f"UPDATE drills SET {', '.join(fields)} WHERE id = ?"
        with self.transaction() as conn:
            cur = conn.execute(sql, values)
            return cur.rowcount > 0

    def log_notification(self, data: Dict[str, Any]):
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO notifications (
                    release_id, rollback_id, drill_id, channel,
                    template_name, recipients, status, error_message, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data.get("release_id"),
                    data.get("rollback_id"),
                    data.get("drill_id"),
                    data["channel"],
                    data["template_name"],
                    self._to_json(data.get("recipients", [])),
                    data["status"],
                    data.get("error_message", ""),
                    data.get("sent_at", datetime.now().isoformat()),
                ),
            )


storage = Storage()
