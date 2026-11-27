"""Visible v3 Unreal reroute test with test-only contention and placement.

Requires a separately prepared disposable Unreal scene whose red/blue source
and shelf nodes match cycle_fixture. It never edits the supplied project. The
starting positions and simultaneous-start barrier are synthetic test inputs;
subsequent virtual robot commands use the isolated MQTT path. Not a physical
robot or safety test. Only processes started by v3_rendered_test are stopped.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import uuid

from server.tests.test_sim_reroute_multi_auto_web import cycle_fixture
from server.tools import v3_rendered_test as rendered


SERVER = '''\
import sqlite3
import uvicorn
from server.order_product_control import ProductControlEngine
original = ProductControlEngine.tick_recoverable
def test_barrier(self, dt):
    with sqlite3.connect(self.runtime.path) as db:
        count = db.execute("SELECT COUNT(*) FROM tasks WHERE status='ASSIGNED'").fetchone()[0]
    return self.snapshot() if count < 2 else original(self, dt)
ProductControlEngine.tick_recoverable = test_barrier
uvicorn.run('server.order_demo_app:app', host='127.0.0.1', port=8000,
            workers=1, proxy_headers=False)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unreal-editor', type=Path, required=True)
    parser.add_argument('--uproject', type=Path, required=True)
    parser.add_argument('--ddc', type=Path)
    parser.add_argument('--output-dir', type=Path, default=rendered.ROOT / '.venv/v3-rendered-reroute')
    parser.add_argument('--broker-deps', type=Path, default=rendered.ROOT / '.venv/broker-deps')
    parser.add_argument('--start-timeout', type=float, default=900)
    parser.add_argument('--order-timeout', type=float, default=240)
    parser.add_argument('--observe-delay', type=float, default=5)
    parser.add_argument('--hold-seconds', type=float, default=15)
    args = parser.parse_args()
    if any(args.output_dir.glob('v3-rendered-*/state/simulation.sqlite3')):
        parser.error('use a fresh output directory for each test')
    fixture = args.output_dir.resolve() / ('fixture-' + uuid.uuid4().hex[:10])
    fixture.mkdir(parents=True, exist_ok=False)
    _world, args.field_config = cycle_fixture(fixture)
    args.level = '/Game/AGV/NewMap'
    args.sim_speed_mm_s = None
    original_verify = rendered.verify_outcome

    def verify(before, after, products, orders, messages):
        observations = original_verify(before, after, products, orders, messages)
        runs = list(args.output_dir.glob('v3-rendered-*/state/simulation.sqlite3'))
        if len(runs) != 1:
            raise AssertionError('use a fresh output directory for each test')
        with sqlite3.connect(runs[0]) as db:
            finish = db.execute('SELECT COUNT(*) FROM sim_reroute_finishes').fetchone()[0]
            steps = db.execute('SELECT COUNT(*) FROM sim_reroute_multi_steps').fetchone()[0]
            if finish < 1 or steps < 2:
                raise AssertionError('no completed multi-segment reroute: finish={} steps={}'.format(finish, steps))
            detail = {}
            for table in ('sim_reroute_finishes', 'sim_reroute_multi_steps'):
                cursor = db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')
                columns = [column[0] for column in cursor.description]
                detail[table] = [dict(zip(columns, row)) for row in cursor.fetchall()]
        (runs[0].parents[1] / 'reroute-db-evidence.json').write_text(
            json.dumps(detail, indent=2, default=str), encoding='utf-8')
        observations['reroute'] = {'finishes': finish, 'multi_steps': steps,
            'initial_positions': {'sim-r1': 'N05', 'sim-r2': 'J02'},
            'contention': 'test-only wait until two assigned tasks',
            'movement': 'simulated robots using isolated MQTT; Unreal receives WebSocket'}
        return observations

    rendered.server_command = lambda python, speed: [python, '-c', SERVER]
    rendered.verify_outcome = verify
    # clean_environment deliberately strips AGV_*; explicitly inject the
    # opt-in into the child environment, not the user's persistent settings.
    original_environment = rendered.make_server_environment
    def environment(*values):
        result = original_environment(*values)
        result['AGV_ORDER_AUTO_REROUTE'] = '1'
        return result
    rendered.make_server_environment = environment
    rendered.run(args)


if __name__ == '__main__':
    main()
