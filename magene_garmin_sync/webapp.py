"""Web control panel for magene-garmin-sync.

A small threaded HTTP server (stdlib only) that serves one page:
  * forms to enter and save Onelap + Garmin credentials (stored on the server,
    never echoed back),
  * Test / Sync-now buttons and an auto-sync toggle,
  * a live log and last-run summary.

Bind it only on your LAN. It is an unauthenticated control panel for your own
account data; do not expose the port to the internet.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .config import Config
from . import engine


class AppState:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self.lock = threading.Lock()         # guards cfg + one-op-at-a-time
        self.busy: str | None = None          # name of running op, or None
        self.logs: deque[str] = deque(maxlen=400)
        self.last_sync: dict | None = None
        self.last_probe: dict | None = None
        self._stop = threading.Event()

    def log(self, line: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.logs.append(f"{stamp}  {line}")

    def snapshot(self) -> dict:
        return {
            "version": __version__,
            "config": self.cfg.redacted(),
            "busy": self.busy,
            "logs": list(self.logs),
            "last_sync": self.last_sync,
            "last_probe": self.last_probe,
        }

    # run an engine op in a worker thread, guarded so only one runs at a time
    def run_async(self, name: str, fn) -> bool:
        with self.lock:
            if self.busy:
                return False
            self.busy = name

        def worker():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self.log(f"{name} crashed: {exc}")
            finally:
                with self.lock:
                    self.busy = None

        threading.Thread(target=worker, daemon=True).start()
        return True

    def do_probe(self) -> None:
        self.log("── probe started ──")
        result = engine.probe(self.cfg, self.log)
        self.last_probe = result
        if result.get("ok"):
            self.cfg.save()  # persist discovered host/hash_password
            self.log("probe ok; host/auth saved.")

    def do_sync(self) -> None:
        self.log("── sync started ──")
        result = engine.sync_once(self.cfg, self.log)
        result["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.last_sync = result
        self.log(f"sync done: {result}")

    def scheduler(self) -> None:
        """Background loop: when auto_sync is on, sync every poll_seconds."""
        next_run = 0.0
        while not self._stop.is_set():
            if self.cfg.auto_sync and time.time() >= next_run and not self.busy:
                self.run_async("auto-sync", self.do_sync)
                next_run = time.time() + max(300, self.cfg.poll_seconds)
            self._stop.wait(5)


STATE = AppState()


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Magene &rarr; Garmin Sync</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e9ef;--mut:#8b93a7;
--acc:#f26722;--ok:#3fb950;--bad:#f85149;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media(prefers-color-scheme:light){:root{--bg:#f3f4f7;--card:#fff;--line:#e2e5ec;
--fg:#1b1f27;--mut:#5b6472}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:20px;margin:0 0 2px;display:flex;align-items:center;gap:10px}
h1 .dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}
h1 .dot.on{background:var(--ok)}h1 .dot.busy{background:var(--acc);animation:p 1s infinite}
@keyframes p{50%{opacity:.3}}
.sub{color:var(--mut);margin:0 0 20px;font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:14px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px}
input[type=text],input[type=password],input[type=email],input[type=number]{width:100%;
background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:8px;
padding:9px 10px;font-size:14px}
.row{display:flex;gap:10px}.row>div{flex:1}
.chk{display:flex;align-items:center;gap:8px;margin-top:12px;color:var(--fg);font-size:13px}
.btns{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 16px;
font-size:14px;font-weight:600;cursor:pointer}button.ghost{background:transparent;
border:1px solid var(--line);color:var(--fg)}button:disabled{opacity:.5;cursor:not-allowed}
.pill{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.pill.set{color:var(--ok);border-color:var(--ok)}
.log{background:#0b0d11;border:1px solid var(--line);border-radius:10px;padding:12px;
font:12px/1.5 var(--mono);color:#cdd3df;height:260px;overflow:auto;white-space:pre-wrap}
.sum{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0 0;font-size:13px}
.sum b{color:var(--fg)}.warn{color:var(--acc);font-size:12px;margin-top:8px}
a{color:var(--acc)}
</style></head><body><div class="wrap">
<h1><span id="dot" class="dot"></span>Magene &rarr; Garmin Sync</h1>
<p class="sub">Pulls rides from your Magene / OnelapFit cloud and uploads them to Garmin Connect.
Credentials are stored on this server only. <span id="ver"></span></p>

<div class="grid">
  <div class="card">
    <h2>Onelap / Magene <span id="ol-pill" class="pill">password not set</span></h2>
    <label>Account (email or phone)</label>
    <input id="onelap_account" type="text" autocomplete="off">
    <label>Password <span style="color:var(--mut)">(blank = keep current)</span></label>
    <input id="onelap_password" type="password" autocomplete="new-password" placeholder="••••••••">
    <label>Server host <span style="color:var(--mut)">(auto-found by Test)</span></label>
    <input id="onelap_host" type="text" placeholder="found automatically">
    <label class="chk"><input id="onelap_hash_password" type="checkbox"> Hash password (set by Test)</label>
  </div>
  <div class="card">
    <h2>Garmin Connect <span id="gc-pill" class="pill">password not set</span></h2>
    <label>Email</label>
    <input id="garmin_email" type="email" autocomplete="off">
    <label>Password <span style="color:var(--mut)">(blank = keep current)</span></label>
    <input id="garmin_password" type="password" autocomplete="new-password" placeholder="••••••••">
    <label>2FA code <span style="color:var(--mut)">(only if your account uses it)</span></label>
    <input id="garmin_mfa" type="text" autocomplete="off" placeholder="optional">
  </div>
</div>

<div class="card" style="margin-top:16px">
  <div class="row">
    <div><label>Sync every (seconds)</label><input id="poll_seconds" type="number" min="300" value="3600"></div>
    <div><label>Ride-list pages to scan</label><input id="lookback_pages" type="number" min="1" value="1"></div>
  </div>
  <label class="chk"><input id="auto_sync" type="checkbox"> Auto-sync on the schedule above</label>
</div>

<div class="btns">
  <button id="save" class="ghost">Save settings</button>
  <button id="probe">Test Onelap login</button>
  <button id="sync">Sync now</button>
</div>

<div class="card">
  <h2>Status</h2>
  <div class="sum" id="sum"><span class="mut">No sync yet.</span></div>
  <div class="warn" id="warn"></div>
  <label style="margin-top:14px">Live log</label>
  <div class="log" id="log"></div>
</div>
</div>

<script>
const $=id=>document.getElementById(id);
const FIELDS=["onelap_account","onelap_host","garmin_email","garmin_mfa","poll_seconds","lookback_pages"];
const CHECKS=["onelap_hash_password","auto_sync"];
let dirtyGuard=false;

function collect(){
  const o={};
  FIELDS.forEach(f=>o[f]=$(f).value);
  CHECKS.forEach(f=>o[f]=$(f).checked);
  // only send passwords if the user typed something
  ["onelap_password","garmin_password"].forEach(f=>{if($(f).value)o[f]=$(f).value;});
  return o;
}
async function post(path,body){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body||{})});
  return r.json();
}
function applyConfig(c){
  if(dirtyGuard)return;
  FIELDS.forEach(f=>{if(document.activeElement!==$(f)&&c[f]!=null)$(f).value=c[f];});
  CHECKS.forEach(f=>{if(document.activeElement!==$(f))$(f).checked=!!c[f];});
  $("ol-pill").textContent=c.onelap_password_set?"password set":"password not set";
  $("ol-pill").className="pill"+(c.onelap_password_set?" set":"");
  $("gc-pill").textContent=c.garmin_password_set?"password set":"password not set";
  $("gc-pill").className="pill"+(c.garmin_password_set?" set":"");
}
function render(s){
  $("ver").textContent="v"+s.version;
  applyConfig(s.config);
  const dot=$("dot");
  dot.className="dot"+(s.busy?" busy":(s.config.onelap_password_set&&s.config.garmin_password_set?" on":""));
  ["save","probe","sync"].forEach(b=>$(b).disabled=!!s.busy);
  const log=$("log");const atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-20;
  log.textContent=(s.logs||[]).join("\\n");
  if(atBottom)log.scrollTop=log.scrollHeight;
  const sum=$("sum");
  if(s.last_sync){const x=s.last_sync;
    sum.innerHTML=`<span>listed <b>${x.listed??0}</b></span><span>new <b>${x.new??0}</b></span>`+
      `<span>uploaded <b>${x.uploaded??0}</b></span><span>duplicate <b>${x.duplicate??0}</b></span>`+
      `<span>skipped <b>${x.skipped??0}</b></span><span>failed <b>${x.failed??0}</b></span>`+
      (x.at?`<span class=mut>at ${x.at.replace("T"," ").replace("+00:00"," UTC")}</span>`:"");
  }
  $("warn").textContent = s.last_probe&&!s.last_probe.ok ? ("Last test: "+(s.last_probe.error||"")) :
    (s.last_sync&&s.last_sync.error? ("Last sync: "+s.last_sync.error):"");
}
async function refresh(){try{render(await (await fetch("/api/status")).json());}catch(e){}}
$("save").onclick=async()=>{dirtyGuard=true;await post("/api/config",collect());
  ["onelap_password","garmin_password"].forEach(f=>$(f).value="");dirtyGuard=false;refresh();};
$("probe").onclick=async()=>{dirtyGuard=true;await post("/api/config",collect());
  ["onelap_password","garmin_password"].forEach(f=>$(f).value="");dirtyGuard=false;
  await post("/api/probe");refresh();};
$("sync").onclick=async()=>{dirtyGuard=true;await post("/api/config",collect());
  ["onelap_password","garmin_password"].forEach(f=>$(f).value="");dirtyGuard=false;
  await post("/api/sync");refresh();};
refresh();setInterval(refresh,1500);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "magene-garmin-sync"

    def log_message(self, *args):  # silence default stderr noise
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            with STATE.lock:
                snap = STATE.snapshot()
            self._json(snap)
        elif self.path == "/healthz":
            self._json({"ok": True})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/config":
            changes = self._read_json()
            with STATE.lock:
                STATE.cfg.update(changes)
                STATE.cfg.save()
            STATE.log("settings saved.")
            self._json({"ok": True})
        elif self.path == "/api/probe":
            started = STATE.run_async("probe", STATE.do_probe)
            self._json({"ok": started, "busy": STATE.busy})
        elif self.path == "/api/sync":
            started = STATE.run_async("sync", STATE.do_sync)
            self._json({"ok": started, "busy": STATE.busy})
        else:
            self._send(404, b"not found", "text/plain")


def serve(host: str = "0.0.0.0", port: int = 8787) -> None:
    threading.Thread(target=STATE.scheduler, daemon=True).start()
    STATE.log(f"control panel on http://{host}:{port}")
    if STATE.cfg.auto_sync:
        STATE.log("auto-sync is ON")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"magene-garmin-sync web UI listening on http://{host}:{port}")
    httpd.serve_forever()
