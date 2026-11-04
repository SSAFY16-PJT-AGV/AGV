"""SQLite-backed task states, independent of MQTT and robot actuation.

Schema initialization is explicit. SQLite rows persist across instances, but
they are not evidence of robot location, physical stop or reservation release.
"""

from contextlib import closing, contextmanager
import os
import sqlite3

from server.tasks import (
    DuplicateTaskError, InvalidTransitionError, StateConflictError,
    TaskSnapshot, UnknownTaskError, _EXCEPTIONS, _NEXT, _task_id,
)


class SqliteTaskStore:
    """Use one SQLite connection per operation; no connection is shared."""

    def __init__(self, path):
        path = os.fspath(path)
        if not isinstance(path, str) or not path or path == ":memory:":
            raise ValueError("a file-backed SQLite path is required")
        self.path = path

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5.0, isolation_level=None)

    @contextmanager
    def _write(self):
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def initialize(self):
        """Create the table if absent; does not migrate existing schemas."""
        with self._write() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tasks "
                "(task_id TEXT PRIMARY KEY NOT NULL, status TEXT NOT NULL)"
            )

    def create(self, task_id):
        task_id = _task_id(task_id)
        try:
            with self._write() as connection:
                connection.execute(
                    "INSERT INTO tasks (task_id, status) VALUES (?, ?)",
                    (task_id, "PENDING"),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateTaskError("task id already exists") from error
        return TaskSnapshot(task_id, "PENDING")

    def get(self, task_id):
        task_id = _task_id(task_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise UnknownTaskError("task id not found")
        return TaskSnapshot(task_id, row[0])

    def transition(self, task_id, expected, target):
        task_id = _task_id(task_id)
        if not isinstance(expected, str) or not isinstance(target, str):
            raise InvalidTransitionError("states must be strings")
        with self._write() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise UnknownTaskError("task id not found")
            current = row[0]
            if current != expected:
                raise StateConflictError("task state no longer matches expected state")
            if current not in _NEXT or (target != _NEXT[current] and target not in _EXCEPTIONS):
                raise InvalidTransitionError("transition is not allowed")
            connection.execute(
                "UPDATE tasks SET status = ? WHERE task_id = ?", (target, task_id)
            )
        return TaskSnapshot(task_id, target)
