"""Recovery orchestration stays fail-closed around synthetic Unreal actions."""
import threading
import unittest

from server.order_demo_actions import DemoActionSession
from server.order_demo_app import DemoRuntime
from server.tests.test_order_product_control import ProductControlTests, Workflow


class ProductPauseTests(ProductControlTests):
    def test_paused_motion_cannot_tick_until_explicitly_reconciled(self):
        self.product_order()
        original = self.engine.snapshot()
        self.assertTrue(self.engine.suspend())
        self.assertEqual(self.engine.snapshot()['status'], 'suspended')
        self.engine.tick(5)
        self.assertEqual(Workflow.created[0].ticks, 0)
        self.assertEqual(self.engine.snapshot()['pose'], original['pose'])
        self.assertTrue(self.engine.resume_after_reconnect())
        self.assertEqual(self.engine.snapshot()['status'], 'moving')
        self.engine.tick(.1)
        self.assertEqual(Workflow.created[0].ticks, 1)

    def test_uncertain_sent_action_never_resumes_or_reissues(self):
        self.product_order()
        action = self.finish_pickup()['pending_action']
        self.engine.suspend(uncertain_action_id=action['action_id'])
        self.assertEqual(self.engine.snapshot()['status'], 'unknown')
        self.assertFalse(self.engine.resume_after_reconnect())
        self.assertIsNone(self.engine.snapshot()['pending_action'])

    def test_unsent_action_remains_pending_during_pause(self):
        self.product_order()
        action = self.finish_pickup()['pending_action']
        self.engine.suspend()
        self.assertEqual(self.engine.snapshot()['pending_action'], action)
        self.assertTrue(self.engine.resume_after_reconnect())
        self.assertEqual(self.engine.snapshot()['pending_action'], action)

    def test_unreal_disconnect_during_pause_marks_order_unknown(self):
        self.product_order()
        self.engine.suspend()
        self.engine.action_unknown()
        self.assertEqual(self.engine.snapshot()['status'], 'unknown')
        self.assertFalse(self.engine.resume_after_reconnect())


