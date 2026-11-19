import unittest
import shutil
import subprocess
from html.parser import HTMLParser


class _Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class OrderDemoUiTest(unittest.TestCase):
    def setUp(self):
        from server.order_demo_ui import HTML_PAGE
        self.html = HTML_PAGE
        self.parser = _Elements()
        self.parser.feed(HTML_PAGE)

    def test_page_has_mobile_viewport_and_accessible_order_controls(self):
        self.assertTrue(any(tag == 'meta' and attrs.get('name') == 'viewport'
                            for tag, attrs in self.parser.tags))
        for field in ('pickup_node', 'destination_node'):
            self.assertTrue(any(tag == 'select' and attrs.get('name') == field
                                for tag, attrs in self.parser.tags), field)
        self.assertTrue(any(attrs.get('aria-live') for _, attrs in self.parser.tags))
        self.assertIn('가상 운반 시연', self.html)
        self.assertIn('가상 도착', self.html)

    def test_script_uses_local_order_nodes_status_endpoints(self):
        for route in ('/order-demo/api/nodes', '/order-demo/api/orders',
                      '/order-demo/api/status'):
            self.assertIn(route, self.html)
        self.assertIn('pickup_node', self.html)
        self.assertIn('destination_node', self.html)
        self.assertIn('current_node', self.html)
        self.assertIn('setInterval', self.html)
        self.assertNotIn('innerHTML', self.html)
        self.assertFalse(any(attrs.get('src', '').startswith(('http:', 'https:', '//'))
                             or attrs.get('href', '').startswith(('http:', 'https:', '//'))
                             for _, attrs in self.parser.tags))

    def test_product_form_uses_catalog_and_order_api(self):
        self.assertTrue(any(tag == 'select' and attrs.get('name') == 'product_id'
                            for tag, attrs in self.parser.tags))
        for value in ('/order-demo/api/products', '/order-demo/api/product-orders',
                      'product_id', 'pickup_node', 'destination_node', 'shelf_id', 'slot_index'):
            self.assertIn(value, self.html)
        self.assertIn('상품 목록을 불러오지 못했습니다', self.html)

    def test_action_outcomes_are_distinct_from_unconfirmed_state(self):
        for value in ('waiting_grab', 'waiting_place', 'unknown', 'failed'):
            self.assertIn(value, self.html)
        self.assertIn('확인 중', self.html)

    @unittest.skipUnless(shutil.which('node'), 'requires Node to exercise browser JavaScript')
    def test_fleet_tracks_multiple_pending_submissions_independently(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(0, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const els = new Map();
function element(id) {
  if (!els.has(id)) els.set(id, {value:'', disabled:false, textContent:'', children:[], handlers:{},
    classList:{toggle() {}}, addEventListener(t,f) {this.handlers[t]=f;},
    replaceChildren() {this.children=[];}, appendChild(item) {this.children.push(item);}});
  return els.get(id);
}
let timer, posts=0, uuid=0, releaseFirst, slowFirst=true, immediateFull=false;
const status={mode:'fleet', accepting_orders:false, accepting_queued_orders:true, stale:false,
  connected:true, unreal_connected:true, robots:[
    {robot_id:'sim-r1', status:'moving', route:[]}, {robot_id:'sim-r2', status:'moving', route:[]}]};
const answers={'id-1':{status:'pending',request_id:'id-1'}, 'id-2':{status:'pending',request_id:'id-2'}};
const context={
  crypto:{randomUUID:()=> 'key-' + (++uuid)},
  document:{getElementById:element, createElement:()=>({children:[], appendChild(x){this.children.push(x)}, replaceChildren(){this.children=[]}, setAttribute(){}})},
  setInterval(f) {timer=f;},
  fetch:async (url) => {
    if (url.endsWith('/products')) return {ok:true,json:async()=>({products:[
      {id:'red',label:'빨강',pickup_node:'D01',destination_node:'D04',shelf_id:'SHELF_RED'}]})};
    if (url.endsWith('/nodes')) return {ok:true,json:async()=>({nodes:[{id:'D01'}]})};
    if (url.endsWith('/status')) return {ok:true,json:async()=>status};
    if (url.endsWith('/product-orders')) { posts++; const id='id-'+posts;
      if (immediateFull) return {ok:false,status:409,json:async()=>({detail:'선반에 빈 슬롯이 없습니다'})};
      return {ok:true,status:202,json:async()=>({status:'pending',request_id:id})}; }
    const id=url.split('/').pop();
    if (id === 'id-1' && slowFirst) return await new Promise(resolve => {
      releaseFirst=()=>resolve({ok:true,json:async()=>answers[id]});
    });
    if (answers[id]) return {ok:true,json:async()=>answers[id]};
    throw Error(url);
  }
};
vm.runInNewContext(source,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush(); await flush();
  element('product').value='red';
  assert.strictEqual(element('submit-product').disabled,false);
  status.robots[1].status='unknown'; await timer();
  assert.strictEqual(element('submit-product').disabled,true);
  status.robots[1].status='moving'; status.stale=true; await timer();
  assert.strictEqual(element('submit-product').disabled,true);
  status.stale=false; status.unreal_connected=false; await timer();
  assert.strictEqual(element('submit-product').disabled,true);
  status.unreal_connected=true; await timer();
  assert.strictEqual(element('submit-product').disabled,false);
  const firstSubmit=element('product-form').handlers.submit({preventDefault(){}});
  await flush(); await flush();
  assert.strictEqual(posts,1);
  assert.strictEqual(element('submit-product').disabled,false);
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(posts,2);
  assert.strictEqual(element('submission-list').children.length,2);
  await flush(); await flush();
  slowFirst=false; releaseFirst(); await firstSubmit;
  await flush(); await flush();
  answers['id-2']={status:'error',request_id:'id-2',error_type:'ShelfFull'};
  await timer();
  assert(element('submission-list').children[1].textContent.includes('선반에 빈 슬롯이 없습니다'));
  assert(element('submission-list').children[0].textContent.includes('확인 중'));
  assert.strictEqual(element('submit-product').disabled,false);
  immediateFull=true;
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(posts,3);
  assert(element('submission-list').children[2].textContent.includes('선반에 빈 슬롯이 없습니다'));
  assert.strictEqual(element('submit-product').disabled,false);
  immediateFull=false; answers['id-4']={status:'pending',request_id:'id-4'};
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(posts,4);
  answers['id-4']={status:'completed',request_id:'id-4',result:{status:'queued',order_id:'order-4'}};
  await timer();
  assert(element('submission-list').children[3].textContent.includes('접수'));
  answers['id-1']={status:'unknown',request_id:'id-1'};
  await timer();
  assert(element('submission-list').children[0].textContent.includes('확인할 수 없'));
  assert.strictEqual(element('submit-product').disabled,true);
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(posts,4);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=self.html,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'requires Node to exercise browser JavaScript')
    def test_unknown_submission_never_looks_like_a_success(self):
        script = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync(0, 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els = new Map();
function element(id) {
  if (!els.has(id)) els.set(id, {value:'', disabled:false, textContent:'', children:[], handlers:{},
    classList:{toggle() {}}, addEventListener(t, f) {this.handlers[t]=f;},
    replaceChildren() {this.children=[];}, appendChild(item) {this.children.push(item);}});
  return els.get(id);
}
let posts = 0;
let timer;
let outcome = {status:'pending', request_id:'id-1'};
const context = {
  crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000001'},
  document: {getElementById:element, createElement:() => ({children:[], appendChild(x){this.children.push(x)}, replaceChildren(){this.children=[]}, setAttribute(){}})},
  setInterval(f) {timer=f;},
  fetch: async (url) => {
    if (url.endsWith('/products')) return {ok:true, json:async () => ({products:[
      {id:'red', label:'빨강', pickup_node:'D01', destination_node:'D04', shelf_id:'SHELF_RED', slot_index:0}]})};
    if (url.endsWith('/nodes')) return {ok:true, json:async () => ({nodes:[{id:'D01'}]})};
    if (url.endsWith('/status')) return {ok:true, json:async () =>
      ({status:'idle', phase:null, route:[], stale:false, connected:true, unreal_connected:true})};
    if (url.endsWith('/product-orders')) {posts++; return {ok:true, status:202, json:async () => ({status:'pending', request_id:'id-1'})};}
    if (url.endsWith('/submissions/id-1')) return {ok:true, json:async () => outcome};
    throw new Error(url);
  }
};
vm.runInNewContext(source, context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await flush(); await flush();
  element('product').value = 'red';
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(posts, 1);
  assert.strictEqual(element('submit-product').disabled, true);
  outcome = {status:'completed', request_id:'id-1', result:{status:'unknown'}};
  await timer();
  assert(element('product-feedback').textContent.includes('재주문하지'));
  assert.strictEqual(element('submit-product').disabled, true);
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(posts, 1);
})().catch(error => {console.error(error); process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=self.html,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'requires Node to exercise browser JavaScript')
    def test_product_order_and_acknowledgement_in_javascript(self):
        script = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync(0, 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', disabled: false, textContent: '', children: [], handlers: {},
    classList: {toggle() {}},
    addEventListener(type, fn) { this.handlers[type] = fn; },
    replaceChildren() { this.children = []; },
    appendChild(item) { this.children.push(item); }
  });
  return elements.get(id);
}
let current = {status:'idle', phase:null, current_node:'J01', route:[]};
let posted;
let timer;
let postCount = 0;
let pendingReply = false;
let requestLost = false;
let rejectBeforeAcceptance = false;
let lookupLost = false;
let submissionState = {status:'pending', request_id:'test-request-1'};
let lastHeaders;
const catalog = [{id:'red', label:'빨강', pickup_node:'D01', destination_node:'D04',
                  shelf_id:'SHELF_RED', slot_index:0}];
const context = {
  crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000001'},
  document: {getElementById:element, createElement:() => ({children:[], appendChild(x){this.children.push(x)}, replaceChildren(){this.children=[]}, setAttribute(){}})},
  setInterval(fn) {timer = fn;},
  fetch: async (url, options = {}) => {
    if (url.endsWith('/products')) return {ok:true, json:async () => ({products:catalog})};
    if (url.endsWith('/nodes')) return {ok:true, json:async () => ({nodes:[{id:'D01'}, {id:'D04'}]})};
    if (url.endsWith('/status')) return {ok:true, json:async () =>
      ({stale:false, connected:true, unreal_connected:true, ...current})};
    if (url.endsWith('/submissions/test-request-1')) {
      if (lookupLost) throw new Error('status lookup lost');
      return {ok:true, json:async () => submissionState};
    }
    if (url.endsWith('/product-orders')) {
      postCount++;
      lastHeaders = options.headers;
      if (requestLost) throw new Error('connection lost');
      if (rejectBeforeAcceptance) return {ok:false, status:503, json:async () => ({detail:'busy'})};
      posted = JSON.parse(options.body);
      if (pendingReply) return {ok:true, status:202, json:async () => ({status:'pending', request_id:'test-request-1'})};
      if (current.mode === 'fleet') {
        current = {...current, accepting_orders:false};
      } else {
        current = {status:'moving', phase:'waiting_grab', product_type:'Red', current_node:'D01', route:['D01','D04']};
      }
      return {ok:true, json:async () => ({status:'moving'})};
    }
    throw new Error('unexpected URL: ' + url);
  },
};
vm.runInNewContext(source, context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await flush(); await flush();
  element('product').value = 'red';
  element('product').handlers.change();
  assert(element('product-details').textContent.includes('D01'));
  assert(element('product-details').textContent.includes('SHELF_RED'));
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(posted.product_id, 'red');
  assert.strictEqual(element('status').textContent, '집기 확인 중');
  assert.strictEqual(element('submit-product').disabled, true);
  current = {status:'moving', phase:'waiting_place', product_type:'Red', current_node:'D04', route:['D01','D04']};
  await timer();
  assert.strictEqual(element('status').textContent, '적재 확인 중');
  current = {status:'unknown', phase:'unknown', product_type:'Red', current_node:'D04', route:[]};
  await timer();
  assert.strictEqual(element('status').textContent, '결과 확인 불가');
  assert.strictEqual(element('submit-product').disabled, true);
  current = {status:'failed', phase:'done', product_type:'Red', current_node:'D04', route:[]};
  await timer();
  assert.strictEqual(element('phase').textContent, '처리 실패');
  assert.strictEqual(element('submit-product').disabled, true);
  assert(element('status-feedback').textContent.includes('다시 시작'));
  current = {status:'completed', phase:'done', product_type:'Red', current_node:'D04', route:[]};
  await timer();
  assert.strictEqual(element('status').textContent, '가상 적재 확인됨');
  current = {status:'completed', phase:'done', product_type:null, current_node:'D04', route:[]};
  await timer();
  assert.strictEqual(element('status').textContent, '가상 도착');
  current = {status:'completed', phase:'done', product_type:'Red', mode:'coordinated', orders_remaining:0, current_node:'D04', route:[]};
  await timer();
  assert.strictEqual(element('direct-orders').hidden, true);
  assert.strictEqual(element('submit-product').disabled, true);
  assert(element('status-feedback').textContent.includes('다시 시작'));
  current = {status:'idle', phase:null, current_node:'J01', route:[], stale:true};
  await timer();
  assert.strictEqual(element('submit-product').disabled, true);
  assert.strictEqual(element('submit-order').disabled, true);
  assert(element('status-feedback').textContent.includes('최신 상태'));
  current = {status:'idle', phase:null, current_node:'J01', route:[], stale:false};
  await timer();
  assert.strictEqual(element('submit-product').disabled, false);
  assert.strictEqual(element('submit-order').disabled, false);
  assert.strictEqual(element('status-feedback').textContent, '');
  current.connected = false;
  await timer();
  assert.strictEqual(element('submit-product').disabled, true);
  assert.strictEqual(element('submit-order').disabled, true);
  assert(element('status-feedback').textContent.includes('연결'));
  current.connected = true;
  current.unreal_connected = false;
  await timer();
  assert.strictEqual(element('submit-product').disabled, true);
  assert.strictEqual(element('submit-order').disabled, true);
  assert(element('status-feedback').textContent.includes('연결'));
  current.unreal_connected = true;
  await timer();
  assert.strictEqual(element('submit-product').disabled, false);
  current = {mode:'fleet', accepting_orders:true, robots:[
    {robot_id:'sim-r1', status:'moving', phase:'to_pickup', current_node:'N1', route:['N1','D01']},
    {robot_id:'sim-r2', status:'idle', phase:null, current_node:'N5', route:[]}
  ]};
  await timer();
  assert.strictEqual(element('direct-orders').hidden, true);
  assert.strictEqual(element('submit-product').disabled, false);
  const rows = element('fleet-status').children;
  assert.strictEqual(rows.length, 2);
  assert(rows[0].textContent.includes('sim-r1') && rows[0].textContent.includes('N1'));
  assert(rows[0].textContent.includes('이동 중') && rows[0].textContent.includes('N1 → D01'));
  assert(rows[1].textContent.includes('sim-r2') && rows[1].textContent.includes('N5'));
  assert(rows[1].textContent.includes('대기 중'));
  current.stale = true;
  await timer();
  assert.strictEqual(element('submit-product').disabled, true);
  assert(element('status-feedback').textContent.includes('최신 상태'));
  current.stale = false;
  await timer();
  assert.strictEqual(element('submit-product').disabled, false);
  assert.strictEqual(element('status-feedback').textContent, '');
  current.connected = false;
  await timer();
  assert.strictEqual(element('submit-product').disabled, true);
  assert(element('status-feedback').textContent.includes('연결'));
  current.connected = true;
  current.unreal_connected = false;
  await timer();
  assert.strictEqual(element('submit-product').disabled, true);
  assert(element('status-feedback').textContent.includes('연결'));
  current.unreal_connected = true;
  await timer();
  assert.strictEqual(element('submit-product').disabled, false);
  current.robots[0].status = 'waiting';
  current.robots[0].phase = 'reservation_wait';
  await timer();
  assert(element('fleet-status').children[0].textContent.includes('구간 예약 대기'));
  current.robots[0].status = 'moving';
  current.robots[0].phase = 'waiting_grab';
  await timer();
  assert(element('fleet-status').children[0].textContent.includes('집기 확인 중'));
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(posted.product_id, 'red');
  assert.strictEqual(element('submit-product').disabled, true);
  current = {mode:'fleet', accepting_orders:'invalid', robots:[]};
  await timer();
  assert.strictEqual(element('submit-product').disabled, true);
  assert(element('status-feedback').textContent.includes('불러오지 못했습니다'));
  current = {status:'idle', phase:null, current_node:'J01', route:[], stale:false};
  await timer();
  pendingReply = true;
  await element('product-form').handlers.submit({preventDefault(){}});
  assert(lastHeaders['Idempotency-Key']);
  assert.strictEqual(element('submit-product').disabled, true);
  assert(element('product-feedback').textContent.includes('확인 중'));
  await timer();
  assert.strictEqual(element('submit-product').disabled, true);
  lookupLost = true;
  await timer();
  assert.strictEqual(element('submit-product').disabled, true);
  assert(element('product-feedback').textContent.includes('자동으로 다시 확인'));
  lookupLost = false;
  await timer();
  assert(element('product-feedback').textContent.includes('확인 중'));
  submissionState = {status:'completed', request_id:'test-request-1', result:{status:'moving', order_id:'web-pickup'}};
  await timer();
  assert(element('product-feedback').textContent.includes('접수'));
  assert.strictEqual(element('submit-product').disabled, false);
  pendingReply = false;
  context.crypto = {getRandomValues(bytes) { bytes.fill(7); return bytes; }};
  rejectBeforeAcceptance = true;
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(lastHeaders['Idempotency-Key'], '07'.repeat(16));
  assert(element('product-feedback').textContent.includes('접수되지 않았습니다'));
  assert.strictEqual(element('submit-product').disabled, false);
  rejectBeforeAcceptance = false;
  requestLost = true;
  await element('product-form').handlers.submit({preventDefault(){}});
  assert(element('product-feedback').textContent.includes('결과를 확인할 수 없습니다'));
  assert(element('product-feedback').textContent.includes('재주문하지'));
  assert.strictEqual(element('submit-product').disabled, true);
  const countAfterLoss = postCount;
  await element('product-form').handlers.submit({preventDefault(){}});
  assert.strictEqual(postCount, countAfterLoss);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=self.html,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
