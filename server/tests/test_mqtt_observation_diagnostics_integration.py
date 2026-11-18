"""Diagnostics against a disposable broker; no physical robot or shared MQTT."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import paho.mqtt.client as mqtt

from server.mqtt_client import MqttService
from server.tests.test_twin_broker_integration import (
    BROKER, free_port, loopback_environment, stop, wait_for_owned_broker,
)


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT')
class MqttObservationDiagnosticsIntegrationTest(unittest.TestCase):
    def test_retained_then_live_and_bad_payload_on_owned_broker(self):
        port = free_port()
        env = loopback_environment()
        env['PYTHONPATH'] = str(Path(os.environ['AMQTT_TEST_PATH']).resolve())
        python = getattr(sys, '_base_executable', sys.executable)
        with tempfile.TemporaryDirectory(prefix='mqtt-observation-diag-') as folder, ExitStack() as cleanup:
            ready_file = str(Path(folder) / 'broker.ready')
            log = cleanup.enter_context(open(os.devnull, 'w', encoding='utf-8'))
            broker = subprocess.Popen([python, '-c', BROKER, str(port), ready_file], env=env,
                                      stdout=log, stderr=log,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cleanup.callback(stop, broker)
            wait_for_owned_broker(self, broker, ready_file)
            publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='diagnostic-owned-publisher')
            connected = threading.Event()
            publisher.on_connect = lambda _c, _u, _f, reason, _p: connected.set() if not reason.is_failure else None
            publisher.connect('127.0.0.1', port, 15)
            publisher.loop_start()
            cleanup.callback(publisher.loop_stop)
            cleanup.callback(publisher.disconnect)
            self.assertTrue(connected.wait(5))
            info = publisher.publish('agv/sim-diag/state', '{"private":"saved"}', qos=1, retain=True)
            info.wait_for_publish(timeout=5)
            self.assertTrue(info.is_published())
            service = MqttService('127.0.0.1', port).start()
            cleanup.callback(service.stop)

            def await_diagnostics(predicate):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    state = service.observation_diagnostics()
                    if predicate(state):
                        return state
                    time.sleep(.02)
                self.fail('expected isolated MQTT diagnostic observation was not received')

            initial = await_diagnostics(lambda data: data['ready'] and
                data['robots'].get('sim-diag', {}).get('state', {}).get('last_delivery') == 'retained')
            self.assertGreaterEqual(initial['robots']['sim-diag']['state']['age_seconds'], 0)
            self.assertEqual(initial['robots']['sim-diag']['connection']['last_delivery'], 'not_received')
            publisher.publish('agv/sim-diag/state', b'{"private":', qos=1).wait_for_publish(timeout=5)
            bad = await_diagnostics(lambda data: data['dropped']['invalid_payload'] == 1)
            self.assertEqual(bad['robots']['sim-diag']['state']['last_delivery'], 'retained')
            publisher.publish('agv/sim-diag/conn', b'online', qos=1).wait_for_publish(timeout=5)
            publisher.publish('agv/sim-diag/state', '{"private":"latest"}', qos=1).wait_for_publish(timeout=5)
            latest = await_diagnostics(lambda data: data['robots']['sim-diag']['state']['last_delivery'] == 'non_retained'
                and data['robots']['sim-diag']['connection']['last_delivery'] == 'non_retained')
            self.assertNotIn('private', repr(latest))
            self.assertNotIn('latest', repr(latest))
            # A retained publication delivered to an existing subscription
            # carries RETAIN=0 in MQTT 3.1.1. Verify the payload changed as
            # well so this cannot pass by observing the previous message.
            publisher.publish('agv/sim-diag/state', '{"private":"retained-live"}',
                              qos=1, retain=True).wait_for_publish(timeout=5)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if service.snapshot()['robots']['sim-diag']['state'].get('private') == 'retained-live':
                    break
                time.sleep(.02)
            else:
                self.fail('retained publication was not delivered to the existing subscription')
            self.assertEqual(service.observation_diagnostics()['robots']['sim-diag']['state']['last_delivery'],
                             'non_retained')
            service.stop()
            self.assertFalse(service.observation_diagnostics()['connected'])
