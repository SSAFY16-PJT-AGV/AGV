"""Read-only state probe for the local, test-owned fleet demonstration only."""
import hmac
import os

from fastapi import HTTPException, Request
import uvicorn

from server.order_demo_app import app
from sim import order_executor

# Deliberately slower motion widens the observable fault-injection window.
# This separate test process never changes a real device or the product code.
order_executor.SPEED_MM_PER_SECOND = 400.0

_token = os.environ['AGV_TEST_OBSERVER_TOKEN']


def _get_demo(request):
    if (request.client is None or request.client.host not in ('127.0.0.1', '::1')
            or not hmac.compare_digest(request.headers.get('x-test-observer-token', ''), _token)):
        raise HTTPException(403)
    demo = getattr(request.app.state, 'demo', None)
    if demo is None:
        raise HTTPException(503)
    return demo


@app.get('/_isolated_test/fleet')
def fleet(request: Request):
    demo = _get_demo(request)
    if not demo.lock.acquire(blocking=False):
        raise HTTPException(503)
    try:
        engines = demo.engine.engines
        return {'recovering': demo._recovering.is_set(),
                'broker_publisher_connected': demo.connected.is_set(),
                'unreal_owner_connected': demo.actions.available_fast(),
                'world_ready': demo._world_ready.is_set(),
                'fleet': demo.engine.snapshot(),
                'robots': {key: {'ready': engine.robot.ready,
                                  'executor': engine.robot.executor.snapshot()}
                           for key, engine in engines.items()}}
    finally:
        demo.lock.release()


@app.get('/_isolated_test/fleet-fast')
def fleet_fast(request: Request):
    """Sample each executor's own lock while the recovery lock is busy.

    A flag change during sampling is rejected. This is observational only,
    does not prove physical position, and never permits a simulated tick.
    """
    demo = _get_demo(request)
    def generation():
        return (demo._recovering.is_set(), demo.connected.is_set(),
                demo.actions.available_fast(), demo._world_ready.is_set())
    before = generation()
    robots = {key: {'ready': engine.robot.ready,
                    'executor': engine.robot.executor.snapshot()}
              for key, engine in demo.engine.engines.items()}
    after = generation()
    if before != after:
        raise HTTPException(503)
    return {'recovering': after[0], 'broker_publisher_connected': after[1],
            'unreal_owner_connected': after[2], 'world_ready': after[3],
            'robots': robots}


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8000, workers=1,
                proxy_headers=False, access_log=False, log_level='info')
