"""Reject endpoint shutdown from a thread holding its own execution locks."""
import threading


def stop_forbidden(proposal_receiver):
    robot = proposal_receiver.robot
    runtime = proposal_receiver.guard.runtime
    receiver = runtime.receiver
    locks = [proposal_receiver._lifecycle_lock, proposal_receiver._connection_fence,
             robot._lock, runtime._lock]
    if receiver is not None:
        locks.append(receiver._dispatch_lock)
    if runtime.inbox is not None:
        locks.append(runtime.inbox._lock)
    if any(getattr(lock, '_is_owned', lambda: False)() for lock in locks):
        return True
    current = threading.current_thread()
    return current in (getattr(robot._client, '_thread', None),
                       getattr(proposal_receiver._client, '_thread', None))
