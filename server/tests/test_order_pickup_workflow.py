import copy
import unittest

from server.order_pickup_workflow import PickupWorkflow
from server.order_sim_map import make_order_sim_map
from server.segments import plan_segment


def map_data():
    return make_order_sim_map({'map_id': 'demo', 'coordinate_unit': 'meter',
        'nodes': [{'id': 'A', 'x': 0, 'y': 0}, {'id': 'B', 'x': 1, 'y': 0},
                  {'id': 'C', 'x': 2, 'y': 0}],
        'edges': [{'from': 'A', 'to': 'B', 'points': []},
                  {'from': 'B', 'to': 'C', 'points': []}]})


def state(status, node, seq=None, stop=None):
    value = {'status': status, 'current_node': node, 'pose': {'x': 0, 'y': 0, 'th': 0},
             'order': None, 'receipt': None}
    if seq is not None:
        value['order'] = {'order_id': 'web-pickup', 'seq': seq, 'map_id': 'demo@v1',
                          'nodes': ['A', 'B', 'C'], 'released_until': stop,
                          'action_at_end': {'type': 'load'}}
        value['receipt'] = {'order_id': 'web-pickup', 'seq': seq, 'digest': 'digest-' + str(seq),
                            'new': True, 'state': 'completed' if status == 'stopped' else 'accepted'}
    return value


class Inbox:
    def __init__(self):
        self.rows = {}
    def get(self, robot, event_id):
        return self.rows.get((robot, event_id))


class Runtime:
    def __init__(self):
        self.calls = []
        self.inbox = Inbox()
        self.is_ready = True
        self.fail = None
    def status(self):
        return {'ready': self.is_ready}
    def create_task(self, task):
        self.calls.append(('create', task))
    def prepare(self, task, pickup, candidates, action):
        self.calls.append(('prepare', task, pickup, copy.deepcopy(candidates), action))
        return {'task_id': task, 'robot_id': 'sim-r1', 'nodes': ['A', 'B', 'C'],
                'released_until': 'B', 'map_id': 'demo@v1'}
    def publish(self, task):
        self.calls.append(('publish', task))
        if self.fail:
            raise RuntimeError(self.fail)
        return {'task_id': task, 'state': 'acked'}
    def begin(self, task, session):
        self.calls.append(('begin', task, session))
        return {'challenge': 'test-challenge'}
    def complete(self, *args):
        self.calls.append(('complete',) + args)
        return {'reconciled': True}


class Robot:
    def __init__(self):
        self.ready = True
        self.states = [state('idle', 'A')]
        self.last = self.states[0]
        self.events = []
    def tick(self, dt):
        if self.states:
            self.last = self.states.pop(0)
        return copy.deepcopy(self.last)
    def publish_event(self, event):
        self.events.append(copy.deepcopy(event))


class PickupWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.map = map_data()
        self.runtime, self.robot = Runtime(), Robot()
        self.clock = [10.0]
        self.workflow = PickupWorkflow(self.runtime, self.robot, self.map,
                                       {'red': {'pickup_node': 'C'}},
                                       clock=lambda: self.clock[0], timeout_seconds=5,
                                       simulation_enabled=True)

    def test_simulation_requires_explicit_opt_in(self):
        for options in ({}, {'simulation_enabled': False}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                PickupWorkflow(self.runtime, self.robot, self.map,
                               {'red': {'pickup_node': 'C'}}, **options)

    def test_second_simulated_robot_is_explicitly_mapped(self):
        class SecondRuntime(Runtime):
            def prepare(self, task, pickup, candidates, action):
                self.calls.append(('candidate', candidates))
                return {'task_id': task, 'robot_id': 'sim-r2', 'nodes': ['A', 'B', 'C'],
                        'released_until': 'B', 'map_id': 'demo@v1'}
        runtime = SecondRuntime()
        workflow = PickupWorkflow(runtime, Robot(), self.map, {'red': {'pickup_node': 'C'}},
                                  simulation_enabled=True, robot_id='sim-r2')
        self.assertEqual(workflow.submit('red')['status'], 'awaiting_order')
        self.assertEqual(runtime.calls[1][1][0]['robot_id'], 'sim-r2')
        for robot_id in ('jetank1', 'sim-', 'sim-a/b', None):
            with self.subTest(robot_id=robot_id), self.assertRaises(ValueError):
                PickupWorkflow(runtime, Robot(), self.map, {'red': {'pickup_node': 'C'}},
                               simulation_enabled=True, robot_id=robot_id)

    def test_contested_segment_waits_then_retries_only_identical_clearance(self):
        class BlockedReceiver:
            def __init__(self, runtime):
                self.runtime, self.calls, self.free = runtime, [], False
            def retry_occupied_event(self, robot, event_id):
                self.calls.append((robot, event_id))
                if not self.free:
                    return {'status': 'occupied'}
                self.runtime.inbox.rows[(robot, event_id)] = {
                    'state': 'applied', 'result': {'status': 'advanced',
                        'task_id': 'web-pickup', 'next_seq': 2}}
                return {'status': 'advanced'}
        self.runtime.receiver = BlockedReceiver(self.runtime)
        wf = PickupWorkflow(self.runtime, self.robot, self.map, {'red': {'pickup_node': 'C'}},
                            simulation_enabled=True, clock=lambda: self.clock[0],
                            timeout_seconds=5, wait_for_reservation=True)
        wf.submit('red')
        self.robot.states = [state('stopped', 'B', 1, 'B')]
        self.assertEqual(wf.tick(0)['status'], 'waiting_progress')
        event = self.robot.events[0]
        self.runtime.inbox.rows[('sim-r1', event['event_id'])] = {'state': 'pending'}
        for _ in range(3):
            self.clock[0] += .3
            self.assertEqual(wf.tick(0)['status'], 'waiting_reservation')
        self.assertEqual(len(self.robot.events), 1)
        self.assertTrue(all(call == ('sim-r1', event['event_id']) for call in self.runtime.receiver.calls))
        self.runtime.receiver.free = True
        self.clock[0] += .3
        self.assertEqual(wf.tick(0)['status'], 'awaiting_order')
        self.assertEqual(len(self.robot.events), 1)

    def test_initial_reservation_wait_keeps_task_and_publishes_once(self):
        from server.sim_handoff_store import SimResourceBusy
        class BusyRuntime(Runtime):
            def __init__(self):
                super().__init__()
                self.blocked = True
            def prepare(self, task, pickup, candidates, action):
                if self.blocked:
                    self.calls.append(('blocked', task))
                    raise SimResourceBusy('occupied')
                return super().prepare(task, pickup, candidates, action)
        runtime = BusyRuntime()
        wf = PickupWorkflow(runtime, self.robot, self.map, {'red': {'pickup_node': 'C'}},
                            simulation_enabled=True, clock=lambda: self.clock[0],
                            timeout_seconds=5, wait_for_reservation=True)
        self.assertEqual(wf.submit('red')['status'], 'waiting_reservation')
        for _ in range(3):
            self.clock[0] += .3
            self.assertEqual(wf.tick(0)['status'], 'waiting_reservation')
        self.assertEqual([c[0] for c in runtime.calls].count('create'), 1)
        self.assertNotIn('publish', [c[0] for c in runtime.calls])
        runtime.blocked = False
        self.clock[0] += .3
        self.assertEqual(wf.tick(0)['status'], 'awaiting_order')
        self.assertEqual([c[0] for c in runtime.calls].count('create'), 1)
        self.assertEqual([c[0] for c in runtime.calls].count('publish'), 1)

    def test_contested_initial_reservation_timeout_never_publishes(self):
        from server.sim_handoff_store import SimResourceBusy
        class BusyRuntime(Runtime):
            def prepare(self, *args, **kwargs):
                raise SimResourceBusy('occupied')
        runtime = BusyRuntime()
        wf = PickupWorkflow(runtime, self.robot, self.map, {'red': {'pickup_node': 'C'}},
                            simulation_enabled=True, clock=lambda: self.clock[0],
                            timeout_seconds=5, wait_for_reservation=True)
        self.assertEqual(wf.submit('red')['status'], 'waiting_reservation')
        self.clock[0] += 6
        self.assertEqual(wf.tick(0)['status'], 'unknown')
        self.assertEqual(wf.tick(0)['status'], 'unknown')
        self.assertEqual(runtime.calls, [('create', 'web-pickup')])
        self.assertEqual(self.robot.events, [])

    def test_changed_idle_state_during_initial_wait_fails_closed(self):
        from server.sim_handoff_store import SimResourceBusy
        class BusyRuntime(Runtime):
            def prepare(self, *args, **kwargs):
                raise SimResourceBusy('occupied')
        runtime = BusyRuntime()
        wf = PickupWorkflow(runtime, self.robot, self.map, {'red': {'pickup_node': 'C'}},
                            simulation_enabled=True, clock=lambda: self.clock[0],
                            timeout_seconds=5, wait_for_reservation=True)
        self.assertEqual(wf.submit('red')['status'], 'waiting_reservation')
        self.robot.states = [state('idle', 'B')]
        self.clock[0] += .3
        self.assertEqual(wf.tick(0)['status'], 'unknown')
        self.assertNotIn('publish', [c[0] for c in runtime.calls])

    def test_handoff_wait_reuses_only_same_adapter_and_unchanged_stop(self):
        from server.sim_handoff_store import SimHandoffCoordinatorStore, SimResourceBusy
        adapter = object.__new__(SimHandoffCoordinatorStore)
        adapter._target, adapter._robot = 'web-delivery', 'sim-r1'
        class BusyHandoffRuntime(Runtime):
            def __init__(self):
                super().__init__()
                self.blocked = True
            def prepare(self, task, pickup, candidates, action, **options):
                self.calls.append(('attempt', options.get('handoff_store')))
                if self.blocked:
                    raise SimResourceBusy('occupied')
                return {'task_id': task, 'robot_id': 'sim-r1', 'nodes': ['A', 'B', 'C'],
                        'released_until': 'B', 'map_id': 'demo@v1'}
        runtime = BusyHandoffRuntime()
        previous = state('stopped', 'A', 1, 'A')
        previous['order'].update(order_id='earlier', nodes=['X', 'A'])
        previous['receipt']['order_id'] = 'earlier'
        self.robot.states = [previous]
        wf = PickupWorkflow(runtime, self.robot, self.map, {'red': {'pickup_node': 'C'}},
            simulation_enabled=True, clock=lambda: self.clock[0], timeout_seconds=5,
            task_id='web-delivery', action_at_end={'type': 'unload'},
            accept_preceding=('earlier', 'A'), prepare_options={'handoff_store': adapter},
            wait_for_reservation=True)
        self.assertEqual(wf.submit('red')['status'], 'waiting_reservation')
        self.clock[0] += .3
        self.assertEqual(wf.tick(0)['status'], 'waiting_reservation')
        runtime.blocked = False
        self.clock[0] += .3
        self.assertEqual(wf.tick(0)['status'], 'awaiting_order')
        self.assertEqual([c[0] for c in runtime.calls].count('create'), 1)
        self.assertEqual([c[0] for c in runtime.calls].count('publish'), 1)
        self.assertTrue(all(call[1] is adapter for call in runtime.calls if call[0] == 'attempt'))

    def test_submit_uses_robot_location_and_existing_coordinator(self):
        result = self.workflow.submit('red')
        self.assertEqual(result['status'], 'awaiting_order')
        self.assertEqual(self.runtime.calls[:3], [
            ('create', 'web-pickup'),
            ('prepare', 'web-pickup', 'C', [{'robot_id': 'sim-r1', 'node': 'A', 'available': True}], {'type': 'load'}),
            ('publish', 'web-pickup')])
        self.assertEqual(self.robot.events, [])
        with self.assertRaises(RuntimeError):
            self.workflow.submit('red')

    def test_releases_only_stopped_segment_and_finishes_after_applied_event(self):
        self.workflow.submit('red')
        self.robot.states = [state('stopped', 'B', 1, 'B')]
        current = self.workflow.tick(1)
        self.assertEqual(current['status'], 'waiting_progress')
        self.assertEqual(len(self.robot.events), 1)
        first = self.robot.events[0]
        initial = set(plan_segment(self.map, ['A', 'B', 'C']).resources)
        following = set(plan_segment(self.map, ['B', 'C']).resources)
        self.assertEqual({tuple(x) for x in first['cleared_resources']}, initial - following)
        self.assertEqual((first['seq'], first['stopped_at']), (1, 'B'))
        self.assertEqual(self.workflow.tick(1)['status'], 'waiting_progress')
        self.assertEqual(len(self.robot.events), 1)
        self.runtime.inbox.rows[('sim-r1', first['event_id'])] = {
            'state': 'applied', 'result': {'status': 'advanced', 'task_id': 'web-pickup', 'next_seq': 2}}
        self.robot.states = [state('stopped', 'C', 2, 'C')]
        self.assertEqual(self.workflow.tick(1)['status'], 'waiting_progress')
        final = self.robot.events[1]
        self.assertEqual((final['seq'], final['stopped_at']), (2, 'C'))
        self.assertEqual({tuple(x) for x in final['cleared_resources']}, following - {('node', 'C')})
        self.assertNotEqual(self.workflow.snapshot()['status'], 'pickup_reached')
        self.assertIsNone(self.workflow.snapshot()['finished_evidence'])
        self.runtime.inbox.rows[('sim-r1', final['event_id'])] = {
            'state': 'applied', 'result': {'status': 'finished', 'task_id': 'web-pickup', 'next_seq': None}}
        result = self.workflow.tick(0)
        self.assertEqual(result['status'], 'pickup_reached')
        self.assertEqual(result['finished_evidence'], {'task_id': 'web-pickup',
            'session': 'demo-session', 'robot_id': 'sim-r1', 'seq': 2,
            'stopped_at': 'C', 'event_id': final['event_id'], 'map_id': 'demo@v1', 'digest': 'digest-2'})
        result['finished_evidence']['stopped_at'] = 'tampered'
        self.assertEqual(self.workflow.snapshot()['finished_evidence']['stopped_at'], 'C')

    def test_puback_or_untrusted_stop_never_proves_pickup(self):
        self.workflow.submit('red')
        self.assertNotEqual(self.workflow.snapshot()['status'], 'pickup_reached')
        bad = state('stopped', 'B', 1, 'B')
        bad['receipt']['state'] = 'accepted'
        self.robot.states = [bad]
        self.assertEqual(self.workflow.tick(1)['status'], 'unknown')
        self.assertEqual(self.robot.events, [])

    def test_timeout_and_publish_failure_are_not_retried(self):
        self.runtime.fail = 'private network details'
        self.assertEqual(self.workflow.submit('red')['status'], 'unknown')
        self.assertNotIn('private', str(self.workflow.snapshot()))
        with self.assertRaises(RuntimeError):
            self.workflow.submit('red')
        self.assertEqual(len([c for c in self.runtime.calls if c[0] == 'publish']), 1)

        runtime, robot = Runtime(), Robot()
        wf = PickupWorkflow(runtime, robot, self.map, {'red': {'pickup_node': 'C'}},
                            clock=lambda: self.clock[0], timeout_seconds=5, simulation_enabled=True)
        wf.submit('red')
        self.clock[0] += 6
        self.assertEqual(wf.tick(0)['status'], 'unknown')

    def test_explicit_prior_stopped_order_can_start_distinct_delivery_leg(self):
        class DeliveryRuntime(Runtime):
            def prepare(self, task, pickup, candidates, action):
                self.calls.append(('prepare', task, pickup, copy.deepcopy(candidates), copy.deepcopy(action)))
                return {'task_id': task, 'robot_id': 'sim-r1', 'nodes': ['C', 'B', 'A'],
                        'released_until': 'B', 'map_id': 'demo@v1'}
        previous = state('stopped', 'C', 2, 'C')
        previous['order']['order_id'] = previous['receipt']['order_id'] = 'web-pickup'
        runtime, robot = DeliveryRuntime(), Robot()
        robot.states = [previous]
        action = {'type': 'unload', 'meta': {'shelf': 'red'}}
        wf = PickupWorkflow(runtime, robot, self.map, {'red': {'pickup_node': 'A'}},
                            task_id='web-delivery', session='demo-session', action_at_end=action,
                            accept_preceding=('web-pickup', 'C'), simulation_enabled=True)
        action['meta']['shelf'] = 'changed'
        self.assertEqual(wf.submit('red')['status'], 'awaiting_order')
        self.assertEqual(runtime.calls[:3], [
            ('create', 'web-delivery'),
            ('prepare', 'web-delivery', 'A', [{'robot_id': 'sim-r1', 'node': 'C', 'available': True}],
             {'type': 'unload', 'meta': {'shelf': 'red'}}),
            ('publish', 'web-delivery')])
        # A preceding receipt remains visible until the new MQTT order arrives.
        self.assertEqual(wf.tick(0)['status'], 'awaiting_order')

    def test_preceding_state_requires_exact_completed_terminal_stop(self):
        for mutation in ('wrong_task', 'not_complete', 'wrong_position', 'not_terminal'):
            previous = state('stopped', 'C', 2, 'C')
            if mutation == 'wrong_task':
                previous['order']['order_id'] = previous['receipt']['order_id'] = 'unexpected'
            elif mutation == 'not_complete':
                previous['receipt']['state'] = 'accepted'
            elif mutation == 'wrong_position':
                previous['current_node'] = 'B'
            else:
                previous['order']['nodes'].append('A')
            runtime, robot = Runtime(), Robot()
            robot.states = [previous]
            wf = PickupWorkflow(runtime, robot, self.map, {'red': {'pickup_node': 'A'}},
                                task_id='web-delivery', accept_preceding=('web-pickup', 'C'),
                                simulation_enabled=True)
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                wf.submit('red')
            self.assertEqual(runtime.calls, [])

    def test_prepare_options_pass_explicit_store_by_identity(self):
        class OptionRuntime(Runtime):
            def prepare(self, task, pickup, candidates, action, **options):
                self.options = options
                return super().prepare(task, pickup, candidates, action)
        runtime = OptionRuntime()
        adapter = object()
        wf = PickupWorkflow(runtime, Robot(), self.map, {'red': {'pickup_node': 'C'}},
                            prepare_options={'handoff_store': adapter}, simulation_enabled=True)
        self.assertEqual(wf.submit('red')['status'], 'awaiting_order')
        self.assertIs(runtime.options['handoff_store'], adapter)

    def test_invalid_elapsed_time_is_rejected_without_starting_or_mutating_task(self):
        for dt in (float('nan'), float('inf'), -1, 10 ** 1000, True):
            with self.subTest(dt=type(dt).__name__), self.assertRaises(ValueError):
                self.workflow.tick(dt)
        self.assertEqual(self.workflow.snapshot()['status'], 'idle')
        self.assertEqual(self.runtime.calls, [])


if __name__ == '__main__':
    unittest.main()
