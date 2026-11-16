"""Opt-in loopback test for the portable harness; never starts Unreal."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from websockets.sync.client import connect
from server.tools import twin_multi_test as tool


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class PortableHarnessIntegrationTest(unittest.TestCase):
    def test_actual_mqtt_to_websocket_and_interrupt_cleanup(self):
        children, received, errors = [], [], []
        ready, done = threading.Event(), threading.Event()
        real_popen = subprocess.Popen
        real_publish = tool.publish_states
        editor = Mock(pid=999999)

        def fake_or_real_popen(command, **kwargs):
            if str(command[0]) == str(editor_path):
                token = kwargs['env']['AGV_TWIN_UNREAL_TOKEN']

                def observe():
                    try:
                        with connect('ws://127.0.0.1:8000/ws/digital-twin', proxy=None,
                                     origin='http://127.0.0.1', open_timeout=5,
                                     additional_headers={'X-AGV-Twin-Token': token}) as ws:
                            ready.set()
                            deadline = time.monotonic() + 10
                            while len(set(received)) < 2 and time.monotonic() < deadline:
                                received.append(json.loads(ws.recv(timeout=5))['data']['agv_id'])
                    except Exception as exc:
                        errors.append(exc)
                        ready.set()
                    finally:
                        done.set()

                threading.Thread(target=observe, daemon=True).start()
                if not ready.wait(10):
                    raise RuntimeError('test observer did not start')
                return editor
            child = real_popen(command, **kwargs)
            children.append(child)
            return child

        def publish_until_received(client, elapsed):
            real_publish(client, elapsed)
            if done.is_set():
                raise KeyboardInterrupt
            if elapsed > 12:
                raise RuntimeError('test observer timed out')

        with tempfile.TemporaryDirectory(dir=tool.ROOT / '.venv') as folder:
            editor_path = Path(folder) / 'fake-editor.exe'
            project = Path(folder) / 'test.uproject'
            editor_path.touch()
            project.touch()
            with patch.object(tool.subprocess, 'Popen', side_effect=fake_or_real_popen), \
                 patch.object(tool, 'publish_states', side_effect=publish_until_received):
                tool.run(editor_path, project, None, os.environ['AMQTT_TEST_PATH'])

        self.assertFalse(errors, errors)
        self.assertEqual(set(received), {'AGV_01', 'AGV_02'})
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.poll() is not None for child in children))
        editor.terminate.assert_not_called()
        editor.kill.assert_not_called()

    def test_runtime_error_cleans_up_children_without_touching_editor(self):
        children = []
        real_popen = subprocess.Popen
        editor = Mock(pid=999999)

        def fake_or_real_popen(command, **kwargs):
            if str(command[0]) == str(editor_path):
                return editor
            child = real_popen(command, **kwargs)
            children.append(child)
            return child

        with tempfile.TemporaryDirectory(dir=tool.ROOT / '.venv') as folder:
            editor_path = Path(folder) / 'fake-editor.exe'
            project = Path(folder) / 'test.uproject'
            editor_path.touch()
            project.touch()
            with patch.object(tool.subprocess, 'Popen', side_effect=fake_or_real_popen), \
                 patch.object(tool, 'publish_states', side_effect=RuntimeError('injected failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                    tool.run(editor_path, project, None, os.environ['AMQTT_TEST_PATH'])
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.poll() is not None for child in children))
        editor.terminate.assert_not_called()
        editor.kill.assert_not_called()
