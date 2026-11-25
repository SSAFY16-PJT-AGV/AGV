"""Run the test-owned server/team-robot reconciliation demo on loopback.

This creates a fresh throwaway SQLite database and a dedicated local broker.
It never loads an existing server database, real robot, or shared broker.
"""
import argparse
import os
from pathlib import Path
import unittest


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the isolated hold-only reconciliation demo')
    parser.add_argument('--team-repo', required=True, help='checkout of the team robot branch')
    parser.add_argument('--mosquitto', required=True, help='local Mosquitto executable')
    options = parser.parse_args(argv)
    repo = Path(options.team_repo).resolve()
    executable = Path(options.mosquitto).resolve()
    if not (repo / 'agv' / 'link' / 'reconcile.py').is_file():
        parser.error('team checkout does not contain agv/link/reconcile.py')
    if not (repo / 'agv' / 'tests' / 'test_link_disconnect_fence.py').is_file():
        parser.error('team checkout does not contain the synthetic controller fixture')
    if not executable.is_file():
        parser.error('Mosquitto executable does not exist')
    previous = {name: os.environ.get(name) for name in ('AGV_FIELD_REPO', 'MOSQUITTO_EXE')}
    os.environ['AGV_FIELD_REPO'], os.environ['MOSQUITTO_EXE'] = str(repo), str(executable)
    try:
        name = ('server.tests.test_physical_reconcile_team_mqtt.TeamReconcileMqttTests.'
                'test_same_session_after_real_broker_restart_exchanges_only_hold_evidence')
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(name))
        return 0 if result.wasSuccessful() and result.testsRun == 1 and not result.skipped else 1
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == '__main__':
    raise SystemExit(main())
