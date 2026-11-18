# -*- coding: utf-8 -*-
import io
import json
import os
import unittest
from agv.link import FakeLink, LinkController, MqttLink
from agv.nav import NavMap, Navigator
from agv.tests.test_link import TestSimServe

DATA = {"map_id":"m", "version":1, "nodes":[
 {"id":"X"}, {"id":"A","kind":"patch","color":"orange"}, {"id":"B","kind":"merge","inputs":{"left":"E1","right":"E3"}},
 {"id":"C","kind":"patch","color":"red","uturn":True}, {"id":"D","kind":"patch","color":"blue"}],
 "edges":[
 {"id":"E0","from":"X","to":"A","length_mm":100,"bidirectional":True},
 {"id":"E1","from":"A","to":"B","length_mm":100,"bidirectional":True},
 {"id":"E2","from":"B","to":"C","length_mm":100,"bidirectional":True},
 {"id":"E3","from":"D","to":"B","length_mm":100,"bidirectional":True}]}
ROUTE=["A","B","C","B","D"]

class Runner(object):
 state="hold"; hold_reason="wait_order"; finished=False; odo=0; resumes=0
 def hold(self, reason): self.state,self.hold_reason="hold",reason
 def resume(self): self.resumes+=1; self.state="running"
 def policy_changed(self): pass

def order(seq,release):
 return {"order_id":"T1","seq":seq,"map_id":"m@v1","nodes":ROUTE,"released_until":release,"action_at_end":{"type":"none"}}

