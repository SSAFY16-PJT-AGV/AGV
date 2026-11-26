"""Browser recovery must not repeat a possibly accepted order."""
import shutil
import subprocess
import unittest

from server.order_demo_ui import HTML_PAGE


class SubmissionRefreshTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'requires Node to exercise browser JavaScript')
    def test_upstream_acceptance_followed_by_503_preserves_original_key(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const values=new Map(), accepted=new Map(); let posts=0, lookup='missing';
const storage={get length(){return values.size},key(i){return [...values.keys()][i] ?? null},
 getItem(k){return values.has(k)?values.get(k):null},setItem(k,v){values.set(k,String(v))},removeItem(k){values.delete(k)}};
const response=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>value});
function makePage(){
 const els=new Map(),timers=[];
 function node(){return {value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
  classList:{toggle(){}},setAttribute(){},addEventListener(t,f){this.handlers[t]=f},
  replaceChildren(){this.children=[]},appendChild(c){this.children.push(c)}}}
 function el(id){if(!els.has(id))els.set(id,node());return els.get(id)}
 const context={localStorage:storage,crypto:{randomUUID:()=> 'accepted-key'},
  document:{getElementById:el,createElement:node,createElementNS:node,querySelector:()=>node()},
  setInterval(f){timers.push(f)},fetch:async(url,options={})=>{
   if(url.endsWith('/products'))return response({products:[{id:'red',label:'Red',pickup_node:'D01',destination_node:'D04',shelf_id:'S'}]});
   if(url.endsWith('/nodes'))return response({nodes:[{id:'D01'}]});
   if(url.endsWith('/status'))return response({status:'idle',phase:null,route:[],stale:false,connected:true,unreal_connected:true});
   if(url.endsWith('/product-orders')){
    posts++; const key=options.headers['Idempotency-Key'];
    assert(values.has('agv-demo-submission-v1:'+key));
    accepted.set(key,{status:'completed',request_id:'accepted-1',result:{status:'moving'}});
    return response({detail:'upstream response lost'},503);
   }
   if(url.endsWith('/submission-lookup')){
    const key=options.headers['Idempotency-Key']; assert(accepted.has(key));
    if(lookup==='missing') return response({},404);
    if(lookup==='unknown') return response({status:'unknown',request_id:'accepted-1'});
    return response(accepted.get(key));
   }
   return response({},404);
  }};
 vm.runInNewContext(source,context); return {el,timers};
}
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
 const first=makePage();await flush();await flush();first.el('product').value='red';
 await first.el('product-form').handlers.submit({preventDefault(){}});
 assert.strictEqual(posts,1);assert.strictEqual(accepted.size,1);assert.strictEqual(values.size,1);
 assert(first.el('product-feedback').textContent.includes('결과를 확인할 수 없습니다'));
 assert.strictEqual(first.el('submit-product').disabled,true);
 await first.el('product-form').handlers.submit({preventDefault(){}});assert.strictEqual(posts,1);
 const reload=makePage();await flush();await flush();reload.el('product').value='red';
 await reload.timers[1]();assert.strictEqual(values.size,1);
 assert.strictEqual(reload.el('submit-product').disabled,true);
 lookup='unknown';await reload.timers[1]();assert.strictEqual(values.size,1);
 assert.strictEqual(reload.el('submit-product').disabled,true);
 lookup='completed';await reload.timers[1]();await flush();
 assert.strictEqual(values.size,0);assert.strictEqual(posts,1);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'requires Node to exercise browser JavaScript')
    def test_lost_post_then_reload_recovers_by_original_key_without_reposting(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const values=new Map();
const storage={get length(){return values.size},key(i){return [...values.keys()][i] ?? null},
 getItem(k){return values.has(k)?values.get(k):null},setItem(k,v){values.set(k,String(v))},removeItem(k){values.delete(k)}};
let posts=0,lookup='missing',submittedKey=null;
const response=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>value});
function makePage(store=storage){
 const els=new Map(),timers=[];
 function node(){return {value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
  classList:{toggle(){}},setAttribute(){},addEventListener(t,f){this.handlers[t]=f},
  replaceChildren(){this.children=[]},appendChild(c){this.children.push(c)}}}
 function el(id){if(!els.has(id))els.set(id,node());return els.get(id)}
 const context={localStorage:store,crypto:{randomUUID:()=> 'original-key'},
  document:{getElementById:el,createElement:node,createElementNS:node,querySelector:()=>node()},
  setInterval(f){timers.push(f)},fetch:async(url,options={})=>{
   if(url.endsWith('/products'))return response({products:[{id:'red',label:'Red',pickup_node:'D01',destination_node:'D04',shelf_id:'S'}]});
   if(url.endsWith('/nodes'))return response({nodes:[{id:'D01'}]});
   if(url.endsWith('/status'))return response({status:'idle',phase:null,route:[],stale:false,connected:true,unreal_connected:true});
   if(url.endsWith('/orders'))return response({},404);
   if(url.endsWith('/map'))return response({},503);
   if(url.endsWith('/product-orders')){
    posts++;submittedKey=options.headers['Idempotency-Key'];
    assert([...values.values()].some(v=>v.includes(submittedKey)),'key must be stored before POST');
    throw Error('connection lost after server may have accepted');
   }
   if(url.endsWith('/submission-lookup')){
    assert.strictEqual(options.headers['Idempotency-Key'],submittedKey);
    if(lookup==='missing')return response({},404);
    if(lookup==='unknown')return response({status:'unknown',request_id:'id-1'});
    return response({status:'completed',request_id:'id-1',result:{status:'moving'}});
   }
   throw Error('unexpected '+url);
  }};
 vm.runInNewContext(source,context);
 return {el,timers};
}
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
 const first=makePage();await flush();await flush();first.el('product').value='red';
 await first.el('product-form').handlers.submit({preventDefault(){}});
 assert.strictEqual(posts,1);assert(values.size>0);assert.strictEqual(first.el('submit-product').disabled,true);
 const reload=makePage();await flush();await flush();
 assert.strictEqual(reload.el('submit-product').disabled,true,'404 does not prove an order was not accepted');
 reload.el('product').value='red';await reload.el('product-form').handlers.submit({preventDefault(){}});
 assert.strictEqual(posts,1,'reload must not repeat the POST');
 lookup='unknown';await reload.timers[1]();
 assert.strictEqual(reload.el('submit-product').disabled,true,'unknown remains blocked');
 lookup='completed';await reload.timers[1]();await flush();
 assert.strictEqual(values.size,0,'confirmed original request clears its marker');
 assert.strictEqual(reload.el('submit-product').disabled,false);
 assert.strictEqual(posts,1);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'requires Node to exercise browser JavaScript')
    def test_unavailable_storage_blocks_before_the_post(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const els=new Map(),timers=[];let posts=0;
function node(){return {value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
 classList:{toggle(){}},setAttribute(){},addEventListener(t,f){this.handlers[t]=f},replaceChildren(){},appendChild(){}}}
function el(id){if(!els.has(id))els.set(id,node());return els.get(id)}
const response=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>value});
const context={localStorage:{get length(){throw Error('denied')}},crypto:{randomUUID:()=> 'x'},
 document:{getElementById:el,createElement:node,createElementNS:node,querySelector:()=>node()},setInterval(f){timers.push(f)},
 fetch:async(url)=>{
  if(url.endsWith('/products'))return response({products:[{id:'red',label:'Red',pickup_node:'D01',destination_node:'D04',shelf_id:'S'}]});
  if(url.endsWith('/nodes'))return response({nodes:[{id:'D01'}]});
  if(url.endsWith('/status'))return response({status:'idle',phase:null,route:[],stale:false,connected:true,unreal_connected:true});
  if(url.endsWith('/product-orders')){posts++;return response({status:'moving'})}
  return response({},404);
 }};
vm.runInNewContext(source,context);
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{await flush();await flush();el('product').value='red';
 await el('product-form').handlers.submit({preventDefault(){}});
 assert.strictEqual(posts,0);assert.strictEqual(el('submit-product').disabled,true);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
