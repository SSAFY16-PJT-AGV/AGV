"""Browser order submission must remain safe across hung network requests."""
import shutil
import subprocess
import unittest

from server.order_demo_ui import HTML_PAGE


class OrderSubmissionTimeoutTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'requires Node for browser JavaScript')
    def test_fleet_lookup_failures_block_new_orders_until_each_is_resolved(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const els=new Map();
function node(id=''){return {id,value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
 classList:{toggle(){}},addEventListener(t,f){this.handlers[t]=f},setAttribute(){},
 replaceChildren(){this.children=[]},appendChild(x){this.children.push(x)}}}
function el(id){if(!els.has(id))els.set(id,node(id));return els.get(id)}
let timers=[],posts=0,directPosts=0,fail=false,answers={'id-1':'pending','id-2':'pending'};
const status={mode:'fleet',action_source:'synthetic',accepting_orders:true,accepting_queued_orders:true,
 connected:true,unreal_connected:true,stale:false,robots:[{robot_id:'sim-r1',status:'idle',phase:'idle',route:[]}]};
const response=value=>({ok:true,status:200,json:async()=>value});
const context={document:{getElementById:el,createElement:()=>node(),createElementNS:()=>node(),querySelector:()=>el('intro')},
 crypto:{randomUUID:()=> 'key-'+posts},setInterval(fn){timers.push(fn)},fetch:async(url,options)=>{
 if(url.endsWith('/product-orders')){posts++;return response({status:'pending',request_id:'id-'+posts});}
 if(url.includes('/submissions/')){const id=url.split('/').pop();if(fail)throw Error('offline');
   if(answers[id]==='completed')return response({status:'completed',request_id:id,result:{status:'moving'}});
   if(answers[id]==='error')return response({status:'error',request_id:id,error_type:'ShelfFull'});
   return response({status:'pending',request_id:id});}
 if(url.endsWith('/nodes'))return response({nodes:[{id:'A'}]});
 if(url.endsWith('/products'))return response({products:[{id:'red',label:'R',pickup_node:'A',destination_node:'B',shelf_id:'S'}]});
 if(url.endsWith('/status'))return response(status);
 if(url.endsWith('/orders')){if(options && options.method==='POST'){directPosts++;return response({status:'moving'});}
   return {status:404,ok:false};}
 if(url.endsWith('/map'))throw Error('offline');throw Error(url);
 }};
vm.runInNewContext(source,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const submit=()=>el('product-form').handlers.submit({preventDefault(){}});
(async()=>{await flush();await flush();el('product').value='red';
 await submit();await flush();await submit();await flush();
 assert.strictEqual(posts,2);assert.strictEqual(el('submit-product').disabled,false);
 fail=true;await timers[1]();
 assert.strictEqual(el('submit-product').disabled,true,'failed pending lookups must block another order');
 assert.strictEqual(el('submit-order').disabled,true);
 await submit();assert.strictEqual(posts,2,'programmatic submit must also be blocked');
 el('pickup').value='A';el('destination').value='B';
 await el('order-form').handlers.submit({preventDefault(){}});
 assert.strictEqual(directPosts,0,'direct node order must also be blocked');
 assert(el('product-feedback').textContent.includes('새로고침'));
 fail=false;answers['id-1']='completed';answers['id-2']='pending';await timers[1]();
 assert.strictEqual(el('submit-product').disabled,true,'one resolved order must not clear another uncertain one');
 answers['id-2']='error';await timers[1]();
 assert.strictEqual(el('submit-product').disabled,false,'resolved success and failure release their own holds');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'requires Node for browser JavaScript')
    def test_hung_post_and_lookup_preserve_unknown_without_duplicate_order(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const els=new Map();
function node(id='') {return {id,value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
 classList:{toggle(){}},addEventListener(t,f){this.handlers[t]=f},setAttribute(){},
 replaceChildren(){this.children=[]},appendChild(x){this.children.push(x)}}}
function el(id){if(!els.has(id))els.set(id,node(id));return els.get(id)}
let now=0, timers=[], posts=0, lookups=[], releasePost, releaseFirstLookup;
const status={mode:'fleet',action_source:'synthetic',accepting_orders:true,connected:true,unreal_connected:true,
 stale:false,robots:[{robot_id:'sim-r1',status:'idle',phase:'idle',route:[]}]};
const response=value=>({ok:true,status:200,json:async()=>value});
const context={Date:class extends Date{static now(){return now}},
 document:{getElementById:el,createElement:()=>node(),createElementNS:()=>node(),querySelector:()=>el('intro')},
 crypto:{randomUUID:()=> 'client-idempotency-key'},setInterval(fn){timers.push(fn)},fetch:async(url,options)=>{
 if(url.endsWith('/product-orders')) {posts++;return await new Promise(resolve=>{
   releasePost=()=>resolve(response({status:'pending',request_id:'server-request-id'}));});}
 if(url.includes('/submissions/')) {lookups.push(url); if(lookups.length===1)return await new Promise(resolve=>{
   releaseFirstLookup=()=>resolve(response({status:'unknown',request_id:'server-request-id'}));});
   return response({status:'completed',request_id:'server-request-id',result:{status:'moving'}});}
 if(url.endsWith('/nodes'))return response({nodes:[{id:'A'}]});
 if(url.endsWith('/products'))return response({products:[{id:'red',label:'R',pickup_node:'A',destination_node:'B',shelf_id:'S'}]});
 if(url.endsWith('/status'))return response(status);
 if(url.endsWith('/orders'))return {status:404,ok:false};
 if(url.endsWith('/map'))throw Error('offline'); throw Error(url);
 }};
vm.runInNewContext(source,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{await flush();await flush();el('product').value='red';
 const submit=el('product-form').handlers.submit({preventDefault(){}});await flush();
 assert.strictEqual(posts,1);assert.strictEqual(el('submit-product').disabled,true);
 now=12000;await timers[1]();
 assert(el('product-feedback').textContent.includes('확인할 수 없습니다'));
 assert(el('product-feedback').textContent.includes('새로고침'));
 assert.strictEqual(el('submit-product').disabled,true);
 await el('product-form').handlers.submit({preventDefault(){}});assert.strictEqual(posts,1);
 releasePost();await submit;await flush();
 assert.deepStrictEqual(lookups,['/order-demo/api/submissions/server-request-id']);
 now=24000;await timers[1]();
 assert(el('product-feedback').textContent.includes('확인'));
 assert.strictEqual(el('submit-product').disabled,true,'delayed fleet lookup must block a duplicate order');
 await timers[1]();await flush();
 assert.strictEqual(lookups.length,2);
 assert.strictEqual(el('submit-product').disabled,false);
 releaseFirstLookup();await flush();await flush();
 assert.strictEqual(el('submit-product').disabled,false,'late unknown response must not override newer completion');
 assert.strictEqual(posts,1,'the POST must never be reissued');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