class SequenceTests(unittest.TestCase):
 def fresh(self):
  nav=Navigator(NavMap(DATA),start="A",hold_at_end=True)
  runner=Runner(); link=FakeLink("sim-r1"); ctl=LinkController(link,nav,DATA,start_node="A"); ctl.attach(runner)
  return nav,runner,link,ctl
 def test_segment_whitelist_cannot_skip_visit_and_extends_after_hold(self):
  nav,runner,link,ctl=self.fresh()
  ctl._handle(json.dumps(order(1,"C")).encode())
  self.assertIsNone(ctl.rejected); self.assertEqual(nav.allowed,{"E1","E2"})
  nav.on_patch("orange",0); nav.pop_stop(); nav.on_merge(100); nav.pop_stop(); nav.on_patch("red",200)
  stop=nav.pop_stop(); self.assertEqual(stop["reason"],"nav_hold")
  runner.state="hold"; runner.hold_reason="nav_hold"; runner.odo=200; ctl._watch_stop()
  ctl._handle(json.dumps(order(2,"D")).encode())
  self.assertIsNone(ctl.rejected); self.assertEqual(nav.allowed,{"E2~r","E3~r"})
  self.assertEqual(nav._target_i,4); self.assertEqual(nav._route_pos,2)
  turn=nav.pop_stop(); self.assertEqual(turn["type"],"uturn")
  nav.resumed(); nav.uturned(220)
  side=nav.choose_split(240); self.assertEqual(side,"left")
  nav.on_split(side,250); nav.pop_stop(); nav.on_patch("blue",350)
  stop=nav.pop_stop(); self.assertEqual((stop["node"],stop["reason"]),("D","nav_hold"))
  self.assertEqual(nav._route_pos,4)
 def test_early_extension_is_rejected_and_keeps_old_route(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  old=set(nav.allowed); ctl._handle(json.dumps(order(2,"D")).encode())
  self.assertIsNotNone(ctl.rejected); self.assertEqual(ctl.book.current["seq"],1); self.assertEqual(nav.allowed,old)
 def test_repeat_inside_one_segment_is_rejected_without_start(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"D")).encode())
  self.assertIsNotNone(ctl.rejected); self.assertEqual(runner.resumes,0); self.assertIsNone(ctl.book.current)
 def test_gap_stops_and_duplicate_does_not_restart(self):
  nav,runner,link,ctl=self.fresh(); payload=json.dumps(order(1,"C")).encode(); ctl._handle(payload)
  resumes=runner.resumes; ctl._handle(payload); self.assertEqual(runner.resumes,resumes)
  nav.on_gap(); stop=nav.pop_stop(); self.assertEqual((stop["reason"],stop["hold"]),("nav_off_route",True))
  self.assertIsNone(nav.at_node)
 def test_unexpected_visit_after_target_does_not_move_cursor(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  nav._route_advance("D"); stop=nav.pop_stop()
  self.assertEqual(nav._route_pos,0); self.assertEqual((stop["reason"],stop["hold"]),("nav_off_route",True))
 def test_repeat_route_with_via_is_rejected(self):
  data=json.loads(json.dumps(DATA)); data["nodes"][2]["kind"]="via"
  nav=Navigator(NavMap(data),start="A")
  with self.assertRaises(ValueError): nav.set_route(ROUTE,release="C")
 def test_same_node_repeat_within_one_release_is_rejected_at_hold(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"B")).encode())
  nav.on_patch("orange",0); nav.pop_stop(); nav.on_merge(100)
  self.assertEqual(nav.pop_stop()["reason"],"nav_hold")
  runner.state="hold"; runner.hold_reason="nav_hold"; runner.odo=100; ctl._watch_stop()
  ctl._handle(json.dumps(order(2,"B")).encode())
  self.assertIsNotNone(ctl.rejected); self.assertEqual(ctl.book.current["seq"],1)
  self.assertEqual(runner.resumes,1)
 def test_team_map_out_and_back_in_separate_confirmed_segments(self):
  root=os.path.abspath(os.path.join(os.path.dirname(__file__),"..",".."))
  with io.open(os.path.join(root,"maps","sim_ssafy_v2.json"),encoding="utf-8") as f: raw=json.load(f)
  nav=Navigator(NavMap(raw),start="P_orange",hold_at_end=True)
  runner=Runner(); link=FakeLink("sim-r1"); ctl=LinkController(link,nav,raw,start_node="P_orange",waiting="all"); ctl.attach(runner)
  route=["P_orange","S1","M1","P_purple","M1","S1","P_orange"]
  def msg(seq,release): return json.dumps({"order_id":"T2","seq":seq,"map_id":ctl.book.map_ref,"nodes":route,"released_until":release,"action_at_end":{"type":"none"}}).encode()
  ctl._handle(msg(1,"P_purple")); self.assertIsNone(ctl.rejected)
  nav.on_patch("orange",0); nav.pop_stop(); side=nav.choose_split(370); self.assertEqual(side,"left")
  nav.on_split(side,470); nav.on_merge(820); nav.on_patch("purple",860)
  self.assertEqual(nav.pop_stop()["reason"],"nav_hold")
  runner.state="hold"; runner.hold_reason="nav_hold"; runner.odo=860; ctl._watch_stop()
  ctl._handle(msg(2,"P_orange")); self.assertIsNone(ctl.rejected)
  turn=nav.pop_stop(); self.assertEqual(turn["type"],"uturn")
  nav.resumed(); nav.uturned(940); nav.on_split("right",970); nav.on_merge(1320)
  nav.on_patch("orange",1700); stop=nav.pop_stop()
  self.assertEqual((stop["node"],stop["reason"]),("P_orange","nav_hold"))
  self.assertEqual(nav._route_pos,6)
 def test_unknown_split_does_not_choose_a_default_branch(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  nav.hyps=[]
  self.assertIsNone(nav.choose_split(0))
  self.assertEqual(nav.pop_stop()["reason"],"nav_off_route")
 def test_unconfirmed_early_and_unexpected_patch_do_not_fake_arrival(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  nav.on_patch_early("red",0); self.assertIsNone(nav.pop_stop()); self.assertIsNone(nav.at_node)
  nav.on_patch("blue",10); stop=nav.pop_stop()
  self.assertEqual((stop["reason"],stop["hold"]),("nav_off_route",True)); self.assertIsNone(nav.at_node)
 def test_unknown_start_direction_is_rejected_without_mutation(self):
  nav,runner,link,ctl=self.fresh(); nav.hyps=[]; ctl._handle(json.dumps(order(1,"C")).encode())
  self.assertIsNotNone(ctl.rejected); self.assertEqual(runner.resumes,0); self.assertIsNone(nav.route)
 def test_incompatible_crossing_direction_is_rejected(self):
  data=json.loads(json.dumps(DATA)); data["nodes"][2].update(crossing=True,through=[["E1","E3"]])
  nav=Navigator(NavMap(data),start="A")
  with self.assertRaises(ValueError): nav.set_route(ROUTE,release="C")
 def test_disconnected_stop_event_does_not_replay_after_reconnect(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  nav.on_patch("orange",0); nav.pop_stop(); nav.on_merge(100); nav.pop_stop(); nav.on_patch("red",200); nav.pop_stop()
  runner.state="hold"; runner.hold_reason="nav_hold"; link.connected=False; ctl._watch_stop()
  self.assertEqual(ctl.error,"event_unconfirmed"); self.assertEqual(link.events,[])
  link.connected=True; ctl._watch_stop(); self.assertEqual(link.events,[])
  ctl._handle(json.dumps(order(2,"D")).encode()); self.assertIsNotNone(ctl.rejected); self.assertEqual(ctl.book.current["seq"],1)
 def test_failed_queue_acceptance_blocks_extension(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  nav.on_patch("orange",0); nav.pop_stop(); nav.on_merge(100); nav.pop_stop(); nav.on_patch("red",200); nav.pop_stop()
  runner.state="hold"; runner.hold_reason="nav_hold"; link.publish_event=lambda event: False; ctl._watch_stop()
  self.assertEqual(ctl.error,"event_unconfirmed")
  ctl._handle(json.dumps(order(2,"D")).encode()); self.assertIsNotNone(ctl.rejected); self.assertEqual(ctl.book.current["seq"],1)
 def test_event_publish_exception_is_not_retried(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  nav.on_patch("orange",0); nav.pop_stop(); nav.on_merge(100); nav.pop_stop(); nav.on_patch("red",200); nav.pop_stop()
  runner.state="hold"; runner.hold_reason="nav_hold"
  def fail(_event): raise RuntimeError("send failure")
  link.publish_event=fail; ctl._watch_stop(); self.assertEqual(ctl.error,"event_unconfirmed")
  link.publish_event=lambda event: self.fail("event must not be retried"); ctl._watch_stop()
 def test_reconnect_does_not_auto_resume_even_if_enabled(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  ctl.resume_after_reconnect=True; ctl.error="link_lost"; runner.state="hold"; runner.hold_reason="link_lost"
  resumes=runner.resumes; ctl._watch_link(100)
  self.assertEqual(ctl.error,"link_lost"); self.assertEqual(runner.resumes,resumes)
 def test_unexpected_dead_end_stops_repeat_sequence(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  nav.on_dead_end("left",100); stop=nav.pop_stop()
  self.assertIsNotNone(stop); self.assertEqual((stop["reason"],stop["hold"]),("nav_off_route",True))
  self.assertIsNone(nav.at_node); self.assertEqual(nav._route_pos,0)
 def test_disconnect_discards_queued_and_already_polled_orders(self):
  class Client(object):
   def publish(self,*args,**kwargs): pass
   def subscribe(self,*args,**kwargs): pass
  class Msg(object):
   topic="agv/sim-r1/order"; retain=False; payload=json.dumps(order(1,"C")).encode()
  link=MqttLink("sim-r1")
  client=Client(); link._on_connect(client,None,None,0)
  link._on_message(client,None,Msg()); polled=link.poll_with_generation()
  self.assertEqual(len(polled),1)
  link._on_message(client,None,Msg()); link._on_disconnect(client,None,1)
  link._on_connect(client,None,None,0)
  self.assertEqual(link.poll(),[])
  seen=[]; self.assertFalse(link.process_if_current(polled[0][0],polled[0][1],seen.append)); self.assertEqual(seen,[])
 def test_unconfirmed_intermediate_does_not_advance_cursor(self):
  nav,runner,link,ctl=self.fresh(); ctl._handle(json.dumps(order(1,"C")).encode())
  nav.on_patch("orange",0); nav.pop_stop(); nav.on_patch("red",200)
  stop=nav.pop_stop(); self.assertEqual((stop["reason"],stop["hold"]),("nav_off_route",True))
  self.assertLess(nav._route_pos,2); self.assertNotEqual(nav.at_node,"C")
 def test_disconnected_queued_initial_order_does_not_start(self):
  nav,runner,link,ctl=self.fresh(); link.send(order(1,"C")); link.connected=False; link.disconnected_at=100
  ctl.step(now=100); self.assertEqual(runner.resumes,0); self.assertIsNone(ctl.book.current); self.assertEqual(runner.state,"hold")

class SequenceMatTests(TestSimServe):
 def test_team_mat_revisit_completes_or_safely_stops_on_missed_observation(self):
  route=["P_orange","S1","M1","P_purple","M1","S1","P_orange"]
  for seed in (2,3,5):
   pending={0:self._o(1,"P_purple",route),1:self._o(2,"P_orange",route)}; sent=set()
   def script(link,ctl,runner,t,state):
    n=len(link.events)
    if n in pending and n not in sent and (n==0 or state["status"]=="WAITING"):
     sent.add(n); link.send(pending[n])
    if n==2 and state["status"]=="IDLE": runner.stop("test_done")
    if n==1 and state["status"]=="ERROR" and ctl.error=="nav_off_route": runner.stop("expected_hold")
   link,ctl,notes,gt=self._run(script,seed=seed,max_time=120)
   if gt["finish_reason"] not in ("test_done","expected_hold"):
    with io.open(os.path.join(self.tmp,"events.jsonl"),encoding="utf-8") as f:
     tail=[x.strip() for x in f.readlines() if "nav_sequence_ambiguous" in x or "nav_off_route" in x or '"type": "' in x][-12:]
    self.fail(repr((seed,[x for x in notes if x[0] != "link_order"],tail)))
   observed=[(e["seq"],e["stopped_at"]) for e in link.events]
   if gt["finish_reason"] == "expected_hold":
    self.assertEqual(observed,[(1,"P_purple")]); self.assertEqual(ctl.error,"nav_off_route")
   else:
    self.assertEqual(observed,[(1,"P_purple"),(2,"P_orange")])

if __name__=="__main__": unittest.main()
