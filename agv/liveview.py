# -*- coding: utf-8 -*-
"""주행 중 검출 화면을 브라우저로 실시간으로 본다(MJPEG). 추가 설치 없이 표준 라이브러리만 쓴다.

    python3 -m agv.explore --mode passive --view 8080
    → 같은 네트워크의 노트북·폰 브라우저에서 http://<젯슨 IP>:8080

- /        화면(검출 그림) + 상태 + 최근 이벤트 + 정지 버튼
- /stream  MJPEG 영상(다른 프로그램에서 열 때. 예: VLC, OpenCV VideoCapture)
- /snap.jpg  지금 한 장
- /status  상태 JSON
- /stop    (POST) 모터 정지·주행 종료. Ctrl+C와 같다

보는 사람이 없으면 JPEG 인코딩을 하지 않으므로 주행 성능에 거의 영향이 없다.
"""
import collections
import json
import socket
import threading
import time

try:  # Python 3
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:  # pragma: no cover - Python 2
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn

import cv2

PAGE = u"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AGV 실시간 화면</title>
<style>
:root{--bg:#f6f7f9;--fg:#1d2330;--mut:#6b7380;--card:#fff;--line:#e2e5ea;--red:#c62828;--hi:#1f6feb}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e6e8eb;--mut:#9aa1ab;--card:#1d2026;--line:#2c3038}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
.wrap{display:flex;flex-wrap:wrap;gap:12px;padding:12px}
.cam{flex:1 1 560px;max-width:960px}
.cam img{width:100%;background:#000;border-radius:8px;display:block}
.side{flex:1 1 300px;min-width:280px;display:flex;flex-direction:column;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
h2{font-size:12px;color:var(--mut);margin:0 0 6px;font-weight:600}
table{border-collapse:collapse;width:100%}td{padding:2px 4px;vertical-align:top}
td:first-child{color:var(--mut);width:84px}
#ev{font:12px/1.5 ui-monospace,monospace;max-height:340px;overflow:auto;white-space:pre}
#ev .hi{color:var(--hi);font-weight:600}
button{font:inherit;font-weight:700;color:#fff;background:var(--red);border:0;border-radius:8px;padding:12px;width:100%;cursor:pointer}
button:disabled{opacity:.5}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--line)}
.off{color:var(--red)}
</style></head><body>
<div class="wrap">
 <div class="cam"><img id="cam" src="stream" alt="검출 화면"></div>
 <div class="side">
  <div class="card"><button id="stop">정지 (모터 끄고 주행 종료)</button></div>
  <div class="card"><h2>상태</h2><table id="st"></table></div>
  <div class="card"><h2>최근 이벤트</h2><div id="ev"></div></div>
 </div>
</div>
<script>
var HI={dead_end:1,split:1,cross:1,tee:1,end:1};
function row(k,v){return "<tr><td>"+k+"</td><td>"+v+"</td></tr>";}
function esc(s){return String(s).replace(/[&<>]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c];});}
function tick(){
 fetch("status",{cache:"no-store"}).then(function(r){return r.json();}).then(function(s){
  var h="";
  for(var i=0;i<s.rows.length;i++)h+=row(esc(s.rows[i][0]),esc(s.rows[i][1]));
  if(s.finished)h+=row("종료","<span class='off'>"+esc(s.finished)+"</span>");
  document.getElementById("st").innerHTML=h;
  var e="";
  for(var j=s.events.length-1;j>=0;j--){var x=s.events[j];
   e+="<div"+(HI[x.type]||(x.msg||"").indexOf("vee")==0?" class='hi'":"")+">"+esc(x.line)+"</div>";}
  document.getElementById("ev").innerHTML=e;
  document.getElementById("stop").disabled=!!s.finished;
 }).catch(function(){
  document.getElementById("st").innerHTML=row("연결","<span class='off'>끊김 (로봇 프로그램이 끝났거나 네트워크 문제)</span>");
 });
}
setInterval(tick,500);tick();
document.getElementById("stop").onclick=function(){
 fetch("stop",{method:"POST"}).then(tick);
};
document.getElementById("cam").onerror=function(){var im=this;setTimeout(function(){im.src="stream?"+Date.now();},1000);};
</script></body></html>
"""


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LiveView(object):
    """마지막 그림 한 장과 상태를 들고 있다가 접속한 브라우저에 보내 준다."""

    def __init__(self, port=8080, host="0.0.0.0", fps=10.0, quality=70, scale=2):
        self.fps = fps
        self.quality = quality
        self.scale = scale
        self.clients = 0
        self.stop_requested = False
        self._jpg = None
        self._seq = 0
        self._cond = threading.Condition()
        self._last_pub = 0.0
        self._status = {"rows": [], "finished": None}
        self._events = collections.deque(maxlen=40)
        self._lock = threading.Lock()
        view = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):   # 터미널을 어지럽히지 않음
                pass

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
                elif path == "/status":
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps(view.status(), ensure_ascii=False).encode("utf-8"))
                elif path == "/snap.jpg":
                    view.clients += 1
                    try:
                        jpg = view.wait_frame(-1, 2.0)[1]
                    finally:
                        view.clients -= 1
                    if jpg is None:
                        self._send(503, "text/plain", b"no frame")
                    else:
                        self._send(200, "image/jpeg", jpg)
                elif path == "/stream":
                    self._stream()
                else:
                    self._send(404, "text/plain", b"not found")

            def do_POST(self):
                if self.path.split("?")[0] == "/stop":
                    view.stop_requested = True
                    self._send(200, "application/json", b'{"ok": true}')
                else:
                    self._send(404, "text/plain", b"not found")

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                view.clients += 1
                seq = -1
                try:
                    while True:
                        seq, jpg = view.wait_frame(seq, 5.0)
                        if jpg is None:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                except (socket.error, IOError, ValueError):
                    pass
                finally:
                    view.clients -= 1

        self.httpd = _Server((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self._th = threading.Thread(target=self.httpd.serve_forever)
        self._th.daemon = True
        self._th.start()

    # ---------- 주행 루프에서 부름 ----------
    def want(self):
        """지금 그림을 만들어 올릴 차례인지(보는 사람이 있고 fps 간격이 지났는지)."""
        return self.clients > 0 and time.time() - self._last_pub >= 1.0 / self.fps

    def publish(self, bgr):
        """그림을 올린다. 보는 사람이 없거나 fps보다 자주 부르면 그냥 넘어간다(인코딩 안 함)."""
        now = time.time()
        if self.clients <= 0 or now - self._last_pub < 1.0 / self.fps:
            return
        self._last_pub = now
        if self.scale and self.scale != 1:
            bgr = cv2.resize(bgr, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_LINEAR)
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
        if not ok:
            return
        with self._cond:
            self._jpg = buf.tobytes()
            self._seq += 1
            self._cond.notify_all()

    def set_status(self, rows, finished=None):
        """rows: [(이름, 값), ...] 화면 오른쪽 상태 표."""
        with self._lock:
            self._status = {"rows": [[k, v] for k, v in rows], "finished": finished}

    def add_event(self, ev, t0=None):
        """이벤트 로그 한 줄을 최근 목록에 넣는다."""
        skip = ("ts", "seq", "type", "odo_mm", "msg")
        rest = u" ".join(u"%s=%s" % (k, v) for k, v in ev.items()
                         if k not in skip and v is not None and not isinstance(v, (dict, list)))
        t = ev.get("ts")
        line = u"%6.1fs %7s %-8s %s%s" % (
            (t - t0) if (t is not None and t0 is not None) else 0.0,
            u"-" if ev.get("odo_mm") is None else u"%.0f" % ev["odo_mm"],
            ev.get("type", u""), (ev.get("msg") + u" ") if ev.get("msg") else u"", rest)
        with self._lock:
            self._events.append({"type": ev.get("type"), "msg": ev.get("msg"), "line": line})

    def status(self):
        with self._lock:
            s = dict(self._status)
            s["events"] = list(self._events)
        return s

    def wait_frame(self, last_seq, timeout):
        with self._cond:
            if self._seq == last_seq or self._jpg is None:
                self._cond.wait(timeout)
            return self._seq, self._jpg

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def local_ips():
    """접속 주소 안내용: 이 기기의 IPv4 주소들(대략)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))     # 실제로 보내지는 않음. 기본 경로의 내 주소만 얻는다
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips or ["<젯슨 IP>"]
