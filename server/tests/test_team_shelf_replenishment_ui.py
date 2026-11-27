"""The team demo exposes an explicitly synthetic approval flow."""
import shutil
import subprocess
import unittest

from server.order_demo_ui import HTML_PAGE


class TeamShelfReplenishmentUiTest(unittest.TestCase):
    def test_ui_uses_shared_keyed_order_path_and_labels_source(self):
        self.assertIn('Unreal 선반 상태에서 합성', HTML_PAGE)
        self.assertIn('id="synthetic-capture"', HTML_PAGE)
        self.assertIn('id="synthetic-approve"', HTML_PAGE)
        self.assertIn('/order-demo/api/synthetic-shortages', HTML_PAGE)
        self.assertIn("submitRequest('/order-demo/api/synthetic-replenishment'", HTML_PAGE)
        self.assertIn("approved: true", HTML_PAGE)

    @unittest.skipUnless(shutil.which('node'), 'requires Node')
    def test_capture_and_approval_preserve_key_before_post(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const els=new Map(),values=new Map(),calls=[];
function node(){return {value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
 classList:{toggle(){}},addEventListener(t,f){this.handlers[t]=f},setAttribute(){},
 replaceChildren(){this.children=[]},appendChild(x){this.children.push(x)}}}
function el(id){if(!els.has(id))els.set(id,node());return els.get(id)}
const storage={get length(){return values.size},key(i){return [...values.keys()][i]??null},
 getItem(k){return values.has(k)?values.get(k):null},setItem(k,v){values.set(k,String(v))},removeItem(k){values.delete(k)}};
const response=value=>({ok:true,status:200,json:async()=>value});
const context={localStorage:storage,document:{getElementById:el,createElement:node,createElementNS:node,querySelector:()=>node()},
 crypto:{randomUUID:()=> 'approval-key'},setInterval(){},fetch:async(url,opts={})=>{
  calls.push([url,opts]);
  if(url.endsWith('/nodes'))return response({nodes:[]});
  if(url.endsWith('/products'))return response({products:[{id:'milk',label:'우유',pickup_node:'D01',destination_node:'D02',shelf_id:'N02'}]});
  if(url.endsWith('/status'))return response({team_shelf_mode:true,mode:'fleet',accepting_orders:true,connected:true,unreal_connected:true,stale:false,robots:[]});
  if(url.endsWith('/synthetic-shortages'))return response({source:'synthetic_from_unreal_snapshot',products:{milk:{status:'shortage'}}});
  if(url.endsWith('/synthetic-replenishment')){
   assert(values.has('agv-demo-submission-v1:approval-key'));
   assert.strictEqual(opts.headers['Idempotency-Key'],'approval-key');
   assert.deepStrictEqual(JSON.parse(opts.body),{product_id:'milk',threshold:1,approved:true});
   return response({status:'moving'});
  }
  if(url.endsWith('/map'))return response({scope:'isolated_simulation',nodes:[],edges:[],occupancy:[]});
  if(url.endsWith('/orders'))return response({scope:'team_shelf_process_only',orders:[]});
  if(url.endsWith('/inventory'))return response({scope:'team_shelf_process_only',fresh:true,shelves:{}});
  if(url.endsWith('/metrics'))return response({source:'unknown'});
  return response({});
 }};
vm.runInNewContext(source,context);
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{await flush();await flush();await flush();
 el('product').value='milk';el('product').handlers.change();
 assert.strictEqual(el('synthetic-approve').disabled,true);
 await el('synthetic-capture').handlers.click();
 assert(el('synthetic-result').textContent.includes('우유: 부족'));
 assert.strictEqual(el('synthetic-approve').disabled,false);
 await el('synthetic-approve').handlers.click();
 assert.strictEqual(calls.filter(([url])=>url.endsWith('/synthetic-replenishment')).length,1);
 assert(!values.has('agv-demo-submission-v1:approval-key'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
