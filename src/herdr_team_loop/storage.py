"""Transactional state and immutable artifacts, scoped by physical workspace."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

from .errors import LoopError, require

TABLES = {
    "workspaces": ("wsp", ("path",)),
    "profiles": ("agt", ()),
    "requirements": ("req", ("workspace_id",)),
    "runs": ("run", ("workspace_id", "requirement_id")),
    "assignments": ("asg", ("workspace_id", "requirement_id", "run_id", "slot")),
    "bugs": ("bug", ("workspace_id", "requirement_id", "run_id")),
    "proposals": ("prp", ("workspace_id", "requirement_id", "run_id", "bug_id")),
    "decisions": ("dec", ("workspace_id", "requirement_id", "run_id", "bug_id", "proposal_id")),
    "run_bugs": ("rbg", ("workspace_id", "requirement_id", "run_id", "bug_id")),
    "reports": ("rpt", ("workspace_id", "requirement_id", "run_id", "assignment_id")),
    "artifacts": ("art", ("workspace_id", "requirement_id", "run_id", "path", "sha256", "kind")),
}


def default_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "herdr-team-loop-cli"


def identifier(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:20]


class Store:
    """Only this module serializes authoritative records or writes their files."""

    def __init__(self, data_dir: str | Path | None = None):
        self.root = Path(data_dir or os.environ.get("HERDR_TEAM_LOOP_DATA", "") or default_data_dir()).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(self.root / "state.sqlite3", timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._depth = 0
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        require(version <= 1, "schema_too_new", "Database was created by a newer CLI")
        if version == 0:
            self._initialize()

    def close(self) -> None:
        self.conn.close()

    def _initialize(self) -> None:
        base = "id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, data TEXT NOT NULL"
        schema = []
        for table, (_, columns) in TABLES.items():
            fields = [base]
            for name in columns:
                nullable = table == "artifacts" and name in ("requirement_id", "run_id")
                fields.append(f"{name} TEXT" + ("" if nullable else " NOT NULL"))
            if table == "workspaces":
                fields.append("UNIQUE(path)")
            if "workspace_id" in columns:
                fields.append("FOREIGN KEY(workspace_id) REFERENCES workspaces(id)")
                fields.append("UNIQUE(id, workspace_id)")
            if "requirement_id" in columns:
                fields.append("FOREIGN KEY(requirement_id, workspace_id) REFERENCES requirements(id, workspace_id)")
                fields.append("UNIQUE(id, workspace_id, requirement_id)")
            if "run_id" in columns:
                fields.append("FOREIGN KEY(run_id, workspace_id, requirement_id) REFERENCES runs(id, workspace_id, requirement_id)")
            if "bug_id" in columns:
                fields.append("FOREIGN KEY(bug_id, workspace_id, requirement_id) REFERENCES bugs(id, workspace_id, requirement_id)")
            if "proposal_id" in columns:
                fields.append("FOREIGN KEY(proposal_id, workspace_id, requirement_id) REFERENCES proposals(id, workspace_id, requirement_id)")
            if "assignment_id" in columns:
                fields.append("FOREIGN KEY(assignment_id, workspace_id, requirement_id) REFERENCES assignments(id, workspace_id, requirement_id)")
            if table == "run_bugs":
                fields.append("UNIQUE(run_id, bug_id)")
            schema.append(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(fields)});")
        schema.extend([
            "CREATE UNIQUE INDEX IF NOT EXISTS live_run ON runs(requirement_id) WHERE status NOT IN ('DONE','STOPPED');",
            "CREATE UNIQUE INDEX IF NOT EXISTS live_assignment ON assignments(run_id,slot) WHERE status IN ('DISPATCHING','STARTING','RUNNING','REPORTED','BLOCKED','UNKNOWN','STOPPING');",
            "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, workspace_id TEXT, requirement_id TEXT, run_id TEXT, assignment_id TEXT, data TEXT NOT NULL);",
            "CREATE TABLE IF NOT EXISTS resource_locks (resource TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), acquired_at REAL NOT NULL);",
            "CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);",
            "CREATE INDEX IF NOT EXISTS requirements_workspace ON requirements(workspace_id);",
            "PRAGMA user_version=1;",
        ])
        self.conn.executescript("BEGIN IMMEDIATE;\n" + "\n".join(schema) + "\nCOMMIT;")

    @contextmanager
    def transaction(self):
        depth = self._depth
        self.conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT nested_{depth}")
        self._depth += 1
        try:
            yield self
            self.conn.execute("COMMIT" if depth == 0 else f"RELEASE nested_{depth}")
        except BaseException as error:
            if depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO nested_{depth}")
                self.conn.execute(f"RELEASE nested_{depth}")
            if isinstance(error, sqlite3.IntegrityError):
                raise LoopError("conflict", "State relationship or active execution conflicts with an existing record") from error
            raise
        finally:
            self._depth -= 1

    def _table(self, table: str) -> None:
        require(table in TABLES, "invalid_table", "Unknown state collection")

    def create(self, table: str, values: dict) -> dict:
        self._table(table)
        record = dict(values)
        at = time.time()
        record.update(id=record.get("id") or identifier(TABLES[table][0]), revision=1, created_at=at, updated_at=at)
        record.setdefault("status", "OPEN")
        columns = ["id", "revision", "status", "created_at", "updated_at", "data", *TABLES[table][1]]
        arguments = [json.dumps(record, ensure_ascii=False, allow_nan=False) if col == "data" else record.get(col) for col in columns]
        with self.transaction():
            self.conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", arguments)
        return record

    def get(self, table: str, object_id: str, workspace_id: str | None = None) -> dict:
        self._table(table)
        row = self.conn.execute(f"SELECT data FROM {table} WHERE id=?", (object_id,)).fetchone()
        require(row is not None, "not_found", f"Unknown {table} ID: {object_id}")
        record = json.loads(row[0])
        if workspace_id is not None:
            require(record.get("workspace_id", record["id"] if table == "workspaces" else None) == workspace_id,
                    "scope_mismatch", "Object belongs to another workspace")
        return record

    def list(self, table: str, **filters) -> list[dict]:
        self._table(table)
        indexed = set(TABLES[table][1]) | {"id", "status", "revision"}
        sql_filters = {key: value for key, value in filters.items() if key in indexed and not isinstance(value, (list, set, tuple, dict))}
        clauses = [f"{key} IS ?" for key in sql_filters]
        sql = f"SELECT data FROM {table}" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at,id"
        rows = [json.loads(row[0]) for row in self.conn.execute(sql, list(sql_filters.values()))]
        return [row for row in rows if all(row.get(key) in value if isinstance(value, (list, set, tuple)) else row.get(key) == value
                                           for key, value in filters.items())]

    def update(self, table: str, object_id: str, changes: dict, expected_revision: int | None = None) -> dict:
        with self.transaction():
            previous = self.get(table, object_id)
            if expected_revision is not None:
                require(previous["revision"] == expected_revision, "stale_revision", "State changed; read the current revision before deciding again")
            for key in ("id", "workspace_id", "requirement_id", "run_id", "bug_id", "proposal_id", "assignment_id", "created_at"):
                require(key not in changes or changes[key] == previous.get(key), "immutable_scope", f"Cannot change {key} of an existing record")
            record = {**previous, **changes, "revision": previous["revision"] + 1, "updated_at": time.time()}
            columns = ["revision", "status", "updated_at", "data", *TABLES[table][1]]
            values = [json.dumps(record, ensure_ascii=False, allow_nan=False) if col == "data" else record.get(col) for col in columns]
            values.extend([object_id, previous["revision"]])
            result = self.conn.execute(f"UPDATE {table} SET {','.join(col+'=?' for col in columns)} WHERE id=? AND revision=?", values)
            require(result.rowcount == 1, "stale_revision", "Concurrent state update rejected")
            return record

    def event(self, kind: str, *, workspace_id: str | None = None, requirement_id: str | None = None,
              run_id: str | None = None, assignment_id: str | None = None, data: dict | None = None) -> None:
        self.conn.execute("INSERT INTO events(at,kind,workspace_id,requirement_id,run_id,assignment_id,data) VALUES (?,?,?,?,?,?,?)",
                          (time.time(), kind, workspace_id, requirement_id, run_id, assignment_id, json.dumps(data or {}, ensure_ascii=False, allow_nan=False)))

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        result = []
        for row in self.conn.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200", (run_id, after)):
            item = dict(row)
            item["data"] = json.loads(item["data"])
            result.append(item)
        return result

    def save_artifact(self, workspace_id: str, *, kind: str, content: str | bytes,
                      requirement_id: str | None = None, run_id: str | None = None, suffix: str = ".md") -> dict:
        self.get("workspaces", workspace_id)
        if requirement_id:
            self.get("requirements", requirement_id, workspace_id)
        if run_id:
            run = self.get("runs", run_id, workspace_id)
            require(run["requirement_id"] == requirement_id, "scope_mismatch", "Artifact run and requirement differ")
        require(suffix in (".md", ".json", ".txt", ".bin"), "invalid_artifact", "Unsupported artifact extension")
        require(kind.replace("_", "").isalnum(), "invalid_artifact", "Invalid artifact kind")
        artifact_id = identifier("art")
        parent = self.root / "workspaces" / workspace_id
        if requirement_id:
            parent = parent / "requirements" / requirement_id
        if run_id:
            parent = parent / "runs" / run_id
        parent = parent / kind
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(data).hexdigest()
        destination = parent / (artifact_id + suffix)
        fd, temporary = tempfile.mkstemp(prefix=".artifact-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.create("artifacts", {"id": artifact_id, "workspace_id": workspace_id,
                                          "requirement_id": requirement_id, "run_id": run_id,
                                          "kind": kind, "path": str(destination.relative_to(self.root)), "sha256": digest})

    def read_artifact(self, artifact_id: str) -> bytes:
        artifact = self.get("artifacts", artifact_id)
        path = (self.root / artifact["path"]).resolve()
        require(path.is_relative_to(self.root), "artifact_corrupt", "Artifact path escapes data root")
        try:
            data = path.read_bytes()
        except OSError as error:
            raise LoopError("artifact_missing", "Registered artifact is not readable") from error
        require(hashlib.sha256(data).hexdigest() == artifact["sha256"], "artifact_changed", "Registered artifact content changed")
        return data

    def acquire_resources(self, run_id: str, resources: list[str]) -> None:
        with self.transaction():
            for resource in sorted(set(resources)):
                row = self.conn.execute("SELECT run_id FROM resource_locks WHERE resource=?", (resource,)).fetchone()
                require(row is None or row[0] == run_id, "resource_busy", "Another run owns a required resource", resource=resource,
                        owner_run=row[0] if row else None)
                self.conn.execute("INSERT OR IGNORE INTO resource_locks VALUES (?,?,?)", (resource, run_id, time.time()))

    def release_resources(self, run_id: str) -> None:
        self.conn.execute("DELETE FROM resource_locks WHERE run_id=?", (run_id,))

    def backup(self, destination: Path) -> dict:
        destination = destination.expanduser().resolve()
        require(not destination.exists(), "destination_exists", "Backup destination already exists")
        destination.mkdir(parents=True)
        target = sqlite3.connect(destination / "state.sqlite3")
        try:
            self.conn.backup(target)
            rows = target.execute("SELECT data FROM artifacts").fetchall()
            for row in rows:
                artifact = json.loads(row[0])
                data = self.read_artifact(artifact["id"])
                path = destination / artifact["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        finally:
            target.close()
        return {"path": str(destination), "artifacts": len(rows)}
