"""Durable development jobs and immutable approvals; execution is added in subsequent phases."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import uuid
import os
import fcntl
from .models import utcnow
from .redaction import redact_text
from .specification import WorkSpec, analyze_request


class JobConflict(ValueError):
    pass


class JobNotFound(LookupError):
    pass


class DevelopmentStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        from .permissions import private_file
        private_file(path)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS specifications(
          job_id TEXT NOT NULL REFERENCES jobs(id), version INTEGER NOT NULL,
          payload TEXT NOT NULL, digest TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0,
          actor TEXT, approved_at TEXT, PRIMARY KEY(job_id,version));
        CREATE TABLE IF NOT EXISTS job_events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES jobs(id),
          timestamp TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS frozen_spec_update BEFORE UPDATE ON specifications
          WHEN OLD.approved=1 BEGIN SELECT RAISE(ABORT,'approved specification is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS frozen_spec_delete BEFORE DELETE ON specifications
          WHEN OLD.approved=1 BEGIN SELECT RAISE(ABORT,'approved specification is immutable'); END;
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _get(self, job_id: str) -> dict:
        row = self.conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return json.loads(row[0])

    def get(self, job_id: str) -> dict:
        with self.lock:
            job = self._get(job_id)
            if version := job.get("spec_version"):
                row = self.conn.execute("SELECT * FROM specifications WHERE job_id=? AND version=?", (job_id, version)).fetchone()
                job["specification"] = {"version": row["version"], "digest": row["digest"],
                    "approved": bool(row["approved"]), "actor": row["actor"], "body": json.loads(row["payload"])}
            return job

    def _save(self, job: dict):
        job["updated_at"] = utcnow().isoformat()
        self.conn.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job), job["id"]))

    def _event(self, job_id: str, kind: str, payload: dict):
        self.conn.execute("INSERT INTO job_events(job_id,timestamp,kind,payload) VALUES(?,?,?,?)",
            (job_id, utcnow().isoformat(), kind, json.dumps(payload)))

    def create(self, prompt: str, repository: str, mode: str = "mock", *, max_attempts=10, timeout_seconds=300, mock_plan=None) -> dict:
        analysis = analyze_request(prompt)
        job = {"id": str(uuid.uuid4()), "prompt": redact_text(prompt), "repository": repository,
            "mode": mode, "status": "needs_clarification", "draft": analysis["draft"],
            "questions": analysis["questions"], "spec_version": None, "approved_digest": None,
            "created_at": utcnow().isoformat(), "updated_at": utcnow().isoformat(),
            "attempts": [], "switches": [], "verification": [], "report": None,
            "max_attempts": max_attempts, "timeout_seconds": timeout_seconds,
            "mock_plan": mock_plan or [], "risk_approved": None}
        from .routing import scrub
        job = scrub(job)
        with self.transaction():
            self.conn.execute("INSERT INTO jobs VALUES(?,?)", (job["id"], json.dumps(job)))
            self._event(job["id"], "created", {"mode": mode})
        return self.get(job["id"])

    def set_spec(self, job_id: str, spec: WorkSpec) -> dict:
        with self.transaction():
            job = self._get(job_id)
            if job["status"] not in ("needs_clarification", "awaiting_spec_approval"):
                raise JobConflict("specification cannot change after approval or cancellation")
            version = (job["spec_version"] or 0) + 1
            self.conn.execute("INSERT INTO specifications(job_id,version,payload,digest) VALUES(?,?,?,?)",
                (job_id, version, spec.canonical(), spec.digest()))
            job.update(status="awaiting_spec_approval", spec_version=version, questions=[])
            self._save(job)
            self._event(job_id, "specification_drafted", {"version": version, "digest": spec.digest()})
        return self.get(job_id)

    def approve(self, job_id: str, version: int, digest: str, actor: str) -> dict:
        with self.transaction():
            job = self._get(job_id)
            if job["status"] != "awaiting_spec_approval" or job["spec_version"] != version:
                raise JobConflict("approval is stale or job is not awaiting approval")
            row = self.conn.execute("SELECT digest FROM specifications WHERE job_id=? AND version=?", (job_id, version)).fetchone()
            if row[0] != digest:
                raise JobConflict("specification digest mismatch")
            self.conn.execute("UPDATE specifications SET approved=1,actor=?,approved_at=? WHERE job_id=? AND version=?",
                (redact_text(actor), utcnow().isoformat(), job_id, version))
            job.update(status="ready", approved_digest=digest)
            self._save(job)
            self._event(job_id, "specification_approved", {"version": version, "digest": digest, "actor": redact_text(actor)})
        return self.get(job_id)

    def events(self, job_id: str) -> list[dict]:
        with self.lock:
            self._get(job_id)
            return [{"seq": r["seq"], "timestamp": r["timestamp"], "kind": r["kind"], "data": json.loads(r["payload"])}
                for r in self.conn.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY seq", (job_id,))]

    def mutate(self, job_id: str, function, *, allowed_statuses=None, event=None):
        from .routing import scrub
        with self.transaction():
            job = self._get(job_id)
            if allowed_statuses is not None and job["status"] not in allowed_statuses:
                raise JobConflict("job state does not allow this action")
            function(job)
            job = scrub(job)
            self._save(job)
            if event:
                self._event(job_id, event, {"status": job["status"]})
        return self.get(job_id)

    def list(self) -> list[dict]:
        with self.lock:
            return [self.get(r[0]) for r in self.conn.execute("SELECT id FROM jobs ORDER BY rowid DESC")]

    def close(self):
        self.conn.close()


class DevelopmentService:
    def __init__(self, data_dir: Path, allowed_repositories=(), provider_factory=None):
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._owner_fd = os.open(data_dir / "router-owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._owner_fd)
            raise JobConflict("another Router process owns this data directory") from None
        self._closed = False
        self.data_dir = data_dir
        self.provider_factory = provider_factory
        try:
            self.store = DevelopmentStore(data_dir / "development.sqlite3")
            from .runner import DevelopmentRunner
            self.runner = DevelopmentRunner(self.store, data_dir, allowed_repositories, provider_factory)
            self.runner.recover()
        except BaseException:
            if hasattr(self, "runner"):
                self.runner.close()
            if hasattr(self, "store"):
                self.store.close()
            fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
            os.close(self._owner_fd)
            raise

    def analyze(self, job_id: str, provider: str = "minimax"):
        return self.runner.analysis(job_id, lambda event: self._analyze(job_id, provider, event))

    def _analyze(self, job_id, provider, event):
        job = self.store.get(job_id)
        if job["status"] not in {"needs_clarification", "awaiting_spec_approval"}:
            raise JobConflict("analysis cannot alter an approved or cancelled job")
        if self.provider_factory is None:
            return job
        adapter = self.provider_factory.make(provider, job_id, self.data_dir / "analysis" / job_id)
        self.runner.register_adapter(job_id, adapter)
        if event.is_set():
            raise JobConflict("analysis cancelled")
        request = json.dumps({"instruction": "Return ONLY JSON {draft: {...}, questions: [...]}. Convert the request into the given specification schema. Unknown fields must remain null and be asked as questions. Never guess allowed files, verification commands, risk or acceptance. Never execute tools.",
            "request": job["prompt"], "schema": WorkSpec.model_json_schema()}, ensure_ascii=False)
        result = adapter.run(prompt=request, attempt=1, timeout_seconds=min(job["timeout_seconds"], 120), max_turns=1, thread_id=job_id)
        from .routing import classify, scrub
        if classify(result.model_dump(mode="json")) != "none":
            raise JobConflict("requirement analysis failed; draft preserved")
        try:
            body = json.loads(result.stdout)
            draft = body["draft"]
            if not isinstance(draft, dict):
                raise ValueError("draft must be an object")
        except (ValueError, KeyError, TypeError):
            raise JobConflict("analysis did not return a structured draft") from None
        try:
            spec = WorkSpec.model_validate(draft)
        except ValueError as exc:
            analysis = analyze_request(json.dumps(scrub(draft), ensure_ascii=False))
            questions = analysis["questions"] or [{"field": "validation", "question": "명세 참조·금지 규칙을 검토해 완전한 명세로 수정해 주세요."}]
            return self.store.mutate(job_id, lambda j: j.update(draft=analysis["draft"], questions=questions, status="needs_clarification"),
                allowed_statuses={"needs_clarification", "awaiting_spec_approval"}, event="requirements_analyzed")
        return self.store.set_spec(job_id, spec)

    def close(self):
        if self._closed:
            return
        self.runner.close()
        self.store.close()
        fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
        os.close(self._owner_fd)
        self._closed = True
