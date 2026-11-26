"""The local dashboard shows uncertainty without issuing a second order."""
import shutil
import subprocess
import unittest

from server.order_demo_ui import HTML_PAGE


class UnknownOrderInvestigationUiTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'requires Node')
    def test_lookup_unknown_and_failed_details_are_separate_from_absent_key(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const key='agv-demo-submission-v1:original-key';
const values=new Map([[key,JSON.stringify({version:1,key:'original-key',kind:'product',label:'상품'})]]);
const storage={get length(){return values.size},key(i){return [...values.keys()][i]??null},
 getItem(k){return values.has(k)?values.get(k):null},setItem(k,v){values.set(k,String(v))},removeItem(k){values.delete(k)}};
const els=new Map(),timers=[],calls=[];let mode='missing',details='ok';
function node(){return {value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
 classList:{toggle(){}},setAttribute(){},addEventListener(t,f){this.handlers[t]=f},
 replaceChildren(){this.children=[]},appendChild(c){this.children.push(c)}}}
function el(id){if(!els.has(id))els.set(id,node());return els.get(id)}
const response=(v,s=200)=>({ok:s>=200&&s<300,status:s,json:async()=>v});
const context={localStorage:storage,crypto:{randomUUID:()=> 'another-key'},
 document:{getElementById:el,createElement:node,createElementNS:node,querySelector:()=>node()},
 setInterval(f){timers.push(f)},fetch:async(url,options={})=>{
  calls.push([url,options.method||'GET']);
  if(url.endsWith('/products'))return response({products:[{id:'red',label:'Red',pickup_node:'D01',destination_node:'D04',shelf_id:'S'}]});
  if(url.endsWith('/nodes'))return response({nodes:[{id:'D01'}]});
  if(url.endsWith('/status'))return response({status:'idle',phase:null,route:[],stale:false,connected:true,unreal_connected:true});
  if(url.endsWith('/submission-lookup')){
   assert.strictEqual(options.headers['Idempotency-Key'],'original-key');
   if(mode==='missing')return response({},404);
   return response({status:'unknown',request_id:'order-1'});
  }
  if(url.endsWith('/orders/order-1')) {
   if(details==='missing')return response({},404);
   return response({request_id:details==='wrong'?'other':'order-1',status:'unknown',
    assigned_robot:'sim-r1',shelf_id:'<img src=x>',slot_index:2});
  }
  return response({},404);
 }};
vm.runInNewContext(source,context);
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
 await flush();await flush();await timers[1]();await flush();
 const row=el('submission-list').children[0];
 assert(row.textContent.includes('기록을 찾지 못했습니다'));
 assert(!calls.some(([url])=>url.endsWith('/orders/order-1')));
 assert(values.has(key));assert.strictEqual(el('submit-product').disabled,true);
 mode='unknown';await timers[1]();await flush();
 assert(row.textContent.includes('order-1'));assert(row.textContent.includes('sim-r1'));
 assert(row.textContent.includes('<img src=x>'));assert(row.textContent.includes('결과 불명'));
 assert(values.has(key));assert.strictEqual(el('submit-product').disabled,true);
 details='wrong';await timers[1]();await flush();
 assert(!row.textContent.includes('sim-r1'));assert(row.textContent.includes('상세 확인 불가'));
 details='missing';await timers[1]();await flush();
 assert(row.textContent.includes('상세 확인 불가'));assert(values.has(key));
 assert(!calls.some(([,method])=>method==='POST'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
