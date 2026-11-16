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
const catalog = [{id:'red', label:'빨강', pickup_node:'D01', destination_node:'D04',
                  shelf_id:'SHELF_RED', slot_index:0}];
const context = {
  document: {getElementById:element, createElement:() => ({})},
  setInterval(fn) {timer = fn;},
  fetch: async (url, options = {}) => {
    if (url.endsWith('/products')) return {ok:true, json:async () => ({products:catalog})};
    if (url.endsWith('/nodes')) return {ok:true, json:async () => ({nodes:[{id:'D01'}, {id:'D04'}]})};
    if (url.endsWith('/status')) return {ok:true, json:async () => current};
    if (url.endsWith('/product-orders')) {
      posted = JSON.parse(options.body);
      current = {status:'moving', phase:'waiting_grab', product_type:'Red', current_node:'D01', route:['D01','D04']};
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
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=self.html,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
