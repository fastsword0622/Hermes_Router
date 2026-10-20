"""Durable store: persistence, uniqueness, restart recovery, limits."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from hermes_router.models import AttemptResult, ErrorClass, Task, TaskStatus, utcnow
from hermes_router.store import (
    DuplicateTaskId,
    SqliteTaskStore,
    TaskNotFound,
)


def make_task(**kwargs) -> Task:
    defaults: dict = {
        "prompt": "hello",
        "max_attempts": 2,
        "timeout_seconds": 30.0,
        "max_turns": 1,
    }
    defaults.update(kwargs)
    return Task(**defaults)


def test_create_and_get_roundtrip(store: SqliteTaskStore) -> None:
    task = make_task()
    store.create(task)
    loaded = store.get(task.id)
    assert loaded.id == task.id
    assert loaded.prompt == task.prompt
    assert loaded.status is TaskStatus.QUEUED


def test_task_ids_are_unique(store: SqliteTaskStore) -> None:
    ids = {make_task().id for _ in range(50)}
    assert len(ids) == 50
    assert all(isinstance(i, uuid.UUID) for i in ids)
    assert all(i.version == 4 for i in ids)


def test_duplicate_id_rejected(store: SqliteTaskStore) -> None:
    task = make_task()
    store.create(task)
    with pytest.raises(DuplicateTaskId):
        store.create(task)
    assert store.count() == 1


def test_get_missing_raises(store: SqliteTaskStore) -> None:
    with pytest.raises(TaskNotFound):
        store.get(uuid.uuid4())


def test_save_updates_state(store: SqliteTaskStore) -> None:
    task = store.create(make_task())
    task.status = TaskStatus.RUNNING
    task.attempts_used = 1
    store.save(task)
    loaded = store.get(task.id)
    assert loaded.status is TaskStatus.RUNNING
    assert loaded.attempts_used == 1


def test_attempts_persisted_and_readable(store: SqliteTaskStore) -> None:
    task = store.create(make_task())
    for n in (1, 2):
        task.attempts.append(
            AttemptResult(
                attempt=n,
                provider="mock",
                provider_id="mock",
                model_id="mock-model",
                ok=(n == 2),
                exit_code=0 if n == 2 else 1,
                stdout=f"out{n}",
                error_class=ErrorClass.NONE if n == 2 else ErrorClass.EXIT_CODE,
            )
        )
    store.save(task)

    stored = store.get_attempts(task.id)
    assert [a.attempt for a in stored] == [1, 2]
    assert stored[0].ok is False
    assert stored[1].ok is True
    # Idempotent: saving again must not duplicate rows.
    store.save(task)
    assert len(store.get_attempts(task.id)) == 2


def test_durability_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "tasks.sqlite3"
    store = SqliteTaskStore(db)
    task = store.create(make_task(prompt="survive restart"))
    task.status = TaskStatus.SUCCEEDED
    store.save(task)
    store.close()
    assert store.is_closed

    reopened = SqliteTaskStore(db)
    try:
        loaded = reopened.get(task.id)
        assert loaded.prompt == "survive restart"
        assert loaded.status is TaskStatus.SUCCEEDED
    finally:
        reopened.close()


def test_close_is_idempotent_and_blocks_use(tmp_path: Path) -> None:
    store = SqliteTaskStore(tmp_path / "t.sqlite3")
    store.close()
    store.close()
    with pytest.raises(RuntimeError):
        store.get(uuid.uuid4())


def test_context_manager_closes(tmp_path: Path) -> None:
    with SqliteTaskStore(tmp_path / "t.sqlite3") as store:
        store.create(make_task())
    assert store.is_closed


def test_list_paginates_newest_first(store: SqliteTaskStore) -> None:
    created = [store.create(make_task(prompt=f"p{i}")) for i in range(5)]
    page, total = store.list(limit=2, offset=0)
    assert total == 5
    assert len(page) == 2
    assert page[0].id == created[-1].id

    page2, _ = store.list(limit=2, offset=2)
    assert page2[0].id == created[-3].id


def test_list_filters_by_status(store: SqliteTaskStore) -> None:
    a = store.create(make_task(prompt="a"))
    b = store.create(make_task(prompt="b"))
    b.status = TaskStatus.FAILED
    store.save(b)

    queued, total = store.list(status=TaskStatus.QUEUED)
    assert total == 1
    assert queued[0].id == a.id
    failed, total = store.list(status=TaskStatus.FAILED)
    assert total == 1
    assert failed[0].id == b.id


def test_list_limit_is_clamped(store: SqliteTaskStore) -> None:
    store.create(make_task())
    page, _ = store.list(limit=10_000, offset=-5)
    assert len(page) == 1


def test_count_by_status(store: SqliteTaskStore) -> None:
    store.create(make_task())
    assert store.count() == 1
    assert store.count(TaskStatus.QUEUED) == 1
    assert store.count(TaskStatus.SUCCEEDED) == 0


def test_delete_removes_task_and_attempts(store: SqliteTaskStore) -> None:
    task = store.create(make_task())
    task.attempts.append(
        AttemptResult(attempt=1, provider="mock", provider_id="mock", model_id="m", ok=True)
    )
    store.save(task)
    store.delete(task.id)
    assert store.count() == 0
    with pytest.raises(TaskNotFound):
        store.get(task.id)


def test_find_active_returns_queued_and_running(store: SqliteTaskStore) -> None:
    q = store.create(make_task(prompt="q"))
    r = store.create(make_task(prompt="r"))
    r.status = TaskStatus.RUNNING
    store.save(r)
    done = store.create(make_task(prompt="done"))
    done.status = TaskStatus.SUCCEEDED
    store.save(done)

    active = {t.id for t in store.find_active()}
    assert active == {q.id, r.id}


def test_recovery_interrupts_without_dispatch(store: SqliteTaskStore) -> None:
    task = store.create(make_task())
    task.status = TaskStatus.RUNNING
    task.deadline_at = utcnow() + timedelta(minutes=5)
    store.save(task)

    recovered = store.interrupt_stale()
    assert [t.id for t in recovered] == [task.id]
    assert recovered[0].status is TaskStatus.INTERRUPTED
    assert recovered[0].finished_at is not None
    assert store.find_active() == []


def test_recovery_fails_task_whose_deadline_elapsed(store: SqliteTaskStore) -> None:
    task = store.create(make_task())
    task.status = TaskStatus.RUNNING
    task.deadline_at = utcnow() - timedelta(seconds=5)
    store.save(task)

    recovered = store.interrupt_stale()
    assert recovered[0].status is TaskStatus.FAILED
    assert "deadline" in (recovered[0].error_message or "")


def test_recovery_with_grace_marks_interrupted(store: SqliteTaskStore) -> None:
    task = store.create(make_task())
    task.status = TaskStatus.RUNNING
    task.deadline_at = utcnow() + timedelta(seconds=10)
    store.save(task)

    recovered = store.interrupt_stale(grace=timedelta(seconds=30))
    assert recovered[0].status is TaskStatus.INTERRUPTED


def test_recovery_is_a_noop_when_nothing_active(store: SqliteTaskStore) -> None:
    assert store.interrupt_stale() == []


def test_deadline_helpers(store: SqliteTaskStore) -> None:
    task = make_task(deadline_at=utcnow() + timedelta(seconds=10))
    assert store.deadline_elapsed(task) is False
    assert store.remaining_seconds(task) > 0
    task.deadline_at = utcnow() - timedelta(seconds=1)
    assert store.deadline_elapsed(task) is True
    task.deadline_at = None
    assert store.deadline_elapsed(task) is False
    assert store.remaining_seconds(task) is None


def test_find_expired(store: SqliteTaskStore) -> None:
    live = store.create(make_task(prompt="live"))
    live.deadline_at = utcnow() + timedelta(minutes=1)
    store.save(live)
    dead = store.create(make_task(prompt="dead"))
    dead.deadline_at = utcnow() - timedelta(minutes=1)
    store.save(dead)

    assert [t.id for t in store.find_expired()] == [dead.id]


def test_rollback_on_failed_write(store: SqliteTaskStore) -> None:
    task = store.create(make_task())
    with pytest.raises(RuntimeError):
        with store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM tasks WHERE id=?", (str(task.id),))
            raise RuntimeError("boom")
    # The aborted transaction must not have removed the row.
    assert store.get(task.id).id == task.id


def test_uses_wal_and_foreign_keys(store: SqliteTaskStore) -> None:
    with store.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_in_memory_database_supported() -> None:
    store = SqliteTaskStore(":memory:")
    try:
        task = store.create(make_task())
        assert store.get(task.id).id == task.id
    finally:
        store.close()


def test_get_accepts_string_id(store: SqliteTaskStore) -> None:
    task = store.create(make_task())
    assert store.get(str(task.id)).id == task.id


def test_attempts_for_unknown_task_is_empty(store: SqliteTaskStore) -> None:
    assert store.get_attempts(uuid.uuid4()) == []


def test_rows_are_valid_json_in_payload_column(store: SqliteTaskStore) -> None:
    import json

    task = store.create(make_task())
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload FROM tasks WHERE id=?", (str(task.id),)
        ).fetchone()
    assert json.loads(row["payload"])["id"] == str(task.id)


def test_write_lock_is_reentrant(store: SqliteTaskStore) -> None:
    # _write_txn uses an RLock; nested use from the same thread must not deadlock.
    with store._write_txn() as conn:
        with store._write_txn() as inner:
            inner.execute("SELECT 1")
        assert conn.execute("SELECT 1").fetchone()[0] == 1


@pytest.fixture
def store(tmp_path: Path):
    s = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    try:
        yield s
    finally:
        s.close()


def test_explicit_conn_close_is_safe(tmp_path: Path) -> None:
    # Double-closing a connection object must not raise.
    conn = sqlite3.connect(str(tmp_path / "x.sqlite3"))
    conn.close()
    conn.close()