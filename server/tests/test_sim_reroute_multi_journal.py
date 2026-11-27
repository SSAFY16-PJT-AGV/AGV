"""Validate immutable per-stop history for a wholly reserved virtual route."""
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

from server.segments import plan_segment
from server.sim_reroute_activation import _canonical
from sim.event_inbox import _event
from server.sim_reroute_multi_progress import validate_steps


class MultiStepJournalTest(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((Path(__file__).resolve().parents[1] / 'examples' /
                                'mat_ssafy_v3_simulated_waits.json').read_text(encoding='utf-8'))
        self.message = {'task_id': 'T1', 'transition_id': 'a' * 32, 'robot_id': 'sim-r1',
                        'robot_session': 'boot-1', 'parent_seq': 1, 'map_id': 'mat_ssafy_v3@v0',
                        'nodes': ['N07', 'N01', 'J02', 'J07', 'N03'], 'action_at_end': {'type': 'none'}}
        self.db = sqlite3.connect(':memory:'); self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE sim_reroute_multiseg_routes(task_id TEXT, resources_json TEXT)')
        self.db.execute('INSERT INTO sim_reroute_multiseg_routes VALUES (?,?)', ('T1', '[]'))
        self.db.execute('CREATE TABLE sim_reroute_multi_steps(task_id TEXT,transition_id TEXT,seq INTEGER,stop_index INTEGER,event_id TEXT,event_digest TEXT,next_payload BLOB,next_digest TEXT,state TEXT)')
        self.db.execute('CREATE TABLE sim_event_inbox(robot_id TEXT,event_id TEXT,task_id TEXT,robot_session TEXT,seq INTEGER,payload TEXT,digest TEXT,state TEXT,result TEXT)')
        event = {'type': 'rear_clear', 'event_id': 'event-new-2', 'robot_session': 'boot-1',
                 'order_id': 'T1', 'seq': 2, 'map_id': 'mat_ssafy_v3@v0', 'stopped_at': 'N01', 'cleared_resources': []}
        _, encoded, digest = _event(event)
        result = _canonical({'status': 'reroute_segment_completed', 'task_id': 'T1', 'next_seq': 3}).decode('ascii')
        self.db.execute('INSERT INTO sim_event_inbox VALUES (?,?,?,?,?,?,?,?,?)',
                        ('sim-r1', event['event_id'], 'T1', 'boot-1', 2, encoded, digest, 'applied', result))
        following = plan_segment(self.data, self.message['nodes'][1:])
        order = {'order_id': 'T1', 'seq': 3, 'map_id': 'mat_ssafy_v3@v0', 'nodes': self.message['nodes'],
                 'released_until': following.released_until, 'action_at_end': {'type': 'none'}}
        payload = _canonical(order)
        self.db.execute('INSERT INTO sim_reroute_multi_steps VALUES (?,?,?,?,?,?,?,?,?)',
                        ('T1', 'a' * 32, 2, 1, event['event_id'], digest, payload,
                         hashlib.sha256(payload).hexdigest(), 'uncertain'))

    def test_exact_fresh_event_and_next_order_are_required(self):
        rows = validate_steps(self.db, self.data, self.message)
        self.assertEqual((len(rows), rows[0]['next_order']['released_until']), (1, 'J07'))
        for sql in (
                "UPDATE sim_reroute_multi_steps SET seq=3",
                "UPDATE sim_reroute_multi_steps SET event_digest='wrong'",
                "UPDATE sim_event_inbox SET result='{}'",
                "UPDATE sim_reroute_multi_steps SET next_digest='wrong'",
                "UPDATE sim_reroute_multi_steps SET stop_index=2",
                "UPDATE sim_event_inbox SET state='pending'"):
            self.db.execute('SAVEPOINT tamper'); self.db.execute(sql)
            with self.assertRaises(ValueError, msg=sql): validate_steps(self.db, self.data, self.message)
            self.db.execute('ROLLBACK TO tamper'); self.db.execute('RELEASE tamper')


if __name__ == '__main__':
    unittest.main()