class ActionPauseTests(unittest.TestCase):
    def test_unreal_disconnect_between_readiness_and_tick_prevents_motion(self):
        class Engine:
            control_ready = True
            def __init__(self):
                self.ticks = 0
            def snapshot(self):
                return {'status': 'moving'}
            def tick(self, dt):
                self.ticks += 1
                return self.snapshot()
        engine = Engine()
        demo = DemoRuntime(engine, 1883, [], auto_recovery=True)
        owner = object()
        self.assertTrue(demo.actions.attach(owner, True))
        demo.connected.set()
        self.assertTrue(demo._ready())
        demo.actions.invalidate_owner(owner)
        self.assertIsNone(demo._tick_if_ready(.1))
        self.assertEqual(engine.ticks, 0)

    def test_disconnect_completes_after_inflight_tick_and_blocks_next_tick(self):
        entered, release = threading.Event(), threading.Event()
        class Engine:
            control_ready = True
            def __init__(self):
                self.ticks = 0
            def snapshot(self):
                return {'status': 'moving'}
            def tick(self, dt):
                self.ticks += 1
                entered.set()
                release.wait(3)
                return self.snapshot()
        engine = Engine()
        demo = DemoRuntime(engine, 1883, [], auto_recovery=True)
        owner = object()
        self.assertTrue(demo.actions.attach(owner, True))
        demo.connected.set()
        ticking = threading.Thread(target=lambda: demo._tick_if_ready(.1))
        ticking.start()
        self.assertTrue(entered.wait(1))
        done = threading.Event()
        disconnect = threading.Thread(target=lambda: (demo.actions.invalidate_owner(owner), done.set()))
        disconnect.start()
        try:
            self.assertFalse(done.wait(.1), 'disconnect must fence an in-flight tick')
            self.assertFalse(demo.actions.available_fast(), 'disconnect signal must be immediate')
        finally:
            release.set()
            ticking.join(2)
            disconnect.join(2)
        self.assertTrue(done.is_set())
        self.assertIsNone(demo._tick_if_ready(.1))
        self.assertEqual(engine.ticks, 1)

    def test_broker_ready_before_unreal_does_not_authorize_motion_or_orders(self):
        class Engine:
            control_ready = True
            def snapshot(self):
                return {'status': 'idle', 'accepting_orders': True}
            def tick(self, dt):
                raise AssertionError('cannot tick without authenticated Unreal owner')
        demo = DemoRuntime(Engine(), 1883, [], auto_recovery=True)
        demo.connected.set()
        demo._refresh_waiting_snapshot()
        state = demo.snapshot()
        self.assertTrue(state['connected'])
        self.assertFalse(state['unreal_connected'])
        self.assertFalse(state['accepting_orders'])
        self.assertFalse(demo._ready())

    def test_transport_loss_reports_only_sent_actions_as_uncertain(self):
        class Engine:
            def __init__(self):
                self.seen = []
            def snapshot(self):
                return {'pending_action': {'order_id': 'o', 'action_id': 'a', 'agv_id': 'AGV_01',
                                           'action': 'grab'}, 'pose': {'x': 0, 'y': 0}}
            def action_unknown(self):
                raise AssertionError('transport pause must not erase unrelated moving jobs')
            def suspend(self, uncertain_action_id=None):
                self.seen.append(uncertain_action_id)
        ready = [True]
        engine = Engine()
        session = DemoActionSession(engine, threading.RLock(), settle_seconds=0,
                                    can_dispatch=lambda: ready[0], pause_on_unavailable=True)
        owner = object()
        self.assertTrue(session.attach(owner, True))
        self.assertIsNotNone(session.poll(owner, 1))
        ready[0] = False
        self.assertIsNone(session.poll(owner, 2))
        self.assertEqual(engine.seen, ['a'])

    def test_demo_freezes_before_attempting_reconnect(self):
        class Engine:
            control_ready = True
            def __init__(self):
                self.uncertain = []
            def snapshot(self):
                return {'status': 'moving'}
            def suspend(self, action_id=None):
                self.uncertain.append(action_id)
                return True
        engine = Engine()
        demo = DemoRuntime(engine, 1883, [], auto_recovery=True)
        self.assertTrue(demo.actions.attach(object(), True))
        demo.connected.set()
        demo.actions.action_id, demo.actions.sent_at = 'sent-id', 1
        demo._enter_recovery()
        self.assertEqual(engine.uncertain, ['sent-id'])
        self.assertFalse(demo._ready())

    def test_shutdown_waits_for_reconnect_worker_before_transport_cleanup(self):
        class Engine:
            control_ready = True
            def snapshot(self):
                return {'status': 'idle'}
        class Client:
            def __init__(self):
                self.cleaned = threading.Event()
            def disconnect(self):
                self.cleaned.set()
            def loop_stop(self):
                pass
        demo = DemoRuntime(Engine(), 1883, [], auto_recovery=True)
        demo.client = Client()
        release = threading.Event()
        demo.thread = threading.Thread(target=lambda: release.wait(5))
        demo.thread.start()
        stopper = threading.Thread(target=demo.stop)
        stopper.start()
        try:
            self.assertTrue(demo.stop_event.wait(1))
            self.assertFalse(demo._ready())
            self.assertFalse(demo.client.cleaned.wait(.1))
        finally:
            release.set()
            stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertTrue(demo.client.cleaned.is_set())

    def test_owner_loss_while_recovering_never_resumes(self):
        class Engine:
            control_ready = True
            def __init__(self):
                self.unknown = False
            def snapshot(self):
                return {'status': 'moving'}
            def suspend(self, action_id=None):
                return True
            def action_unknown(self):
                self.unknown = True
        engine = Engine()
        demo = DemoRuntime(engine, 1883, [], auto_recovery=True)
        owner = object()
        self.assertTrue(demo.actions.attach(owner, True))
        demo._enter_recovery()
        demo.actions.invalidate_owner(owner)
        self.assertFalse(demo._try_recovery())
        self.assertTrue(engine.unknown)

    def test_initial_observer_start_without_unreal_does_not_start_recovery(self):
        class Item:
            def snapshot(self):
                return {'status': 'idle'}
        class Engine:
            control_ready = True
            engines = {'sim-r1': Item(), 'sim-r2': Item()}
            def snapshot(self):
                return {'status': 'idle'}
            def action_unknown(self):
                raise AssertionError('initial startup has no action to invalidate')
        demo = DemoRuntime(Engine(), 1883, [], auto_recovery=True)
        demo._enter_recovery()
        self.assertFalse(demo._recovering.is_set())

    def test_partial_disconnect_preserves_both_checkpoints_and_reconciles_both(self):
        class Robot:
            def __init__(self, name, ready):
                self.name, self.ready = name, ready
                self.calls = []
                self.executor = self
            def connected_recovery_checkpoint(self):
                self.calls.append('connected checkpoint')
                return self.name
            def recovery_checkpoint(self):
                self.calls.append('disconnected checkpoint')
                return self.name
            def check_recovery_checkpoint(self, value):
                return value == self.name
            def reconnect(self, *, authorization_guard):
                self.calls.append('reconnect')
                self.ready = True
            def snapshot(self):
                return {'status': 'moving', 'order': {'order_id': self.name + '-task'}}
        class Runtime:
            def __init__(self):
                self.invalidated, self.reconciled = [], []
            def status(self):
                return {'ready': True}
            def invalidate_robot(self, key):
                self.invalidated.append(key)
            def reconcile_moving(self, key, task, session, robot, checkpoint):
                self.reconciled.append(key)
        class Item:
            SESSION = 'session'
            def __init__(self, robot, runtime):
                self.robot, self.runtime = robot, runtime
            def snapshot(self):
                return {'status': 'suspended'}
        class Fleet:
            control_ready = True
            def __init__(self, engines):
                self.engines = engines
                self.on_resume = None
                self.unknown = False
            def snapshot(self):
                return {'status': 'moving'}
            def suspend(self, action_id=None):
                pass
            def resume_after_reconnect(self):
                if self.on_resume:
                    self.on_resume()
                return True
            def action_unknown(self):
                self.unknown = True
        runtime = Runtime()
        r1, r2 = Robot('sim-r1', False), Robot('sim-r2', True)
        fleet = Fleet({'sim-r1': Item(r1, runtime), 'sim-r2': Item(r2, runtime)})
        demo = DemoRuntime(fleet, 1883, [], auto_recovery=True)
        self.assertTrue(demo.actions.attach(object(), True))
        demo.connected.set()
        demo._enter_recovery()
        self.assertTrue(demo._try_recovery())
        self.assertEqual(r1.calls, ['disconnected checkpoint', 'reconnect'])
        self.assertEqual(r2.calls, ['connected checkpoint'])
        self.assertEqual(set(runtime.invalidated), {'sim-r1', 'sim-r2'})
        self.assertEqual(set(runtime.reconciled), {'sim-r1', 'sim-r2'})
        # Owner can be invalidated without acquiring the common control lock.
        # Simulate that exact race while the engines are being resumed.
        owner = demo.actions.owner
        demo._enter_recovery()
        fleet.on_resume = lambda: demo.actions.invalidate_owner(owner)
        self.assertFalse(demo._try_recovery())
        self.assertTrue(fleet.unknown)
        self.assertFalse(demo._ready())
