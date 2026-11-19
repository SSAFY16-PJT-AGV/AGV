"""Local simulation map is observable without authorizing physical motion."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server.order_demo_app import app, DemoRuntime
from server.sim_handoff_store import SimHandoffStore
from server.sim_runtime import SimRuntime
from server.segment_progress import SegmentProgress
from server.segments import plan_segment

MAP = {'map_id': 'map', 'version': 1, 'unit': 'mm',
       'nodes': [{'id': 'A', 'x': 0., 'y': 0., 'pos': [0., 0.]},
                 {'id': 'B', 'x': 1., 'y': 0., 'pos': [1000., 0.]},
                 {'id': 'C', 'x': 0., 'y': 1., 'pos': [0., 1000.]}],
       'edges': [{'from': 'A', 'to': 'B', 'shape': [[0., 0.], [1000., 0.]],
                  'bidirectional': True, 'lanes': 1},
                 {'from': 'A', 'to': 'C', 'shape': [[0., 0.], [0., 1000.]],
                  'bidirectional': True, 'lanes': 1}],
       'zones': [{'id': 'a', 'type': 'waiting', 'node': 'A'},
                 {'id': 'b', 'type': 'waiting', 'node': 'B'},
                 {'id': 'c', 'type': 'waiting', 'node': 'C'}]}

async def get_map():
    scope = {'type':'http','asgi':{'version':'3.0'},'http_version':'1.1',
             'method':'GET','scheme':'http','path':'/order-demo/api/map',
             'raw_path':b'/order-demo/api/map','query_string':b'',
             'headers':[(b'host',b'127.0.0.1:8000')],
             'client':('127.0.0.1',50000),'server':('127.0.0.1',8000)}
    messages = []
    async def receive(): return {'type':'http.request','body':b'','more_body':False}
    async def send(message): messages.append(message)
    await app(scope,receive,send)
    start=next(m for m in messages if m['type']=='http.response.start')
    return start['status'], dict(start['headers']), json.loads(b''.join(
        m.get('body',b'') for m in messages if m['type']=='http.response.body'))

class _Engine:
    def snapshot(self): return {'mode':'fleet','robots':[]}

class LocalMapApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)/'field.sqlite3'
        self.store=SimHandoffStore.create_new(path,simulation_enabled=True,
                                               idle_positions={'sim-r1':'A','sim-r2':'B'})
        self.runtime=SimRuntime(path,MAP,{'sim-r1':{'job1':'s1'},'sim-r2':{'job2':'s2'}},
                                port=18831,client_id='map-test',simulation_store=self.store)
        self.runtime._owner.acquire()
        self.addCleanup(self.runtime._owner.release)
        self.runtime._started=True
        self.runtime.store=self.store
        self.runtime.receiver=SimpleNamespace(ready=True)
        self.runtime.progress=SegmentProgress(self.store,MAP,simulation_enabled=True)
        self.runtime.progress.initialize()
        self.demo=DemoRuntime(_Engine(),18831,[])
        self.demo.map_runtime=self.runtime
        app.state.demo=self.demo
        self.addCleanup(setattr,app.state,'demo',None)

    async def test_returns_mm_geometry_and_idle_occupancy_from_real_db(self):
        code,headers,value=await get_map()
        self.assertEqual(code,200)
        self.assertEqual(headers.get(b'cache-control'),b'no-store')
        self.assertEqual(value['scope'],'isolated_simulation')
        self.assertEqual(value['coordinate_unit'],'mm')
        self.assertEqual(value['snapshot_consistency'],'separate_from_status')
        self.assertEqual(value['map_id'],'map@v1')
        self.assertEqual(value['nodes'][1],{'id':'B','pos':[1000.0,0.0]})
        self.assertEqual(value['edges'][0]['shape'],[[0.0,0.0],[1000.0,0.0]])
        self.assertEqual(value['occupancy'],[{'kind':'node','resource_id':'A','robot_id':'sim-r1'},
                                              {'kind':'node','resource_id':'B','robot_id':'sim-r2'}])

    async def test_disconnected_stopped_and_unsupported_return_503_not_empty_map(self):
        for configure in (lambda:setattr(self.runtime,'_stopped',True),
                          lambda:(setattr(self.runtime,'_stopped',False),setattr(self.runtime.receiver,'ready',False)),
                          lambda:setattr(self.demo,'map_runtime',None)):
            configure()
            code,headers,value=await get_map()
            self.assertEqual(code,503)
            self.assertEqual(headers.get(b'cache-control'),b'no-store')
            self.assertNotIn('occupancy',value)

    async def test_bad_geometry_or_db_error_never_returns_empty_occupancy(self):
        self.runtime.map_data['nodes'][0]['pos']=[float('nan'),0]
        self.assertEqual((await get_map())[0],503)
        self.runtime.map_data['nodes'][0]['pos']=[0,0]
        with patch('server.order_demo_map.sqlite3.connect',side_effect=sqlite3.OperationalError('locked')):
            code,headers,value=await get_map()
        self.assertEqual(code,503)
        self.assertEqual(headers.get(b'cache-control'),b'no-store')
        self.assertNotIn('occupancy',value)

    async def test_field_map_coordinates_use_mm_pos_not_metre_xy(self):
        code,_,value=await get_map()
        self.assertEqual(code,200)
        self.assertEqual(value['nodes'][1]['pos'],[1000.0,0.0])

    async def test_owner_pinned_across_database_read(self):
        original=self.store.validate_bound
        def checked(*args, **kwargs):
            self.assertGreater(self.runtime._reference_operations,0)
            return original(*args,**kwargs)
        with patch.object(self.store,'validate_bound',side_effect=checked):
            self.assertEqual((await get_map())[0],200)
        self.assertEqual(self.runtime._reference_operations,0)

    async def test_missing_idle_robot_occupancy_is_not_an_empty_space(self):
        with closing(sqlite3.connect(self.runtime.path)) as db, db:
            db.execute("DELETE FROM sim_idle_robots WHERE robot_id='sim-r2'")
        code,headers,value=await get_map()
        self.assertEqual(code,503)
        self.assertEqual(headers.get(b'cache-control'),b'no-store')
        self.assertNotIn('occupancy',value)

    def _allocate(self):
        self.store.create_task('job1')
        segment=plan_segment(MAP,['A','C'])
        self.store.allocate('job1','sim-r1','map@v1',['A','C'],segment.released_until,
                            segment.resources,{'type':'load'},
                            map_fingerprint=self.runtime.progress._fingerprint)

    async def test_active_and_idle_occupancy_are_one_snapshot(self):
        self._allocate()
        code,_,value=await get_map()
        self.assertEqual(code,200)
        occupancy={(row['kind'],row['resource_id'],row['robot_id']) for row in value['occupancy']}
        self.assertEqual(occupancy,{('node','A','sim-r1'),('node','C','sim-r1'),
                                    ('edge','["A","C"]','sim-r1'),('node','B','sim-r2')})

    async def test_missing_active_occupancy_is_not_an_empty_space(self):
        self._allocate()
        with closing(sqlite3.connect(self.runtime.path)) as db, db:
            db.execute("DELETE FROM allocation_occupancy WHERE kind='edge'")
        code,headers,value=await get_map()
        self.assertEqual(code,503)
        self.assertEqual(headers.get(b'cache-control'),b'no-store')
        self.assertNotIn('occupancy',value)

    async def test_mutated_map_fingerprint_is_unavailable(self):
        self.runtime.map_data['nodes'][1]['pos']=[1001.,0.]
        self.assertEqual((await get_map())[0],503)

    async def test_active_task_cannot_be_observed_under_another_robot(self):
        self._allocate()
        self.runtime._tasks['job1']=('sim-r2','s2')
        self.assertEqual((await get_map())[0],503)

    async def test_stop_between_snapshot_and_return_is_unavailable(self):
        original=self.store.validate_bound
        count=0
        def stop_after_second_check(*args, **kwargs):
            nonlocal count
            result=original(*args,**kwargs)
            count+=1
            if count==2:
                self.runtime._stopped=True
            return result
        with patch.object(self.store,'validate_bound',side_effect=stop_after_second_check):
            code,headers,value=await get_map()
        self.assertEqual(code,503)
        self.assertEqual(headers.get(b'cache-control'),b'no-store')
        self.assertNotIn('occupancy',value)

    async def test_nonfleet_mode_without_idle_occupancy_is_unavailable(self):
        self.store._idle_enabled=False
        code,_,value=await get_map()
        self.assertEqual(code,503)
        self.assertNotIn('occupancy',value)
