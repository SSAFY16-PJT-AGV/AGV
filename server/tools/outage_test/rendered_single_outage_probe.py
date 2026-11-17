"""Test-only probe and controlled sim-r2 reconnect hold for a local demo.

The broker and sim-r1 are unaffected. A separate duplicate MQTT client causes
the real sim-r2 socket disconnect; this gate only prevents early retry until
the test has observed the disconnected period. It is never used by the app.
"""
import os
from pathlib import Path

import uvicorn

from rendered_fleet_outage_probe import app
from sim.mqtt_order_robot import SimMqttOrderRobot

_gate = Path(os.environ['AGV_TEST_R2_RECONNECT_GATE'])
_original_reconnect = SimMqttOrderRobot.reconnect


def _gated_reconnect(self, *, authorization_guard):
    if self.robot_id == 'sim-r2':
        try:
            enabled = _gate.read_text(encoding='utf-8').strip() == 'released'
        except OSError:
            enabled = False
        if not enabled:
            raise ConnectionError('test-only sim-r2 fault remains active')
    return _original_reconnect(self, authorization_guard=authorization_guard)


SimMqttOrderRobot.reconnect = _gated_reconnect


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8000, workers=1,
                proxy_headers=False, access_log=False, log_level='info')
