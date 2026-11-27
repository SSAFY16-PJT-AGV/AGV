"""Never admit the next shared pickup while its exit is reserved."""
import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from server.team_shelf_order_slots import TeamShelfOrderSlots
from server.team_shelf_pickup_gate import pickup_is_clear
from server.tests.test_team_shelf_order_slots import snapshot


class TeamShelfPickupGateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='team-pickup-gate-')
        self.addCleanup(tmp.cleanup)
        self.store = TeamShelfOrderSlots.create_new(Path(tmp.name) / 'slots.sqlite3')
        self.session = self.store.begin_session('a' * 32)
        self.store.apply_snapshot(self.session, snapshot(), fence=self.store.begin_snapshot(self.session))

    def test_unpicked_and_occupied_exit_block_next_order(self):
        self.store.reserve(self.session, 'r1', 'milk')
        with patch('server.team_shelf_pickup_gate.read_map', return_value={'occupancy': []}):
            self.assertFalse(pickup_is_clear(self.store, object()))
        self.store.mark_dispatched(self.session, 'r1', 'pick', 'pick1')
        self.store.complete_action(self.session, 'r1', 'pick1', True)
        for resource in ({'kind':'node','resource_id':'N01'},
                         {'kind':'edge','resource_id':'["D01","N01"]'},
                         {'kind':'zone','resource_id':'source-zone'}):
            with self.subTest(resource=resource), patch('server.team_shelf_pickup_gate.read_map',
                    return_value={'occupancy': [resource]}):
                self.assertFalse(pickup_is_clear(self.store, object(), {'source-zone'}))
        with patch('server.team_shelf_pickup_gate.read_map', return_value={'occupancy': []}):
            self.assertTrue(pickup_is_clear(self.store, object(), {'source-zone'}))

    def test_unreadable_or_unknown_never_opens_gate(self):
        for error in (OSError('not observed'), sqlite3.OperationalError('database locked')):
            with self.subTest(error=error), patch('server.team_shelf_pickup_gate.read_map', side_effect=error):
                self.assertFalse(pickup_is_clear(self.store, object()))
        self.store.reserve(self.session, 'r1', 'milk')
        self.store.end_session(self.session)
        with patch('server.team_shelf_pickup_gate.read_map', return_value={'occupancy': []}):
            self.assertFalse(pickup_is_clear(self.store, object()))
