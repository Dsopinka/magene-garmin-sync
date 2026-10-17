"""Web control panel for the Strava -> Garmin bridge.

A small threaded HTTP server (stdlib only). One page: enter your Strava API app
credentials and Garmin login, connect Strava with one click (OAuth), pick which
rides to sync, and watch it run. Credentials/tokens live on this server only.

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
from urllib.parse import parse_qs, urlparse

from . import __version__, engine
from .config import Config
from .strava import StravaClient, build_authorize_url


class AppState:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self.lock = threading.Lock()
        self.busy: str | None = None
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
        self.log("── test started ──")
        self.last_probe = engine.probe(self.cfg, self.log)

    def do_sync(self) -> None:
        self.log("── sync started ──")
        result = engine.sync_once(self.cfg, self.log)
        result["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.last_sync = result

    def scheduler(self) -> None:
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
--acc:#fc5200;--ok:#3fb950;--bad:#f85149;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media(prefers-color-scheme:light){:root{--bg:#f3f4f7;--card:#fff;--line:#e2e5ec;--fg:#1b1f27;--mut:#5b6472}}
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
background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:9px 10px;font-size:14px}
.row{display:flex;gap:10px}.row>div{flex:1}
.chk{display:flex;align-items:center;gap:8px;margin-top:12px;color:var(--fg);font-size:13px}
.btns{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}
button,a.btn{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 16px;
font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
button.ghost,a.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
button:disabled{opacity:.5;cursor:not-allowed}
.pill{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.pill.set{color:var(--ok);border-color:var(--ok)}
.hint{font-size:12px;color:var(--mut);margin-top:10px}
.log{background:#0b0d11;border:1px solid var(--line);border-radius:10px;padding:12px;
font:12px/1.5 var(--mono);color:#cdd3df;height:260px;overflow:auto;white-space:pre-wrap}
.sum{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0 0;font-size:13px}
.sum b{color:var(--fg)}.warn{color:var(--acc);font-size:12px;margin-top:8px}
a{color:var(--acc)}
</style></head><body><div class="wrap">
<h1><span id="dot" class="dot"></span>Magene &rarr; Garmin Sync</h1>
<p class="sub">Reads your rides from Strava (where your Magene rides already sync) and uploads them to
Garmin Connect. Everything is stored on this server only. <span id="ver"></span></p>

<div class="grid">
  <div class="card">
    <h2>Strava <span id="st-pill" class="pill">not connected</span></h2>
    <label>Client ID</label>
    <input id="strava_client_id" type="text" autocomplete="off" placeholder="from strava.com/settings/api">
    <label>Client Secret <span style="color:var(--mut)">(blank = keep current)</span></label>
    <input id="strava_client_secret" type="password" autocomplete="new-password" placeholder="••••••••">
    <div class="btns" style="margin:14px 0 0">
      <button id="connect">Connect Strava</button>
    </div>
    <div class="hint">One-click Connect works if your app's <b>Authorization Callback Domain</b> is this
    server's address (shown at the bottom). Already have a Strava app for something else? Use the manual connect below &mdash; it won't touch that app's callback.</div>
    <details style="margin-top:10px">
      <summary style="cursor:pointer;color:var(--mut);font-size:13px">Connect manually (existing app / no-touch)</summary>
      <label>Your app's redirect URI <span style="color:var(--mut)">(must match its callback domain)</span></label>
      <input id="manual_redirect" type="text" placeholder="e.g. http://localhost or your app's callback URL">
      <div class="hint"><a id="authlink" href="#" target="_blank" rel="noopener">1. Open Strava authorization &rarr;</a>
      &nbsp;approve, then copy the <code>code</code> value from the address bar you land on.</div>
      <label>2. Paste the authorization code</label>
      <div class="row"><div><input id="manual_code" type="text" placeholder="code from the URL"></div>
        <div style="flex:0 0 auto"><button id="exchange">Exchange</button></div></div>
      <label style="margin-top:10px">or paste a refresh token <span style="color:var(--mut)">(scope activity:read_all)</span></label>
      <div class="row"><div><input id="manual_token" type="password" placeholder="refresh token"></div>
        <div style="flex:0 0 auto"><button id="usetoken" class="ghost">Use token</button></div></div>
    </details>
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
  <h2>What to sync</h2>
  <label>Device filter <span style="color:var(--mut)">(match on Strava device name; blank = all)</span></label>
  <input id="device_filter" type="text" placeholder="e.g. Magene">
  <label class="chk"><input id="rides_only" type="checkbox"> Cycling activities only</label>
  <label class="chk"><input id="include_virtual" type="checkbox"> Include virtual rides (Zwift)</label>
  <div class="row" style="margin-top:12px">
    <div><label>Sync every (seconds)</label><input id="poll_seconds" type="number" min="300" value="3600"></div>
    <div><label>Activities to scan</label><input id="scan_count" type="number" min="1" value="30"></div>
  </div>
  <label class="chk"><input id="auto_sync" type="checkbox"> Auto-sync on the schedule above</label>
</div>

<div class="btns">
  <button id="save" class="ghost">Save settings</button>
  <button id="probe">Test Strava</button>
  <button id="sync">Sync now</button>
</div>

<div class="card">
  <h2>Status</h2>
  <div class="sum" id="sum"><span class="mut">No sync yet.</span></div>
  <div class="warn" id="warn"></div>
  <label style="margin-top:14px">Live log</label>
  <div class="log" id="log"></div>
  <div class="hint">This server's callback address: <b id="cbaddr"></b></div>
</div>
</div>

<script>
const $=id=>document.getElementById(id);
const TEXT=["strava_client_id","garmin_email","garmin_mfa","device_filter","poll_seconds","scan_count"];
const CHECKS=["rides_only","include_virtual","auto_sync"];
let loaded=false;
function collect(){
  const o={};
  TEXT.forEach(f=>o[f]=$(f).value);
  CHECKS.forEach(f=>o[f]=$(f).checked);
  ["strava_client_secret","garmin_password"].forEach(f=>{if($(f).value)o[f]=$(f).value;});
  return o;
}
async function post(path,body){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
  return r.json();
}
function fillAll(c){
  TEXT.forEach(f=>{if(c[f]!=null)$(f).value=c[f];});
  CHECKS.forEach(f=>{$(f).checked=!!c[f];});
}
function live(s){
  const c=s.config;
  $("ver").textContent="v"+s.version;
  $("st-pill").textContent=c.strava_connected?"connected":"not connected";
  $("st-pill").className="pill"+(c.strava_connected?" set":"");
  $("gc-pill").textContent=c.garmin_password_set?"password set":"password not set";
  $("gc-pill").className="pill"+(c.garmin_password_set?" set":"");
  const dot=$("dot");
  dot.className="dot"+(s.busy?" busy":((c.strava_connected&&c.garmin_password_set)?" on":""));
  ["save","probe","sync","connect","exchange","usetoken"].forEach(b=>$(b).disabled=!!s.busy);
  updateAuthLink();
  $("cbaddr").textContent=location.protocol+"//"+location.host+"/strava/callback  (callback domain: "+location.hostname+")";
  const log=$("log");const atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-20;
  log.textContent=(s.logs||[]).join("\\n");
  if(atBottom)log.scrollTop=log.scrollHeight;
  const sum=$("sum");
  if(s.last_sync){const x=s.last_sync;
    sum.innerHTML=`<span>scanned <b>${x.listed??0}</b></span><span>new <b>${x.new??0}</b></span>`+
      `<span>uploaded <b>${x.uploaded??0}</b></span><span>duplicate <b>${x.duplicate??0}</b></span>`+
      `<span>skipped <b>${x.skipped??0}</b></span><span>failed <b>${x.failed??0}</b></span>`+
      (x.at?`<span class=mut>at ${x.at.replace("T"," ").replace("+00:00"," UTC")}</span>`:"");
  }
  $("warn").textContent = s.last_probe&&!s.last_probe.ok ? ("Last test: "+(s.last_probe.error||"")) :
    (s.last_sync&&s.last_sync.error? ("Last sync: "+s.last_sync.error):"");
}
async function refresh(){
  try{const s=await (await fetch("/api/status")).json();
    if(!loaded){fillAll(s.config);loaded=true;}
    live(s);}catch(e){}
}
async function saveThen(path){
  await post("/api/config",collect());
  ["strava_client_secret","garmin_password"].forEach(f=>$(f).value="");
  if(path)await post(path);
  refresh();
}
$("save").onclick=()=>saveThen(null);
$("probe").onclick=()=>saveThen("/api/probe");
$("sync").onclick=()=>saveThen("/api/sync");
$("connect").onclick=async()=>{await post("/api/config",collect());
  $("strava_client_secret").value="";window.location="/strava/connect";};
function updateAuthLink(){
  const cid=$("strava_client_id").value.trim();
  const ruri=$("manual_redirect").value.trim();
  const a=$("authlink");
  if(cid&&ruri){
    a.href="https://www.strava.com/oauth/authorize?client_id="+encodeURIComponent(cid)+
      "&redirect_uri="+encodeURIComponent(ruri)+"&response_type=code&scope=activity:read_all&approval_prompt=force";
    a.style.opacity="1";a.style.pointerEvents="auto";
  }else{a.href="#";a.style.opacity=".5";}
}
$("strava_client_id").addEventListener("input",updateAuthLink);
$("manual_redirect").addEventListener("input",updateAuthLink);
$("exchange").onclick=async()=>{
  const code=$("manual_code").value.trim(); if(!code)return;
  await post("/api/config",collect()); $("strava_client_secret").value="";
  await post("/api/strava/exchange",{code}); $("manual_code").value=""; refresh();
};
$("usetoken").onclick=async()=>{
  const t=$("manual_token").value.trim(); if(!t)return;
  await post("/api/config",collect()); $("strava_client_secret").value="";
  await post("/api/strava/token",{refresh_token:t}); $("manual_token").value=""; refresh();
};
refresh();setInterval(refresh,1500);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "magene-garmin-sync"

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _redirect(self, location: str) -> None:
        self._send(302, b"", "text/plain", {"Location": location})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def _redirect_uri(self) -> str:
        host = self.headers.get("Host") or "localhost:8787"
        return f"http://{host}/strava/callback"

    def do_GET(self):
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif route.path == "/api/status":
            with STATE.lock:
                snap = STATE.snapshot()
            self._json(snap)
        elif route.path == "/strava/connect":
            if not STATE.cfg.strava_ready:
                STATE.log("Connect: set Strava Client ID and Secret first, then Save.")
                return self._redirect("/")
            url = build_authorize_url(STATE.cfg.strava_client_id, self._redirect_uri())
            self._redirect(url)
        elif route.path == "/strava/callback":
            self._strava_callback(parse_qs(route.query))
        elif route.path == "/healthz":
            self._json({"ok": True})
        else:
            self._send(404, b"not found", "text/plain")

    def _strava_callback(self, q: dict) -> None:
        if q.get("error"):
            STATE.log(f"Strava authorization denied: {q['error'][0]}")
            return self._redirect("/")
        code = (q.get("code") or [""])[0]
        if not code:
            STATE.log("Strava callback missing code.")
            return self._redirect("/")
        try:
            tokens = StravaClient.exchange_code(
                STATE.cfg.strava_client_id, STATE.cfg.strava_client_secret, code
            )
            STATE.cfg.set_strava_tokens(tokens)
            STATE.log("Strava connected.")
        except Exception as exc:  # noqa: BLE001
            STATE.log(f"Strava connect failed: {exc}")
        self._redirect("/")

    def do_POST(self):
        if self.path == "/api/config":
            with STATE.lock:
                STATE.cfg.update(self._read_json())
                STATE.cfg.save()
            STATE.log("settings saved.")
            self._json({"ok": True})
        elif self.path == "/api/probe":
            self._json({"ok": STATE.run_async("probe", STATE.do_probe)})
        elif self.path == "/api/sync":
            self._json({"ok": STATE.run_async("sync", STATE.do_sync)})
        elif self.path == "/api/strava/exchange":
            code = str(self._read_json().get("code") or "").strip()
            if not STATE.cfg.strava_ready:
                STATE.log("Exchange: set Strava Client ID and Secret first.")
            elif not code:
                STATE.log("Exchange: no code provided.")
            else:
                try:
                    tokens = StravaClient.exchange_code(
                        STATE.cfg.strava_client_id, STATE.cfg.strava_client_secret, code
                    )
                    STATE.cfg.set_strava_tokens(tokens)
                    STATE.log("Strava connected (code exchanged).")
                except Exception as exc:  # noqa: BLE001
                    STATE.log(f"Exchange failed: {exc}")
            self._json({"ok": True})
        elif self.path == "/api/strava/token":
            token = str(self._read_json().get("refresh_token") or "").strip()
            if token:
                STATE.cfg.set_strava_tokens({"refresh_token": token})
                STATE.log("Strava refresh token saved.")
            else:
                STATE.log("No refresh token provided.")
            self._json({"ok": True})
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
