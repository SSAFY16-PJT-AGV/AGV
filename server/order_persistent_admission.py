"""Durable FIFO admission for the explicitly isolated order simulation.

The journal owns idempotency and request IDs. The dispatcher must perform a
durable claim immediately before its first side effect. On restart, only
unclaimed queued requests may be considered for execution again.
"""
import threading

from server.order_demo_admission import AdmissionConflict, AdmissionStopped


class PersistentAdmission:
    def __init__(self, store, dispatch, stop_event, *, wait_seconds=.25):
        if not callable(dispatch) or not isinstance(stop_event, threading.Event):
            raise ValueError('durable dispatcher and stop event required')
        self.store, self.dispatch, self.stop_event = store, dispatch, stop_event
        self.wait_seconds = wait_seconds
        self._lock = threading.RLock()
        self._events = {}
        self._worker = None
        self._stopped = False

    @property
    def can_accept(self):
        with self._lock:
            return not self._stopped and not self.stop_event.is_set()

    def _start_locked(self):
        if self._worker is None and not self._stopped and not self.stop_event.is_set():
            self._worker = threading.Thread(target=self._drain, name='durable-order-admission', daemon=True)
            self._worker.start()

    def resume(self):
        """Resume only queued entries; the dispatcher waits for fresh UE proof."""
        with self._lock:
            self._start_locked()

    def submit_product(self, product_id, request_key=None):
        with self._lock:
            if self._stopped or self.stop_event.is_set():
                raise AdmissionStopped('admission is shut down')
            try:
                order = self.store.submit(product_id, request_key)
            except ValueError as exc:
                if 'request key belongs' in str(exc):
                    raise AdmissionConflict('request key already belongs to a different request') from exc
                raise
            request_id = order['request_id']
            event = self._events.setdefault(request_id, threading.Event())
            if order['state'] == 'queued':
                self._start_locked()
        event.wait(self.wait_seconds)
        state = self.get(request_id)
        if state['status'] == 'completed':
            return dict(state['result'], request_id=request_id)
        if state['status'] == 'unknown':
            return {'status': 'unknown', 'request_id': request_id}
        # Failed requests are reported by the existing request-status endpoint.
        return {'status': 'pending', 'request_id': request_id}

    def get(self, request_id):
        order = self.store.get_order(request_id)
        state = order['state']
        if state in ('active', 'completed') and isinstance(order['result'], dict):
            return {'status': 'completed', 'request_id': request_id,
                    'result': dict(order['result'], request_id=request_id)}
        if state == 'unknown':
            return {'status': 'unknown', 'request_id': request_id}
        if state == 'failed':
            return {'status': 'error', 'request_id': request_id,
                    'error_type': order.get('error') or 'RuntimeError'}
        return {'status': 'pending', 'request_id': request_id}

    def get_by_key(self, request_key):
        """Observe the existing journal record without starting the worker."""
        return self.get(self.store.get_order_by_key(request_key)['request_id'])

    def _drain(self):
        while True:
            with self._lock:
                if self._stopped or self.stop_event.is_set():
                    self._worker = None
                    return
                order = self.store.peek_next()
                if order is None:
                    self._worker = None
                    return
            request_id = order['request_id']
            try:
                self.dispatch(order)
            except AdmissionStopped:
                # A request still marked queued has no side effects and will
                # be restored later. Claimed work is conservatively unknown.
                row = self.store.get_order(request_id)
                if row['state'] == 'dispatching':
                    self.store.mark_unknown(request_id, 'dispatch interrupted')
                with self._lock:
                    self._worker = None
                return
            except Exception as exc:
                row = self.store.get_order(request_id)
                if row['state'] == 'queued':
                    self.store.mark_failed(request_id, type(exc).__name__)
                elif row['state'] in ('dispatching', 'active'):
                    self.store.mark_unknown(request_id, 'dispatch outcome unavailable')
            else:
                row = self.store.get_order(request_id)
                if row['state'] == 'dispatching':
                    self.store.mark_unknown(request_id, 'dispatch returned without an outcome')
                elif row['state'] == 'queued':
                    # Dispatcher returned without claiming; do not spin or
                    # silently convert a safe pending request into a failure.
                    with self._lock:
                        self._worker = None
                    return
            with self._lock:
                event = self._events.get(request_id)
                if event is not None:
                    event.set()

    def shutdown(self):
        with self._lock:
            self._stopped = True

    def join(self, timeout=None):
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()
