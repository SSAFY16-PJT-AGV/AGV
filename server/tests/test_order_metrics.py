"""Statistics are derived only from persisted synthetic order observations."""
import importlib
import importlib.util
from contextlib import closing
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from server.order_persistence import PersistentOrderStore
from server.tests.test_order_persistence import CATALOG, shelves


class OrderMetricsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='order-metrics-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'orders.db'
        self.store = PersistentOrderStore(self.path, 'warehouse@v1', CATALOG)

    def metrics(self):
        self.assertIsNotNone(importlib.util.find_spec('server.order_metrics'), 'metrics module required')
        return importlib.import_module('server.order_metrics').order_metrics(self.store)

    def completed_order(self):
        order = self.store.submit('red', request_key='one')
        self.store.claim_next()
        revision = self.store.sync_shelves(shelves())
        slot = self.store.reserve_slot('SHELF_RED', 'Red', revision, request_id=order['request_id'])
        result = {'assigned_robot': 'sim-r1', 'order_id': 'job-1', 'shelf_id': 'SHELF_RED',
                  'slot_index': slot['slot_index'], 'reservation_id': slot['reservation_id']}
        self.store.mark_active(order['request_id'], result)
        self.store.mark_completed(order['request_id'], result)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE orders SET created_at='2026-01-01 00:00:00' WHERE request_id=?",
                       (order['request_id'],))
            for state, at in [('queued', '2026-01-01 00:00:00'), ('dispatching', '2026-01-01 00:00:02'),
                              ('active', '2026-01-01 00:00:05'), ('completed', '2026-01-01 00:00:20')]:
                db.execute('UPDATE order_events SET at=? WHERE request_id=? AND state=?',
                           (at, order['request_id'], state))
        return order['request_id']

    def test_counts_and_durations_use_recorded_events_without_guessing_stock(self):
        self.completed_order()
        self.store.submit('blue', request_key='two')
        data = self.metrics()
        self.assertEqual(data['source'], 'simulation_journal')
        self.assertEqual(data['orders_by_state']['completed'], 1)
        self.assertEqual(data['orders_by_state']['queued'], 1)
        self.assertEqual(data['waiting_seconds'], {'samples': 1, 'average': 5.0})
        self.assertEqual(data['processing_seconds'], {'samples': 1, 'average': 15.0})
        self.assertEqual(data['total_seconds'], {'samples': 1, 'average': 20.0})
        self.assertEqual(data['invalid_timestamps'], 0)
        self.assertEqual(data['inventory']['freshness'], 'unknown')
        self.assertEqual(data['inventory']['change'], None)

    def test_missing_or_reversed_events_never_generate_fake_durations(self):
        request_id = self.completed_order()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE order_events SET at='2025-12-31 23:59:59' "
                       "WHERE request_id=? AND state='active'", (request_id,))
        data = self.metrics()
        self.assertEqual(data['invalid_timestamps'], 1)
        self.assertEqual(data['waiting_seconds'], {'samples': 0, 'average': None})
        self.assertEqual(data['processing_seconds'], {'samples': 0, 'average': None})
        self.assertEqual(data['total_seconds'], {'samples': 0, 'average': None})

    def test_empty_journal_reports_no_samples_instead_of_zero_seconds(self):
        data = self.metrics()
        self.assertEqual(data['orders_by_state']['completed'], 0)
        self.assertEqual(data['total_seconds'], {'samples': 0, 'average': None})
        self.assertEqual(data['inventory']['unknown'], 6)

    def test_local_dashboard_api_reports_metrics_without_mutating_orders(self):
        from server.order_demo_app import app
        old = app.state._state.copy()
        self.addCleanup(lambda: (app.state._state.clear(), app.state._state.update(old)))
        app.state.demo = SimpleNamespace(journal=self.store)
        self.store.submit('red')
        before = self.store.list_orders()
        path = '/order-demo/api/metrics'
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'GET', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                 'query_string': b'', 'root_path': '', 'headers': [(b'host', b'127.0.0.1:8000')],
                 'client': ('127.0.0.1', 9999), 'server': ('127.0.0.1', 8000)}
        messages = []
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            messages.append(message)
        asyncio.run(app(scope, receive, send))
        start = next(m for m in messages if m['type'] == 'http.response.start')
        body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
        self.assertEqual(start['status'], 200)
        self.assertEqual(dict(start['headers']).get(b'cache-control'), b'no-store')
        self.assertEqual(json.loads(body)['orders_by_state']['queued'], 1)
        self.assertEqual(self.store.list_orders(), before)


if __name__ == '__main__':
    unittest.main()
