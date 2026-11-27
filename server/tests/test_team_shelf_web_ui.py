"""Team shelf UI exposes temporary scope and unknown slot observations."""
import shutil
import subprocess
import unittest
from server.order_demo_ui import HTML_PAGE


class TeamShelfUiTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'requires Node')
    def test_process_scope_and_unknown_slots_are_visible(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(0,'utf8');
assert(html.includes('서버 재시작 후 작업을 자동 복구하지 않으며'));
const source=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els=new Map();
function node(){return {value:'',disabled:false,hidden:false,textContent:'',children:[],handlers:{},
 classList:{toggle(){}},addEventListener(t,f){this.handlers[t]=f},setAttribute(){},
 replaceChildren(){this.children=[]},appendChild(x){this.children.push(x)}}}
function el(id){if(!els.has(id))els.set(id,node());return els.get(id)}
const response=value=>({ok:true,status:200,json:async()=>value});
const context={localStorage:(()=>{const m=new Map();return {get length(){return m.size},key(i){return [...m.keys()][i]??null},getItem(k){return m.has(k)?m.get(k):null},setItem(k,v){m.set(k,String(v))},removeItem(k){m.delete(k)}}})(),
 document:{getElementById:el,createElement:node,createElementNS:node,querySelector:()=>node()},
 crypto:{randomUUID:()=> 'x'},setInterval(){},fetch:async url=>{
 if(url.endsWith('/nodes'))return response({nodes:[]});
 if(url.endsWith('/products'))return response({products:[]});
 if(url.endsWith('/map'))return response({scope:'isolated_simulation',nodes:[],edges:[],occupancy:[]});
 if(url.endsWith('/orders'))return response({scope:'team_shelf_process_only',orders:[]});
 if(url.endsWith('/inventory'))return response({scope:'team_shelf_process_only',fresh:true,shelves:{N01:{slots:[true,false,null,false],claimed:[0]}}});
 if(url.endsWith('/metrics'))return response({source:'unknown'});
 if(url.endsWith('/status'))return response({team_shelf_mode:true,state_persistence:'process_only',restart_recovery:false,mode:'fleet',accepting_orders:false,connected:true,unreal_connected:true,stale:false,robots:[]});
 throw Error(url);
 }};
vm.runInNewContext(source,context);
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{await flush();await flush();await flush();
 assert.strictEqual(el('team-shelf-lifetime').hidden,false);
 assert(el('saved-inventory').textContent.includes('확인 불가 1'));
 assert(el('saved-inventory').textContent.includes('예약 1'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
