"""Stop must fence new simulation events before waiting for reference readers."""
import json
import os
import threading
import unittest
from unittest.mock import patch

from server.recovery import _digest_json
from server.segments import plan_segment
from server.sim_runtime import SimRuntime
from server.tests.test_segment_recovery_mqtt import two_segment_map
from server.tests import test_sim_runtime_command_observations_mqtt as fixture


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class InvestigationShutdownMqttTests(unittest.TestCase):
    setUp = fixture.RuntimeCommandObservationMqttTests.setUp
    peer = fixture.RuntimeCommandObservationMqttTests.peer
    wait_for = fixture.RuntimeCommandObservationMqttTests.wait_for

    def test_stop_fences_rear_clear_while_reference_read_is_inflight(self):
        robot, orders = self.peer()
        data = two_segment_map()
        runtime = SimRuntime(self.path, data, {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reference-stop-test', investigation_notes=True)
        runtime.start()
        self.cleanup.callback(runtime.stop)
        runtime.create_task('T1')
        runtime.prepare('T1', 'N5', [{'robot_id': 'sim-r1', 'node': 'N1', 'available': True}],
                        {'type': 'load'})
        runtime.publish('T1')
        order = json.loads(orders.get(timeout=5))
        challenge = runtime.begin('T1', 'boot-1')['challenge']
        runtime.complete('T1', 'boot-1', challenge, 'sim-r1', 'T1', 1,
                         'integration@v1', 'N3', _digest_json(order), True)
        old = set(plan_segment(data, ['N1', 'N2', 'N3', 'N4', 'N5']).resources)
        new = set(plan_segment(data, ['N3', 'N4', 'N5']).resources)
        event = {'type': 'rear_clear', 'event_id': 'stop-race', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 1, 'map_id': 'integration@v1', 'stopped_at': 'N3',
                 'cleared_resources': [list(item) for item in sorted(old - new)]}
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = runtime.investigation_notes.list
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release reference read')
            return original(*args, **kwargs)
        def read():
            try:
                runtime.list_investigation_notes('T1')
            except BaseException as exc:
                errors.append(exc)
        def stop_runtime():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()
        with patch.object(runtime.investigation_notes, 'list', side_effect=delayed):
            reader, stopper = threading.Thread(target=read), threading.Thread(target=stop_runtime)
            reader.start()
            try:
                self.assertTrue(entered.wait(3))
                stopper.start()
                self.wait_for(lambda: runtime._stopped, 'stop request')
                info = robot.publish('agv/sim-r1/event', json.dumps(event), qos=1)
                info.wait_for_publish(timeout=5)
                self.assertTrue(info.is_published())
                self.wait_for(lambda: runtime.receiver._closed or not orders.empty(), 'event or receiver shutdown')
                self.assertTrue(orders.empty(), 'stop allowed a new sequence-2 command')
                self.assertTrue(runtime.workflow._stopping)
                self.assertFalse(stopped.is_set())
                self.assertIsNotNone(runtime._owner.file)
            finally:
                release.set()
                for thread in (reader, stopper):
                    if thread.ident is not None:
                        thread.join(10)
        self.assertFalse(reader.is_alive() or stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(stopped.is_set())
        self.assertIsNone(runtime._owner.file)

    def test_cleanup_failure_waits_for_reference_and_preserves_owner(self):
        runtime = SimRuntime(self.path, two_segment_map(), {'sim-r1': {'T1': 'boot-1'}},
                             port=self.port, client_id='reference-stop-failure', investigation_notes=True)
        runtime.start()
        runtime.create_task('T1')
        entered, release = threading.Event(), threading.Event()
        errors = []
        original_list, original_stop = runtime.investigation_notes.list, runtime.workflow.stop
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release reference read')
            return original_list(*args, **kwargs)
        def failing_stop():
            original_stop()
            raise RuntimeError('injected cleanup failure')
        def stop_runtime():
            try:
                runtime.stop()
            except BaseException as exc:
                errors.append(exc)
        with patch.object(runtime.investigation_notes, 'list', side_effect=delayed), \
             patch.object(runtime.workflow, 'stop', side_effect=failing_stop):
            reader = threading.Thread(target=lambda: runtime.list_investigation_notes('T1'))
            stoppers = [threading.Thread(target=stop_runtime) for _ in range(2)]
            reader.start()
            try:
                self.assertTrue(entered.wait(3))
                for stopper in stoppers:
                    stopper.start()
                self.wait_for(lambda: runtime._stopped, 'stop request')
                self.assertTrue(all(stopper.is_alive() for stopper in stoppers))
                self.assertIsNotNone(runtime._owner.file)
            finally:
                release.set()
                reader.join(10)
                for stopper in stoppers:
                    if stopper.ident is not None:
                        stopper.join(10)
        try:
            self.assertFalse(reader.is_alive() or any(stopper.is_alive() for stopper in stoppers))
            self.assertEqual(len(errors), 2)
            self.assertTrue(all(isinstance(exc, RuntimeError) for exc in errors))
            self.assertIsNotNone(runtime._owner.file)
        finally:
            runtime._owner.release()


if __name__ == '__main__':
    unittest.main()
