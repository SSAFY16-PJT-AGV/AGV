import shutil
import subprocess
import unittest

from server.order_demo_ui import HTML_PAGE


class DashboardStatusTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'requires Node for browser JavaScript')
    def test_hung_requests_expire_and_late_results_cannot_restore_stale_state(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const els=new Map();
function node(id='') {return {id,value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
 classList:{toggle(){}},addEventListener(t,f){this.handlers[t]=f},setAttribute(){},
 replaceChildren(){this.children=[]},appendChild(x){this.children.push(x)}}}
function el(id){if(!els.has(id))els.set(id,node(id));return els.get(id)}
let now=0,timers=[],holdStatus=false,holdMap=false,holdOrders=false,releaseStatus,releaseMap,releaseOrders;
const status={mode:'fleet',action_source:'synthetic',accepting_orders:true,connected:true,unreal_connected:true,
 stale:false,robots:[{robot_id:'sim-r1',status:'moving',phase:'to_pickup',pose:{x:300,y:0},route:[]}]};
const map={scope:'isolated_simulation',coordinate_unit:'mm',snapshot_consistency:'separate_from_status',map_id:'m@v1',
 nodes:[{id:'A',pos:[0,0]},{id:'B',pos:[1000,0]}],edges:[{from:'A',to:'B',shape:[[0,0],[1000,0]]}],occupancy:[]};
const response=value=>({ok:true,status:200,json:async()=>value});
const context={localStorage:(()=>{const m=new Map();return {get length(){return m.size},key(i){return [...m.keys()][i]??null},getItem(k){return m.has(k)?m.get(k):null},setItem(k,v){m.set(k,String(v))},removeItem(k){m.delete(k)}}})(),Date:class extends Date { static now(){return now;} },
 document:{getElementById:el,createElement:()=>node(),createElementNS:()=>node(),querySelector:()=>el('intro')},
 crypto:{randomUUID:()=> 'x'},setInterval(fn){timers.push(fn)},fetch:async url=>{
 if(url.endsWith('/map')){if(holdMap)return await new Promise(resolve=>{releaseMap=()=>resolve(response(map))});return response(map)}
 if(url.endsWith('/nodes'))return response({nodes:[{id:'A'}]});
 if(url.endsWith('/products'))return response({products:[{id:'red',label:'R',pickup_node:'A',destination_node:'B',shelf_id:'S'}]});
 if(url.endsWith('/status')){if(holdStatus)return await new Promise(resolve=>{releaseStatus=()=>resolve(response(status))});return response(status)}
 if(url.endsWith('/orders')){if(holdOrders)return await new Promise(resolve=>{releaseOrders=()=>resolve(response({orders:[]}))});return response({orders:[]})}
 if(url.endsWith('/inventory'))return response({shelves:{S:{slots:[true,false]}},fresh:true});throw Error(url);
 }};
vm.runInNewContext(source,context);
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{await flush();await flush();
 assert.strictEqual(el('submit-product').disabled,false);
 assert.strictEqual(el('dashboard-map').hidden,false);
 assert.strictEqual(el('dashboard-map').children[2].children.length,1);
 holdStatus=true;const oldStatus=timers[1]();await flush();
 now+=4000;await timers[1]();
 assert.strictEqual(el('submit-product').disabled,true);
 assert(el('dashboard-health').textContent.includes('확인'));
 assert.strictEqual(el('dashboard-map').children[2].children.length,0);
 releaseStatus();await oldStatus;
 assert.strictEqual(el('submit-product').disabled,true);
 assert.strictEqual(el('dashboard-map').children[2].children.length,0);
 holdStatus=false;await timers[1]();
 assert.strictEqual(el('submit-product').disabled,false);
 assert.strictEqual(el('dashboard-map').children[2].children.length,1);
 holdMap=true;const oldMap=timers[0]();await flush();
 now+=7000;await timers[0]();
 assert.strictEqual(el('dashboard-map').hidden,true);
 assert(el('map-feedback').textContent.includes('확인'));
 releaseMap();await oldMap;
 assert.strictEqual(el('dashboard-map').hidden,true);
 holdMap=false;await timers[0]();
 assert.strictEqual(el('dashboard-map').hidden,false);
 holdOrders=true;const oldOrders=timers[0]();await flush();
 now+=7000;await timers[0]();
 assert(el('inventory-freshness').textContent.includes('확인'));
 releaseOrders();await oldOrders;
 assert(el('inventory-freshness').textContent.includes('확인'));
 holdOrders=false;await timers[0]();
 assert(el('inventory-freshness').textContent.includes('현재'));
 // A response may arrive after the deadline but before the watchdog timer.
 // It must not refresh status, map or inventory merely because it won the race.
 holdStatus=true;const slowStatus=timers[1]();await flush();
 now+=4000;releaseStatus();await slowStatus;
 assert.strictEqual(el('submit-product').disabled,true);
 assert(el('dashboard-health').textContent.includes('확인'));
 holdStatus=false;await timers[1]();
 holdMap=true;const slowMap=timers[0]();await flush();
 now+=7000;releaseMap();await slowMap;
 assert.strictEqual(el('dashboard-map').hidden,true);
 holdMap=false;await timers[0]();
 holdOrders=true;const slowOrders=timers[0]();await flush();
 now+=7000;releaseOrders();await slowOrders;
 assert(el('inventory-freshness').textContent.includes('확인'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'requires Node for browser JavaScript')
    def test_map_snapshot_draws_server_geometry_and_clears_on_error(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const els=new Map();
function node(id='') {return {id,value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},attrs:{},
  classList:{toggle(){}},addEventListener(t,f){this.handlers[t]=f},setAttribute(k,v){this.attrs[k]=v},
  replaceChildren(){this.children=[]},appendChild(x){this.children.push(x)}}}
function el(id){if(!els.has(id))els.set(id,node(id));return els.get(id)}
let fail=false, statusFail=false,timers=[];
const map={scope:'isolated_simulation',coordinate_unit:'mm',snapshot_consistency:'separate_from_status',map_id:'m@v1',
  nodes:[{id:'A',pos:[0,0]},{id:'B',pos:[1000,0]}],edges:[{from:'A',to:'B',shape:[[0,0],[1000,0]]}],
  occupancy:[{kind:'edge',resource_id:'["A","B"]',robot_id:'sim-r1'},
             {kind:'zone',resource_id:'shared',robot_id:'sim-r1'}]};
const status={mode:'fleet',accepting_orders:true,stale:false,connected:true,unreal_connected:true,
  robots:[{robot_id:'sim-r1',status:'moving',phase:'waiting_grab',pose:{x:300,y:0,th:0},route:[]}]};
const context={localStorage:(()=>{const m=new Map();return {get length(){return m.size},key(i){return [...m.keys()][i]??null},getItem(k){return m.has(k)?m.get(k):null},setItem(k,v){m.set(k,String(v))},removeItem(k){m.delete(k)}}})(),document:{getElementById:el,createElement:()=>node(),createElementNS:()=>node(),querySelector:()=>el('intro')},
 crypto:{randomUUID:()=> 'x'},setInterval(fn){timers.push(fn)},fetch:async url=>{
 if(url.endsWith('/map')){if(fail)throw Error('offline');return {ok:true,json:async()=>map}}
 if(url.endsWith('/nodes'))return {ok:true,json:async()=>({nodes:[{id:'A'}]})};
 if(url.endsWith('/products'))return {ok:true,json:async()=>({products:[{id:'red',label:'R',pickup_node:'A',destination_node:'B',shelf_id:'S'}]})};
 if(url.endsWith('/status')){if(statusFail)throw Error('offline');return {ok:true,json:async()=>status}};
 if(url.endsWith('/orders'))return {status:404,ok:false};throw Error(url);
 }};
vm.runInNewContext(source,context);
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{await flush();await flush();
 assert.strictEqual(el('dashboard-map').hidden,false);
 assert(el('dashboard-map').children.length>=3);
 assert.strictEqual(el('dashboard-map').children[0].children[0].attrs.stroke,'#cb8c36');
 assert(el('map-details').textContent.includes('shared'));
 assert.strictEqual(el('dashboard-map').children[2].children.length,1);
 const firstMarker=el('dashboard-map').children[2].children[0];
 assert.strictEqual(firstMarker.children[1].textContent,'sim-r1');
 status.robots.push({robot_id:'sim-r2',status:'moving',phase:'to_pickup',pose:{x:700,y:0,th:0},route:[]});
 await timers[1]();
 assert.deepStrictEqual(el('dashboard-map').children[2].children.map(marker=>marker.children[1].textContent),['sim-r1','sim-r2']);
 status.robots.pop();await timers[1]();
 const track=el('fleet-status').children[0].children[0];
 assert(track.children[2].className.includes('current'));
 assert.strictEqual(track.children.filter(step => (step.className || '').includes('current')).length,1);
 status.stale=true;await timers[1]();
 assert.strictEqual(el('dashboard-map').children[2].children.length,0);
 const staleTrack=el('fleet-status').children[0].children[0];
 assert.strictEqual(staleTrack.children.filter(step => (step.className || '').includes('current')).length,0);
 status.stale=false;
 for (const uncertain of ['unknown','failed','suspended']) {
   status.robots[0].status=uncertain;await timers[1]();
   assert.strictEqual(el('fleet-status').children[0].children[0].children.filter(step => (step.className || '').includes('current')).length,0);
   assert.strictEqual(el('dashboard-map').children[2].children.length,0,uncertain + ' position is not current');
 }
 status.robots[0].status='moving';await timers[1]();
 assert.strictEqual(el('dashboard-map').children[2].children.length,1);
 const lastTrack=el('fleet-status').children[0].children[0];
 assert(lastTrack.children[2].className.includes('current'));
 statusFail=true;await timers[1]();
 assert.strictEqual(lastTrack.children.filter(step => (step.className || '').includes('current')).length,0);
 map.occupancy=[];await timers[0]();
 assert(el('map-details').textContent.includes('없음'));
 fail=true; await timers[0](); await flush();
 assert.strictEqual(el('dashboard-map').hidden,true);
 assert(el('map-feedback').textContent.includes('확인'));
 assert(!el('map-details').textContent.includes('shared'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'requires Node for browser JavaScript')
    def test_source_freshness_and_failed_inventory_are_visible(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(0, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const els = new Map();
function el(id) {
  if (!els.has(id)) els.set(id, {value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
    classList:{toggle() {}},addEventListener(t,f){this.handlers[t]=f;},
    replaceChildren(){this.children=[]},appendChild(x){this.children.push(x)}});
  return els.get(id);
}
let timers=[], lost=false, inventoryLost=false, ordersMissing=false;
let status={mode:'fleet', action_source:'synthetic', accepting_orders:true, connected:true,
  unreal_connected:true, stale:false, robots:[{robot_id:'sim-r1', status:'idle',route:[]}]};
const context={localStorage:(()=>{const m=new Map();return {get length(){return m.size},key(i){return [...m.keys()][i]??null},getItem(k){return m.has(k)?m.get(k):null},setItem(k,v){m.set(k,String(v))},removeItem(k){m.delete(k)}}})(),crypto:{randomUUID:()=> 'request-key'},document:{getElementById:el,createElement:()=>({children:[],appendChild(x){this.children.push(x)},replaceChildren(){this.children=[]},setAttribute(){}}),
  querySelector:()=>el('intro')},setInterval(f){timers.push(f)},fetch:async url=>{
    if (url.endsWith('/nodes')) return {ok:true,json:async()=>({nodes:[{id:'A'}]})};
    if (url.endsWith('/products')) return {ok:true,json:async()=>({products:[{id:'red',label:'빨강',
       pickup_node:'A',destination_node:'B',shelf_id:'S'}]})};
    if (url.endsWith('/status')) {if(lost) throw Error('offline'); return {ok:true,json:async()=>status}}
    if (url.endsWith('/orders')) return ordersMissing ? {status:404,ok:false} : {ok:true,json:async()=>({orders:[]})};
    if (url.endsWith('/inventory')) {if(inventoryLost) throw Error('offline'); return {ok:true,json:async()=>
      ({shelves:{S:{slots:[true,false]}},fresh:false})}}
    throw Error(url);
  }};
vm.runInNewContext(source,context);
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
  await flush();await flush();
  assert(el('dashboard-source').textContent.includes('합성'));
  assert(el('dashboard-health').textContent.includes('연결'));
  assert(el('inventory-freshness').textContent.includes('저장'));
  ordersMissing=true;await timers[0]();
  assert(el('inventory-freshness').textContent.includes('재고·주문 연결 확인'));
  ordersMissing=false;await timers[0]();
  assert(el('inventory-freshness').textContent.includes('저장'));
  for (const field of ['stale','connected','unreal_connected']) {
    const saved=status[field];delete status[field];await timers[1]();
    assert.strictEqual(el('submit-product').disabled,true,field+' missing must block submission');
    assert(el('dashboard-health').textContent.includes('확인'));
    assert(el('status-feedback').textContent.includes('불러오지 못했습니다'));
    status[field]=saved;await timers[1]();
    assert.strictEqual(el('submit-product').disabled,false,field+' restored');
  }
  status.stale=true;await timers[1]();
  assert(el('dashboard-health').textContent.includes('최신'));
  assert.strictEqual(el('submit-product').disabled,true);
  assert(el('fleet-status').children[0].textContent.includes('마지막 관측'));
  status.stale=false;status.connected=false;await timers[1]();
  assert(el('dashboard-health').textContent.includes('연결'));
  status.connected=true;lost=true;await timers[1]();
  assert(el('dashboard-health').textContent.includes('확인'));
  assert(!el('fleet-status').children[0].textContent.includes('현재 노드'));
  inventoryLost=true;await timers[0]();
  assert(el('inventory-freshness').textContent.includes('확인'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
