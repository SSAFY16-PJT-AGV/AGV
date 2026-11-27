"""The dashboard must describe the selected team's restart capability."""
import shutil
import subprocess
import unittest

from server.order_demo_ui import HTML_PAGE


class TeamShelfPersistenceUiTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'requires Node')
    def test_lifetime_and_synthetic_controls_follow_server_capability(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const script=fs.readFileSync(0,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
async function run(extra){
 const els=new Map();
 function node(){return {value:'',disabled:false,hidden:false,textContent:'',children:[],
  classList:{toggle(){}},addEventListener(){},setAttribute(){},replaceChildren(){},appendChild(){}}}
 function el(id){if(!els.has(id))els.set(id,node());return els.get(id)}
 const storage={length:0,key(){return null},getItem(){return null},setItem(){},removeItem(){}};
 const context={localStorage:storage,crypto:{randomUUID:()=> 'x'},setInterval(){},
  document:{getElementById:el,createElement:node,createElementNS:node,querySelector:node},
  fetch:async url=>({ok:true,status:200,json:async()=> url.endsWith('/status') ?
   {team_shelf_mode:true,mode:'fleet',accepting_orders:false,connected:true,unreal_connected:true,
    stale:false,robots:[],...extra}:url.endsWith('/nodes')?{nodes:[]}:
   url.endsWith('/products')?{products:[]}:url.endsWith('/orders')?{orders:[]}:
   url.endsWith('/inventory')?{fresh:false}:url.endsWith('/map')?{nodes:[],edges:[],occupancy:[]}:{}})};
 vm.runInNewContext(script,context);
 for(let i=0;i<4;i++)await new Promise(r=>setImmediate(r));
 return {note:el('team-shelf-lifetime'),synthetic:el('synthetic-replenishment')};
}
(async()=>{
 let x=await run({state_persistence:'durable',restart_recovery:true,
  restart_recovery_scope:'completed_only',synthetic_replenishment_available:false});
 assert(!x.note.hidden);assert(x.note.textContent.includes('완료'));
 assert(x.note.textContent.includes('불명'));assert(x.synthetic.hidden);
 x=await run({state_persistence:'process_only',restart_recovery:false,
  restart_recovery_scope:'none',synthetic_replenishment_available:true});
 assert(!x.note.hidden);assert(x.note.textContent.includes('이번 시연'));
 assert(!x.synthetic.hidden);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], input=HTML_PAGE,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
