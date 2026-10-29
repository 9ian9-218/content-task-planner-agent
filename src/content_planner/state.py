import json
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid
from urllib.parse import quote


SCHEMA_VERSION = 3


class StateError(sqlite3.DatabaseError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def is_busy(error):
    return isinstance(error, sqlite3.OperationalError) and (
        (getattr(error, "sqlite_errorcode", 0) or 0) & 255 in [5, 6] or
        str(error) in ["database is locked", "database table is locked"])


def _schema_version(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in [0, 1, 2, SCHEMA_VERSION]:
        raise StateError("UNSUPPORTED_SCHEMA_VERSION")
    return version


def _copy_database(connection, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise StateError("DESTINATION_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".planner-backup-", suffix=".sqlite3", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    target = None
    started = time.monotonic()

    def progress(status, remaining, total):
        if time.monotonic() - started > 10:
            raise StateError("BACKUP_TIMEOUT")

    try:
        target = sqlite3.connect(temporary, timeout=5)
        connection.backup(target, pages=128, progress=progress, sleep=0.01)
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise StateError("BACKUP_INVALID")
        version = _schema_version(target)
        target.close()
        target = None
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            raise StateError("DESTINATION_EXISTS") from None
        return {"status": "completed", "schema_version": version, "path": str(destination),
                "size_bytes": destination.stat().st_size, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
    finally:
        if target:
            target.close()
        temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, path, lease_seconds=60, heartbeat_interval=10, max_active_tasks=2, read_only=False, journal_mode=None):
        self.path = str(path)
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = heartbeat_interval
        self.max_active_tasks = max_active_tasks
        self.read_only = read_only
        self.owner = None
        self.heartbeat = None
        self.heartbeat_stop = threading.Event()
        if read_only:
            source = Path(path).resolve()
            if not source.is_file():
                raise StateError("STATE_NOT_FOUND")
            self.connection = sqlite3.connect("file:" + quote(str(source)) + "?mode=ro", uri=True, timeout=5)
            try:
                _schema_version(self.connection)
            except BaseException:
                self.connection.close()
                raise
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        try:
            version = _schema_version(self.connection)
            if version == SCHEMA_VERSION:
                tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"requests", "operations", "plans", "leases", "task_controls"} <= tables:
                    raise StateError("INVALID_SCHEMA")
            if journal_mode is not None:
                current = self.connection.execute("PRAGMA journal_mode").fetchone()[0]
                if current != journal_mode and self.connection.execute("PRAGMA journal_mode=" + journal_mode).fetchone()[0] != journal_mode:
                    raise StateError("JOURNAL_MODE_UNAVAILABLE")
            if version == SCHEMA_VERSION:
                return
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                _schema_version(self.connection)
                for sql in [
                    "CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, snapshot TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, payload TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS plans (plan_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS leases (request_id TEXT PRIMARY KEY, token TEXT NOT NULL, generation INTEGER NOT NULL, expires_at REAL NOT NULL)",
                    "CREATE INDEX IF NOT EXISTS lease_expiry ON leases(expires_at)",
                    "CREATE TABLE IF NOT EXISTS task_controls (request_id TEXT PRIMARY KEY, cancel_requested INTEGER NOT NULL, requested_at REAL NOT NULL)",
                ]:
                    self.connection.execute(sql)
                self.connection.execute("PRAGMA user_version=3")
        except BaseException as exc:
            self.connection.close()
            if is_busy(exc):
                raise StateError("STATE_BUSY") from None
            raise

    def backup(self, destination):
        return _copy_database(self.connection, destination)

    @staticmethod
    def restore(source, destination):
        source = Path(source).resolve()
        if not source.is_file():
            raise StateError("BACKUP_NOT_FOUND")
        connection = sqlite3.connect("file:" + quote(str(source)) + "?mode=ro", uri=True, timeout=5)
        try:
            _schema_version(connection)
            required = {"requests", "operations", "plans"}
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required <= tables or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StateError("BACKUP_INVALID")
            return _copy_database(connection, destination)
        finally:
            connection.close()

    def get_request(self, request_id):
        row = self.connection.execute("SELECT snapshot FROM requests WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def observe(self, request_id):
        """Read the snapshot and its lease in one SQLite read snapshot."""
        if self.connection.execute("SELECT name FROM sqlite_master WHERE name='leases'").fetchone() is None:
            return self.get_request(request_id), "legacy"
        row = self.connection.execute("SELECT r.snapshot,l.expires_at FROM requests r LEFT JOIN leases l ON l.request_id=r.request_id WHERE r.request_id=?",
                                      (request_id,)).fetchone()
        if row is None:
            return None, "legacy"
        state = "legacy" if row[1] is None else ("active" if row[1] > time.time() else "expired")
        return json.loads(row[0]), state

    def lease_state(self, request_id):
        if self.connection.execute("SELECT name FROM sqlite_master WHERE name='leases'").fetchone() is None:
            return "legacy"
        row = self.connection.execute("SELECT expires_at FROM leases WHERE request_id=?", (request_id,)).fetchone()
        return "legacy" if row is None else ("active" if row[0] > time.time() else "expired")

    def cancel_requested(self, request_id):
        if self.connection.execute("SELECT name FROM sqlite_master WHERE name='task_controls'").fetchone() is None:
            return False
        row = self.connection.execute("SELECT cancel_requested FROM task_controls WHERE request_id=?", (request_id,)).fetchone()
        return bool(row and row[0])

    def cancel(self, request_id):
        if self.read_only:
            raise StateError("READ_ONLY")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.get_request(request_id)
            if existing is None:
                return {"request_id": request_id, "status": "not_found"}
            if existing["status"] == "completed":
                return {"request_id": request_id, "status": "too_late", "plan_id": existing["plan_id"]}
            self.connection.execute("INSERT INTO task_controls VALUES (?,1,?) ON CONFLICT(request_id) DO UPDATE SET cancel_requested=1,requested_at=excluded.requested_at",
                                    (request_id, time.time()))
        return {"request_id": request_id, "status": "accepted"}

    def assert_owner(self):
        if self.owner is None:
            raise StateError("LEASE_LOST")
        row = self.connection.execute("SELECT token,generation,expires_at FROM leases WHERE request_id=?", (self.owner[0],)).fetchone()
        if not row or tuple(row[:2]) != self.owner[1:] or row[2] <= time.time():
            raise StateError("LEASE_LOST")

    def _start_heartbeat(self):
        owner = self.owner
        self.heartbeat_stop.clear()

        def renew():
            connection = sqlite3.connect(self.path, timeout=min(0.1, self.heartbeat_interval))
            try:
                while not self.heartbeat_stop.wait(self.heartbeat_interval):
                    now = time.time()
                    try:
                        with connection:
                            changed = connection.execute("UPDATE leases SET expires_at=? WHERE request_id=? AND token=? AND generation=? AND expires_at>?",
                                (now + self.lease_seconds, *owner, now)).rowcount
                        if not changed:
                            return
                    except sqlite3.Error:
                        # Transient locks do not grant a new lease; the main writer still checks expiry.
                        continue
            finally:
                connection.close()

        self.heartbeat = threading.Thread(target=renew, name="planner-lease", daemon=True)
        self.heartbeat.start()

    def release(self):
        self.heartbeat_stop.set()
        if self.heartbeat is not None:
            self.heartbeat.join(timeout=2)
            self.heartbeat = None
        if self.owner is not None:
            try:
                with self.connection:
                    self.connection.execute("UPDATE leases SET expires_at=0 WHERE request_id=? AND token=? AND generation=?", self.owner)
            except sqlite3.Error:
                pass  # A failed release remains fenced by the existing expiry.
            self.owner = None

    def claim(self, context, resume=False):
        if self.read_only:
            raise StateError("READ_ONLY")
        # Fast rejection leaves the writer slot to already-admitted work. The same
        # capacity limit is checked again under BEGIN IMMEDIATE before any claim.
        if self.connection.execute("SELECT count(*) FROM leases WHERE expires_at>?", (time.time(),)).fetchone()[0] >= self.max_active_tasks:
            raise StateError("CAPACITY_EXCEEDED")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.get_request(context["request_id"])
            if self.cancel_requested(context["request_id"]):
                raise StateError("CANCELLED")
            if existing:
                if not resume or existing["input_fingerprint"] != context["input_fingerprint"]:
                    return False
                inflight_model = existing.get("model_inflight")
                if inflight_model and inflight_model.get("model_mode") != "mock":
                    raise StateError("OPERATION_OUTCOME_UNKNOWN")
                if existing["status"] == "running":
                    state = self.lease_state(context["request_id"])
                    if state == "active":
                        return False
                    if state == "legacy":
                        raise StateError("LEGACY_RECOVERY_REQUIRED")
                    if existing.get("model_mode") != "mock" and ("model_inflight" not in existing or existing.get("model_inflight")):
                        raise StateError("OPERATION_OUTCOME_UNKNOWN")
                    inflight = existing.get("tool_inflight")
                    if inflight:
                        if inflight.get("replay_policy") != "local_transactional":
                            raise StateError("OPERATION_OUTCOME_UNKNOWN")
                        context.setdefault("abandoned_local_attempts", []).append(dict(inflight, state="abandoned_before_commit"))
                        context["tool_inflight"] = None
                    context["recovered"] = True
            now = time.time()
            if self.connection.execute("SELECT count(*) FROM leases WHERE expires_at>?", (now,)).fetchone()[0] >= self.max_active_tasks:
                raise StateError("CAPACITY_EXCEEDED")
            previous = self.connection.execute("SELECT generation FROM leases WHERE request_id=?", (context["request_id"],)).fetchone()
            generation = previous[0] + 1 if previous else 1
            token = uuid.uuid4().hex
            context["lease_generation"] = generation
            self.connection.execute("INSERT INTO leases VALUES (?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET token=excluded.token,generation=excluded.generation,expires_at=excluded.expires_at",
                (context["request_id"], token, generation, now + self.lease_seconds))
            if existing:
                self.connection.execute("UPDATE requests SET snapshot=? WHERE request_id=?",
                                        (json.dumps(context, ensure_ascii=False), context["request_id"]))
            else:
                self.connection.execute("INSERT INTO requests VALUES (?, ?, ?)",
                                        (context["request_id"], context["input_fingerprint"], json.dumps(context, ensure_ascii=False)))
        self.owner = (context["request_id"], token, generation)
        self._start_heartbeat()
        return True

    def persist(self, context, plan=None):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.assert_owner()
            admitting_action = context.get("status") == "running" and (context.get("model_inflight") or context.get("tool_inflight"))
            if (plan is not None or admitting_action) and self.cancel_requested(context["request_id"]):
                raise StateError("CANCELLED")
            self.connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET snapshot=excluded.snapshot",
                (context["request_id"], context["input_fingerprint"], json.dumps(context, ensure_ascii=False)))
            for operation in context["operations"]:
                self.connection.execute("INSERT OR IGNORE INTO operations VALUES (?, ?, ?)",
                                        (operation["operation_id"], context["request_id"], json.dumps(operation, ensure_ascii=False)))
            if plan:
                self.connection.execute("INSERT INTO plans VALUES (?, ?, ?)",
                                        (plan["plan_id"], context["request_id"], json.dumps(plan, ensure_ascii=False)))

    def get_plan(self, plan_id):
        row = self.connection.execute("SELECT payload FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_plans(self):
        return [json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM plans ORDER BY plan_id")]

    def close(self):
        self.release()
        self.connection.close()
