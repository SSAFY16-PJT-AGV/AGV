"""Bounded, process-local at-most-once admission for an isolated demo.

Admission is not a robot command, a reservation, or a durable order journal.
The caller must revalidate all live authorization inside the submitted action.
"""
import copy
import math
import threading
import uuid


class AdmissionBusy(RuntimeError):
    """No operation was started because the executor is busy or full."""


class AdmissionConflict(ValueError):
    """An idempotency key was reused with a different request."""


class AdmissionStopped(RuntimeError):
    """The executor has been stopped and cannot start new operations."""


class AdmissionOutcomeUnknown(RuntimeError):
    """An operation began and its effects cannot be determined safely."""


class OrderAdmission:
    """Run at most one operation; retain outcomes for safe local retries.

    A slow submit returns an opaque request ID. Records are bounded and never
    evicted during this process, so a retained key can never launch twice.
    `shutdown` marks in-flight work unknown without attempting to cancel it;
    `join` lets the owner wait before tearing down resources that work uses.
    No restart or cross-process idempotency guarantee is made.
    """

    def __init__(self, *, wait_seconds=.25, max_records=256):
        if (type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds)
                or not 0 <= wait_seconds <= 5):
            raise ValueError('wait_seconds must be between zero and five')
        if type(max_records) is not int or not 1 <= max_records <= 4096:
            raise ValueError('invalid admission record limit')
        self.wait_seconds = float(wait_seconds)
        self.max_records = max_records
        self._lock = threading.Lock()
        self._records = {}
        self._keys = {}
        self._active = None
        self._worker = None
        self._stopped = False

    @staticmethod
    def _identifier(value, name, limit):
        if (not isinstance(value, str) or not value or len(value) > limit
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError('{} must be a bounded nonempty string'.format(name))

    def submit(self, operation, fingerprint, request_key=None):
        if not callable(operation):
            raise ValueError('operation must be callable')
        self._identifier(fingerprint, 'fingerprint', 512)
        if request_key is not None:
            self._identifier(request_key, 'request_key', 128)
        with self._lock:
            request_id = self._keys.get(request_key) if request_key is not None else None
            if request_id is not None:
                job = self._records[request_id]
                if job['fingerprint'] != fingerprint:
                    raise AdmissionConflict('request key already belongs to a different request')
            else:
                if self._stopped:
                    raise AdmissionStopped('admission is shut down')
                if self._active is not None or len(self._records) >= self.max_records:
                    raise AdmissionBusy('admission is busy or its record limit was reached')
                request_id = uuid.uuid4().hex
                job = {'request_id': request_id, 'fingerprint': fingerprint,
                       'state': 'pending', 'event': threading.Event(),
                       'result': None, 'error': None}
                self._records[request_id] = job
                if request_key is not None:
                    self._keys[request_key] = request_id
                self._active = request_id
                worker = threading.Thread(target=self._execute, args=(job, operation),
                                          name='order-admission', daemon=True)
                self._worker = worker
                try:
                    worker.start()
                except BaseException:
                    self._active = self._worker = None
                    self._records.pop(request_id, None)
                    if request_key is not None:
                        self._keys.pop(request_key, None)
                    raise
        job['event'].wait(self.wait_seconds)
        with self._lock:
            state = job['state']
            if state == 'completed':
                return copy.deepcopy(job['result'])
            if state == 'error':
                raise job['error'].with_traceback(None)
            return {'status': state, 'request_id': request_id}

    def _execute(self, job, operation):
        try:
            result = copy.deepcopy(operation())
        except BaseException as exc:
            with self._lock:
                if job['state'] == 'pending':
                    if isinstance(exc, AdmissionOutcomeUnknown):
                        job['state'] = 'unknown'
                    else:
                        job['error'] = exc.with_traceback(None)
                        job['state'] = 'error'
                self._active = None
                job['event'].set()
            return
        with self._lock:
            if job['state'] == 'pending':
                job['result'] = result
                job['state'] = 'completed'
            self._active = None
            job['event'].set()

    def get(self, request_id):
        self._identifier(request_id, 'request_id', 128)
        with self._lock:
            job = self._records.get(request_id)
            if job is None:
                raise KeyError(request_id)
            result = {'status': job['state'], 'request_id': request_id}
            if job['state'] == 'completed':
                result['result'] = copy.deepcopy(job['result'])
            elif job['state'] == 'error':
                result['error_type'] = type(job['error']).__name__
            return result

    def shutdown(self):
        with self._lock:
            self._stopped = True
            if self._active is not None:
                job = self._records[self._active]
                if job['state'] == 'pending':
                    job['state'] = 'unknown'
                    job['event'].set()

    def join(self, timeout=None):
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()
