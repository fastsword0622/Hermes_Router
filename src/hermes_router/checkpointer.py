"""LangGraph SQLite checkpointer lifecycle.

Uses the verified official pattern::

    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn)
    graph = builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": task_id}}

``check_same_thread=False`` is required because the API thread reads task state
while the sync worker thread is executing a graph run against the same database.
Writes are serialised by the saver's own locking plus the task store's write
lock, so concurrent readers never see a half-written checkpoint.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

__all__ = ["CheckpointManager"]


class CheckpointManager:
    """Owns the ``SqliteSaver`` connection and hands out ``thread_id`` configs."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
            from .permissions import private_file
            private_file(self._path)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            timeout=30.0,
        )
        # Required by the sqlite saver for cross-thread use.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._saver = self._build_saver(self._conn)

    @staticmethod
    def _build_saver(conn: sqlite3.Connection) -> Any:
        from langgraph.checkpoint.sqlite import SqliteSaver

        return SqliteSaver(conn)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def saver(self) -> Any:
        if self._closed:
            raise RuntimeError("checkpointer is closed")
        return self._saver

    @property
    def is_closed(self) -> bool:
        return self._closed

    @staticmethod
    def thread_config(task_id: str | Any) -> dict[str, dict[str, Any]]:
        """The LangGraph config shape keyed by our task id."""
        return {"configurable": {"thread_id": str(task_id)}}

    def close(self) -> None:
        """Close the saver (if it exposes a close) and the underlying connection.

        Idempotent.  Called from the FastAPI lifespan shutdown and from tests.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            saver_close = getattr(self._saver, "close", None)
            if callable(saver_close):
                try:
                    saver_close()
                except Exception:  # pragma: no cover - saver variants differ
                    pass
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass

    def __enter__(self) -> "CheckpointManager":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
