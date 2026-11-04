"""In-memory task state records; no physical or communication side effects.

The meaning of failure/cancellation during physical operations is not yet
agreed. These state changes must not be used as proof that a robot stopped,
that a load was delivered, or that reservations can be released.
"""

from collections import namedtuple
import threading


class DuplicateTaskError(ValueError):
    """The same identifier already exists in this registry."""


class UnknownTaskError(ValueError):
    """The requested task does not exist in this registry."""


class InvalidTransitionError(ValueError):
    """The requested change is not allowed by this local state model."""


class StateConflictError(ValueError):
    """The task no longer has the state expected by the caller."""


TaskSnapshot = namedtuple("TaskSnapshot", ["task_id", "status"])

_NEXT = {
    "PENDING": "ASSIGNED",
    "ASSIGNED": "PICKING",
    "PICKING": "MOVING",
    "MOVING": "DELIVERING",
    "DELIVERING": "VERIFIED",
    "VERIFIED": "DONE",
}
_EXCEPTIONS = ("FAILED", "CANCELLED")


def _task_id(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("task id must be a non-empty string")
    return value


class TaskRegistry:
    """Single-process registry with atomic expected-state transitions."""

    def __init__(self):
        self._states = {}
        self._lock = threading.Lock()

    def create(self, task_id):
        task_id = _task_id(task_id)
        with self._lock:
            if task_id in self._states:
                raise DuplicateTaskError("task id already exists")
            self._states[task_id] = "PENDING"
            return TaskSnapshot(task_id, "PENDING")

    def get(self, task_id):
        task_id = _task_id(task_id)
        with self._lock:
            if task_id not in self._states:
                raise UnknownTaskError("task id not found")
            return TaskSnapshot(task_id, self._states[task_id])

    def transition(self, task_id, expected, target):
        task_id = _task_id(task_id)
        if not isinstance(expected, str) or not isinstance(target, str):
            raise InvalidTransitionError("states must be strings")
        with self._lock:
            if task_id not in self._states:
                raise UnknownTaskError("task id not found")
            current = self._states[task_id]
            if current != expected:
                raise StateConflictError("task state no longer matches expected state")
            if current not in _NEXT or (target != _NEXT[current] and target not in _EXCEPTIONS):
                raise InvalidTransitionError("transition is not allowed")
            self._states[task_id] = target
            return TaskSnapshot(task_id, target)
