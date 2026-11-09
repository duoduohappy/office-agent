"""结构化记忆（方案 4.6 memory/structured_db.py）。

SQLite 单文件存储：用户偏好、任务审计日志、工具调用统计。
配合向量库（后续阶段接入）构成双层记忆。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task TEXT NOT NULL,
    agent TEXT NOT NULL DEFAULT '',
    tool_name TEXT,
    arguments TEXT,
    ok INTEGER,
    elapsed_ms INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    name TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    department TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_log(task);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


class StructuredMemory:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---------------- 审计日志 ----------------
    def log_step(
        self,
        task: str,
        tool_name: str,
        arguments: dict[str, Any],
        ok: bool,
        elapsed_ms: int = 0,
        agent: str = "",
        detail: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (ts, task, agent, tool_name, arguments, ok, elapsed_ms, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), task, agent, tool_name, json.dumps(arguments, ensure_ascii=False), int(ok), elapsed_ms, detail),
        )
        self.conn.commit()

    def recent(self, limit: int = 20, task: str | None = None) -> list[dict[str, Any]]:
        if task:
            rows = self.conn.execute(
                "SELECT * FROM audit_log WHERE task = ? ORDER BY id DESC LIMIT ?", (task, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def tool_stats(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT tool_name, COUNT(*) AS calls, SUM(ok) AS ok_calls,"
            " AVG(elapsed_ms) AS avg_ms FROM audit_log GROUP BY tool_name ORDER BY calls DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 用户偏好 ----------------
    def set_pref(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, time.time()),
        )
        self.conn.commit()

    def get_pref(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def all_prefs(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM preferences").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---------------- 本地通讯录（方案 4.4） ----------------
    def add_contact(self, name: str, email: str, department: str = "") -> None:
        self.conn.execute(
            "INSERT INTO contacts (name, email, department, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET email = excluded.email,"
            " department = excluded.department, updated_at = excluded.updated_at",
            (name, email, department, time.time()),
        )
        self.conn.commit()

    def lookup_contact(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM contacts WHERE name = ? OR name LIKE ?", (name, f"%{name}%")
        ).fetchone()
        return dict(row) if row else None

    def resolve_group(self, group: str) -> list[dict[str, Any]]:
        """按部门解析收件人组；'全体成员' 返回全部。"""
        if group in {"全体成员", "全体", "所有人"}:
            rows = self.conn.execute("SELECT * FROM contacts ORDER BY name").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM contacts WHERE department = ? OR department LIKE ? ORDER BY name",
                (group, f"%{group}%"),
            ).fetchall()
        return [dict(r) for r in rows]

    def all_contacts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM contacts ORDER BY name").fetchall()]

    def close(self) -> None:
        self.conn.close()
