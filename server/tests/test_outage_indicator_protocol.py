"""Protocol checks for the test-owned visible outage indicator."""
import unittest
from datetime import datetime

from server.tools.outage_indicator import PHASES, parse_update


class OutageIndicatorProtocolTests(unittest.TestCase):
    def sample(self, phase='disconnected', seq=1):
        return {'seq': seq, 'phase': phase, 'message': '격리 브로커 단절 감지',
                'at': datetime.now().astimezone().isoformat(timespec='seconds')}

    def test_new_status_is_shown_with_event_identity_and_time(self):
        status = self.sample()
        self.assertEqual(parse_update(status, 0), status)

    def test_polling_same_or_older_sequence_does_not_duplicate_a_transition(self):
        self.assertIsNone(parse_update(self.sample(seq=1), 1))
        self.assertIsNone(parse_update(self.sample(seq=1), 2))

    def test_unrecognized_phase_and_control_text_are_rejected(self):
        for invalid in (dict(self.sample(), phase='success'),
                        dict(self.sample(), message='bad\nline'),
                        dict(self.sample(), at=''),
                        dict(self.sample(), seq=True)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_update(invalid, 0)

    def test_only_known_fields_are_accepted(self):
        with self.assertRaises(ValueError):
            parse_update(dict(self.sample(), extra='ignored'), 0)

    def test_each_supported_phase_is_distinct(self):
        for phase in ('preparing', 'ready', 'injecting', 'disconnected', 'frozen', 'reconnecting', 'recovered', 'failed'):
            with self.subTest(phase=phase):
                self.assertEqual(parse_update(self.sample(phase), 0)['phase'], phase)

    def test_detected_disconnect_does_not_claim_robots_are_stopped(self):
        self.assertIn('단절', PHASES['disconnected'][0])
        self.assertNotIn('정지', PHASES['disconnected'][0])
        self.assertEqual(PHASES['frozen'][0], '단절 중 위치·점유 확인')

    def test_preparation_does_not_claim_a_connection(self):
        self.assertNotIn('정상 연결', PHASES['preparing'][0])

    def test_injection_label_means_preparation_before_broker_stop(self):
        self.assertEqual(PHASES['injecting'][0], '단절 주입 준비')


if __name__ == '__main__':
    unittest.main()
