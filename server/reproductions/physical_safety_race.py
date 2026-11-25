"""Reproduce two unresolved physical safety windows; the assertions currently fail.

Run from the repository root with the project virtual environment::

    python -m server.reproductions.physical_safety_race

This file is intentionally outside automatic ``test*.py`` discovery. Failures
demonstrate that the safety requirements are not yet satisfied; they are not
acceptable errors to suppress in a physical deployment. The tests use a new,
test-owned SQLite database and in-memory MQTT fakes only, without a broker or
any real motor. Physical runtime startup must remain blocked independently.
"""
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.allocation_store import AllocationStore
from server.order_publisher import OrderPublisher
from server.physical_state_proof import PhysicalStateWatcher
from server.progress_publisher import ProgressPublisher
from server.recovery import RecoveryGate
from server.segment_progress import SegmentProgress
from server.segments import plan_segment
from server.tests.test_progress_publisher import FakeClient as PublisherClient
from server.tests.test_segment_progress import map_data
from sim.event_inbox import SimEventInbox
from sim.event_receiver import SimEventReceiver
from sim.tests.test_event_receiver import FakeClient as ReceiverClient


def message(kind, payload, *, qos=1):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode('utf-8')
    return SimpleNamespace(topic='agv/physical-1/' + kind, payload=payload,
                           retain=False, qos=qos, mid=42)


class SafetyWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.venv')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fleet.sqlite3'
        self.map = map_data()
        self.nodes = ['N1', 'N2', 'N3', 'N4', 'N5']
        first = plan_segment(self.map, self.nodes)
        self.next = plan_segment(self.map, self.nodes[2:])
        fingerprint = hashlib.sha256(json.dumps(self.map, sort_keys=True, ensure_ascii=True,
                                  allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.store = AllocationStore(self.path)
        self.store.initialize()
        self.store.create_task('T1')
        allocated = self.store.allocate('T1', 'physical-1', 'layout@v4', self.nodes,
                    first.released_until, first.resources, {'type': 'none'}, map_fingerprint=fingerprint)
        order = self.store.get_order('T1')
        self.store.mark_attempted('T1', order['payload'], allocated, first.resources)
        self.store.mark_broker_acked('T1', order['payload'], allocated, first.resources)
        self.gate = RecoveryGate(self.path)
        self.gate.initialize()
        token = self.gate.begin('T1', 'boot-1')
        digest = hashlib.sha256(json.dumps(json.loads(order['payload']), sort_keys=True,
            ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')).hexdigest()
        self.gate.complete('T1', 'boot-1', token, robot_id='physical-1', order_id='T1',
                           seq=1, map_id='layout@v4', stopped_at='N3', order_digest=digest, stopped=True)
        self.progress = SegmentProgress(self.store, self.map, simulation_enabled=True, recovery_gate=self.gate)
        self.progress.initialize()
        self.event = {'type': 'rear_clear', 'event_id': 'E1', 'robot_session': 'boot-1',
                      'order_id': 'T1', 'seq': 1, 'map_id': 'layout@v4', 'stopped_at': 'N3',
                      'cleared_resources': [list(x) for x in sorted(set(first.resources) - set(self.next.resources))]}
        self.watcher = PhysicalStateWatcher(['physical-1'], client=SimpleNamespace(), host='127.0.0.1',
                                            port=1, log=lambda _: None)
        # Model an already acknowledged watcher subscription and online robot;
        # there are no sockets, broker connections, or physical motors here.
        self.watcher._mqtt_started = True
        self.watcher._connected.set()
        self.watcher._online['physical-1'] = True
        self.watcher._runtime = SimpleNamespace(_tasks={'T1': ('physical-1', 'boot-1')}, gate=self.gate)
        self.watcher._sessions = {'physical-1': 'boot-1'}
        self.state = {'robot_session': 'boot-1', 'status': 'WAITING', 'order_id': 'T1',
                      'seq': 1, 'last_node': 'N3', 'released_until': 'N3', 'map_id': 'layout@v4'}
        self.watcher._on_message(None, None, message('state', self.state))
        self.watcher._proven['physical-1'] = (('T1', 1), time.monotonic())

    def test_moving_state_must_block_automatic_receiver_progress(self):
        inbox = SimEventInbox(self.path, robot_tasks={'physical-1': ('T1',)}, simulation_enabled=True)
        inbox.initialize()
        client = ReceiverClient()
        receiver = SimEventReceiver(client, self.progress, inbox=inbox, recovery_gate=self.gate,
                                    robot_tasks={'physical-1': ('T1',)},
                                    robot_sessions={'physical-1': {'T1': 'boot-1'}}, simulation_enabled=True)
        receiver._ready = True
        before = self.store.list_occupancy()
        self.watcher._on_message(None, None, message('state', dict(self.state, status='MOVING')))
        receiver._on_message(client, None, message('event', self.event))
        self.assertEqual(self.store.list_occupancy(), before,
                         'MOVING state must not allow old stop proof to transfer occupancy')

    def test_observed_offline_latch_must_block_worker_publish(self):
        self.progress.advance('physical-1', self.event)
        self.client = PublisherClient()
        publisher = OrderPublisher(self.client, enabled=True)
        runner = ProgressPublisher(self.progress, publisher, ack_timeout=1)
        started = threading.Event()
        worker = []
        original = publisher.validate

        def validate(*args, **kwargs):
            if not worker:
                def offline():
                    started.set()
                    self.watcher._on_message(None, None, message('conn', b'offline'))
                thread = threading.Thread(target=offline)
                worker.append(thread)
                thread.start()
                self.assertTrue(started.wait(2))
                deadline = time.monotonic() + 2
                while 'physical-1' not in self.watcher._invalidated:
                    if time.monotonic() > deadline:
                        self.fail('watcher did not observe offline')
                    time.sleep(.001)
            return original(*args, **kwargs)

        publisher.validate = validate
        try:
            try:
                runner.publish('T1', 2, 'boot-1')
            except Exception:
                pass
        finally:
            for thread in worker:
                thread.join(5)
                self.assertFalse(thread.is_alive(), 'gate invalidation stuck')
        self.assertEqual(self.client.publications, [],
                         'worker must not enqueue a command after offline was observed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
