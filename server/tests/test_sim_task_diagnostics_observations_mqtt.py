"""Untrusted simulation claims through isolated MQTT to authenticated diagnostics."""
import asyncio
import json
import os
import unittest

from fastapi import FastAPI

from server.sim_api import router
from server.tests.test_sim_api import request
import server.tests.test_sim_runtime_command_observations_mqtt as harness_module


@unittest.skipUnless(os.environ.get('AMQTT_TEST_PATH'), 'requires isolated aMQTT dependencies')
class RuntimeClaimDiagnosticsMqttTests(unittest.TestCase):
    def test_real_record_then_default_restart_reports_historical_claim(self):
        harness = harness_module.RuntimeCommandObservationMqttTests('runTest')
        try:
            harness.setUp()
            peer, orders = harness.peer()
            runtime = harness.runtime(command_observations=True)
            runtime.start()
            harness.cleanup.callback(runtime.stop)
            response, occupied = harness.prepare(runtime, orders)
            info = peer.publish('sim-observation/sim-r1/order-response', json.dumps(response), qos=1, retain=False)
            info.wait_for_publish(timeout=5)
            self.assertTrue(info.is_published())
            harness.wait_for(lambda: runtime.command_observation_receiver.last_result is not None,
                             'response claim stored')
            self.assertEqual(runtime.command_observation_receiver.last_result,
                             {'status': 'recorded', 'outcome': 'accepted'})
            app = FastAPI()
            app.include_router(router)
            app.state.sim_runtime = runtime
            app.state.sim_api_token = 'diagnostic-token'
            def get(token='Bearer diagnostic-token'):
                return asyncio.run(request(app, 'GET', '/simulation/tasks/T1/diagnostics', token=token))
            self.assertEqual(get(token=None)[0], 401)
            code, body, headers = get()
            self.assertEqual(code, 200)
            self.assertEqual(headers[b'cache-control'], b'no-store')
            data = body['database_snapshot']['command_response_observations']
            self.assertEqual(data, {'availability': 'available', 'integrity': 'not_checked',
                'kind': 'unverified_simulation_claim', 'truncated': False,
                'records': [{'event_id': 'response-1', 'seq': 1, 'claimed_outcome': 'accepted',
                             'epoch_relation': 'same_at_db_snapshot'}]})
            self.assertNotIn(response['order_digest'], repr(body))
            self.assertNotIn(response['robot_session'], repr(body))
            self.assertEqual(runtime.store.list_occupancy(), occupied)
            runtime.stop()
            fallback = harness.runtime()
            fallback.start()
            harness.cleanup.callback(fallback.stop)
            app.state.sim_runtime = fallback
            self.assertIn('T1', fallback.gate._blocked)
            code, body, _ = get()
            self.assertEqual(code, 200)
            data = body['database_snapshot']['command_response_observations']
            self.assertEqual(data['records'][0]['epoch_relation'], 'different_at_db_snapshot')
            self.assertEqual(data['kind'], 'unverified_simulation_claim')
            self.assertEqual(data['integrity'], 'not_checked')
            self.assertEqual(fallback.store.list_occupancy(), occupied)
            self.assertEqual(fallback.store.get_order('T1')['state'], 'acked')
        finally:
            harness.doCleanups()


if __name__ == '__main__':
    unittest.main()
