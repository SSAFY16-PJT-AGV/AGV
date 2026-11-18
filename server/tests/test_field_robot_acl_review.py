"""Offline review of a proposed robot MQTT topic matrix, never a broker ACL."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from server.field_robot_acl_review import review_robot_acl


def proposal():
    return {'robots': [
        {'robot_id': 'r1', 'principal': 'robot-1',
         'publish': ['agv/r1/state', 'agv/r1/event', 'agv/r1/conn'],
         'subscribe': ['agv/r1/order']},
        {'robot_id': 'r2', 'principal': 'robot-2',
         'publish': ['agv/r2/state', 'agv/r2/event', 'agv/r2/conn'],
         'subscribe': ['agv/r2/order']},
    ]}


class FieldRobotAclReviewTests(unittest.TestCase):
    def test_exact_separate_robot_matrix_has_no_reported_issues(self):
        self.assertEqual(review_robot_acl(proposal()), [])

    def test_missing_and_cross_robot_grants_are_reported(self):
        data = proposal()
        data['robots'][0]['publish'].remove('agv/r1/event')
        data['robots'][0]['publish'].append('agv/r2/state')
        self.assertEqual(review_robot_acl(data), [
            {'robot_id': 'r1', 'direction': 'publish', 'problem': 'missing', 'topic': 'agv/r1/event'},
            {'robot_id': 'r1', 'direction': 'publish', 'problem': 'unexpected', 'topic': 'agv/r2/state'},
        ])

    def test_broad_or_unknown_grants_are_never_accepted(self):
        for topic in ('agv/+/state', '#', 'agv/r1/#', 'agv/r1/obs', '$SYS/#'):
            with self.subTest(topic=topic):
                data = proposal()
                data['robots'][0]['publish'].append(topic)
                self.assertTrue(review_robot_acl(data))

    def test_shared_principal_and_duplicate_robot_ids_are_rejected(self):
        for key in ('robot_id', 'principal'):
            data = proposal()
            data['robots'][1][key] = data['robots'][0][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                review_robot_acl(data)

    def test_malformed_identifiers_permissions_and_unknown_fields_are_rejected(self):
        bad_values = ['', 'r/1', 'r+1', 'r#1', 'r\n1', ' r1', 'r1 ', '한글', 'x' * 129]
        for key in ('robot_id', 'principal'):
            for value in bad_values:
                data = proposal()
                data['robots'][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    review_robot_acl(data)
        for value in (None, 'agv/r1/state', ['agv/r1/state', 'agv/r1/state'], ['x\nacl'], [42]):
            data = proposal()
            data['robots'][0]['publish'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                review_robot_acl(data)
        data = proposal()
        data['robots'][0]['password'] = 'secret-not-accepted'
        with self.assertRaises(ValueError):
            review_robot_acl(data)

    def test_cli_reads_only_manifest_and_exits_nonzero_on_excess_grant(self):
        data = proposal()
        data['robots'][1]['subscribe'].append('agv/r1/order')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'proposal.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            result = subprocess.run([sys.executable, '-m', 'server.field_robot_acl_review', str(path)],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        result_data = json.loads(result.stdout)
        self.assertFalse(result_data['matches_proposal'])
        self.assertEqual(result_data['issues'][0]['topic'], 'agv/r1/order')

    def test_cli_rejects_duplicate_json_keys_without_echoing_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'proposal.json'
            path.write_text('{"robots": [], "robots": "SECRET-VALUE"}', encoding='utf-8')
            result = subprocess.run([sys.executable, '-m', 'server.field_robot_acl_review', str(path)],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('SECRET-VALUE', result.stderr + result.stdout)


if __name__ == '__main__':
    unittest.main()
