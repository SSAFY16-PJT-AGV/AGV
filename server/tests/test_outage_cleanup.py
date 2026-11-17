"""Ensure an interrupted visual outage test still cleans up its own children."""
import json
import tempfile
import unittest
from pathlib import Path

from server.tools.outage_test.outage_cleanup import finalize_run


class FakeTakeover:
    def __init__(self, *, fail_disconnect=False):
        self.calls = []
        self.fail_disconnect = fail_disconnect

    def disconnect(self):
        self.calls.append('disconnect')
        if self.fail_disconnect:
            raise RuntimeError('disconnect failed')

    def loop_stop(self):
        self.calls.append('loop_stop')


class OutageCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name)
        self.events = []

    def record(self, event, **details):
        self.events.append((event, details))

    def test_one_child_stop_failure_does_not_skip_other_children(self):
        stopped = []
        def stop(process):
            stopped.append(process)
            if process == 'game':
                raise RuntimeError('game stop failed')

        result = {'passed': True}
        with self.assertRaises(RuntimeError):
            finalize_run(result, self.run_dir,
                         {'game': 'game', 'server': 'server', 'broker': 'broker', 'indicator': 'indicator'},
                         stop, self.record)

        self.assertCountEqual(stopped, ['game', 'server', 'broker', 'indicator'])
        self.assertFalse(result['passed'])
        self.assertTrue(result['cleanup_errors'])
        persisted = json.loads((self.run_dir / 'result.json').read_text(encoding='utf-8'))
        self.assertFalse(persisted['passed'])
        self.assertTrue(persisted['cleanup_errors'])

    def test_cleanup_failure_preserves_original_exception(self):
        stopped = []
        def stop(process):
            stopped.append(process)
            raise RuntimeError('stop failed')

        result = {'passed': False, 'error': 'original test failure'}
        try:
            raise ValueError('original test failure')
        except ValueError:
            with self.assertRaisesRegex(ValueError, 'original test failure'):
                try:
                    raise
                finally:
                    finalize_run(result, self.run_dir, {'game': 'game'}, stop, self.record, had_error=True)
        self.assertEqual(stopped, ['game'])
        self.assertEqual(result['error'], 'original test failure')
        self.assertTrue(result['cleanup_errors'])

    def test_takeover_loop_is_stopped_even_if_disconnect_fails(self):
        takeover = FakeTakeover(fail_disconnect=True)
        stopped = []
        result = {'passed': True}
        with self.assertRaises(RuntimeError):
            finalize_run(result, self.run_dir, {'broker': 'broker'}, stopped.append,
                         self.record, takeover=takeover)
        self.assertEqual(takeover.calls, ['disconnect', 'loop_stop'])
        self.assertEqual(stopped, ['broker'])
        self.assertFalse(result['passed'])

    def test_result_write_failure_does_not_skip_child_cleanup(self):
        bad_run_dir = self.run_dir / 'missing'
        stopped = []
        with self.assertRaises((OSError, RuntimeError)):
            finalize_run({'passed': True}, bad_run_dir,
                         {'game': 'game', 'server': 'server'}, stopped.append, self.record)
        self.assertCountEqual(stopped, ['game', 'server'])

    def test_record_failure_does_not_skip_child_cleanup(self):
        stopped = []
        def bad_record(event, **details):
            raise OSError('log write failed')

        with self.assertRaises((OSError, RuntimeError)):
            finalize_run({'passed': True}, self.run_dir,
                         {'game': 'game', 'server': 'server'}, stopped.append, bad_record)
        self.assertCountEqual(stopped, ['game', 'server'])


if __name__ == '__main__':
    unittest.main()
