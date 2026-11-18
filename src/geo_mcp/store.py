"""Run history: SQLite index + per-run folder with data.json / report.md / report.html.

Every tool call and scheduled run is persisted so that agents (and humans) can
pull trends, compare runs and audit what the engines said on a given day.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  project TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  trigger TEXT,
  params_json TEXT,
  metrics_json TEXT,
  alerts_json TEXT,
  progress_json TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_project_kind ON runs(project, kind, created_at);
"""


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# The shape this store creates: %Y%m%d-%H%M%S- plus 6 hex characters.
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def valid_run_id(run_id: str) -> bool:
    """True when ``run_id`` looks like an id this store produced.

    Run ids travel in URL paths, so anything else is rejected before it can
    become a path segment.
    """
    return bool(RUN_ID_RE.match(run_id or ""))


class RunStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).expanduser()
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "runs.db"
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def run_dir(self, run_id: str) -> Path:
        # A run id reaches this class straight from a URL path parameter, so it
        # is validated against the shape this store creates before it is used
        # as a path segment.
        if not valid_run_id(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        return self.root / "runs" / run_id

    def create(self, kind: str, project: str | None, params: dict[str, Any], trigger: str = "mcp") -> str:
        run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO runs(id, kind, project, status, created_at, trigger, params_json) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (run_id, kind, project, "running", now(), trigger, json.dumps(params, default=str)))
        return run_id

    def progress(self, run_id: str, done: int, total: int, message: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE runs SET progress_json=? WHERE id=?",
                      (json.dumps({"done": done, "total": total, "message": message, "at": now()}), run_id))

    def finish(self, run_id: str, data: dict[str, Any], metrics: dict[str, Any], alerts: list[dict[str, Any]],
               report_md: str, report_html: str | None = None) -> None:
        d = self.run_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "data.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        (d / "report.md").write_text(report_md, encoding="utf-8")
        if report_html:
            (d / "report.html").write_text(report_html, encoding="utf-8")
        with self._lock, self._conn() as c:
            c.execute("UPDATE runs SET status='complete', finished_at=?, metrics_json=?, alerts_json=? WHERE id=?",
                      (now(), json.dumps(metrics), json.dumps(alerts), run_id))

    def fail(self, run_id: str, error: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE runs SET status='failed', finished_at=?, error=? WHERE id=?", (now(), error[:4000], run_id))

    def mark_orphans_failed(self, older_than_hours: float = 6) -> int:
        """Runs stuck in 'running' longer than any real run takes were interrupted (crash/restart).

        Age-based so that several processes (stdio clients, HTTP server, cron CLI) can share one data dir.
        """
        cutoff = (datetime.now(UTC) - timedelta(hours=older_than_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE runs SET status='failed', error='interrupted (process exited before completion)' "
                            "WHERE status='running' AND created_at < ?", (cutoff,))
            return cur.rowcount

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        for k in ("params_json", "metrics_json", "alerts_json", "progress_json"):
            d[k.removesuffix("_json")] = json.loads(d.pop(k) or "null")
        return d

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def list(self, project: str | None = None, kind: str | None = None, limit: int = 20,
             status: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM runs WHERE 1=1", []
        for col, val in (("project", project), ("kind", kind), ("status", status)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        q += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]  # type: ignore[misc]

    def previous(self, project: str, kind: str, before_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM runs WHERE project=? AND kind=? AND status='complete' AND id<? "
                "ORDER BY id DESC LIMIT 1", (project, kind, before_id)).fetchone())

    def load_data(self, run_id: str) -> dict[str, Any] | None:
        try:
            p = self.run_dir(run_id) / "data.json"
        except ValueError:
            return None
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def load_report(self, run_id: str, fmt: str = "md") -> str | None:
        try:
            p = self.run_dir(run_id) / f"report.{ 'html' if fmt == 'html' else 'md'}"
        except ValueError:
            return None
        return p.read_text(encoding="utf-8") if p.exists() else None
