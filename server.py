#!/usr/bin/env python3
"""
SIPp Web UI — Python-only, port 8000
SIPp is built from source automatically on first run.
Usage: python3 server.py
"""

import http.server
import json
import subprocess
import threading
import os
import shlex
import shutil
import sys
from urllib.parse import parse_qs, urlparse

# ─────────────────────────────────────────────────────────────────────────────
# SIPp — build from source on startup
# ─────────────────────────────────────────────────────────────────────────────
SIPP_REPO    = "https://github.com/SIPp/sipp.git"
SIPP_SRC_DIR = "/tmp/sipp_src"
SIPP_BIN     = "/usr/local/bin/sipp"

BUILD_DEPS = [
    "git", "cmake", "make",
    "build-essential",
    "libssl-dev",
    "libpcap-dev",
    "libxml2-dev",
    "libsctp-dev",
    "lksctp-tools",
]


def _print(msg: str):
    print(msg, flush=True)


def _run(cmd: list, cwd: str = None, check: bool = True) -> int:
    """Run a command, stream output to stdout, return exit code."""
    _print(f"  $ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
    )
    for line in iter(proc.stdout.readline, b""):
        sys.stdout.write("    " + line.decode("utf-8", errors="replace"))
        sys.stdout.flush()
    proc.wait()
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit {proc.returncode}): {' '.join(cmd)}")
    return proc.returncode


def install_deps():
    _print("\n[1/4] Installing build dependencies via apt-get...")
    _run(["sudo", "apt-get", "update", "-y"], check=False)
    _run(["sudo", "apt-get", "install", "-y"] + BUILD_DEPS)


def clone_sipp():
    _print("\n[2/4] Cloning SIPp source from GitHub...")
    if os.path.isdir(SIPP_SRC_DIR):
        _print(f"  Removing old source dir: {SIPP_SRC_DIR}")
        _run(["rm", "-rf", SIPP_SRC_DIR])
    _run(["git", "clone", "--depth=1", SIPP_REPO, SIPP_SRC_DIR])


def build_sipp():
    _print("\n[3/4] Configuring with CMake...")
    build_dir = os.path.join(SIPP_SRC_DIR, "build")
    os.makedirs(build_dir, exist_ok=True)
    _run(
        ["cmake", "..",
         "-DUSE_SSL=ON",
         "-DUSE_SCTP=ON",
         "-DUSE_PCAP=ON",
         "-DCMAKE_BUILD_TYPE=Release"],
        cwd=build_dir,
    )
    _print("\n[4/4] Compiling SIPp (this may take a few minutes)...")
    cpu_count = str(os.cpu_count() or 2)
    _run(["make", f"-j{cpu_count}"], cwd=build_dir)
    _print("\n  Installing binary to /usr/local/bin/sipp ...")
    _run(["sudo", "make", "install"], cwd=build_dir)


def ensure_sipp():
    """
    Called once at startup.
    If sipp is already on PATH, skip the build entirely.
    Otherwise, build from source.
    """
    if shutil.which("sipp"):
        path = shutil.which("sipp")
        _print(f"\n✅  SIPp already installed → {path}  (skipping build)\n")
        return

    _print("\n" + "═" * 60)
    _print("  SIPp not found — building from source...")
    _print("═" * 60)

    try:
        install_deps()
        clone_sipp()
        build_sipp()
    except Exception as e:
        _print(f"\n❌  Build failed: {e}")
        _print("    Fix the error above and restart the server.")
        sys.exit(1)

    if not shutil.which("sipp"):
        _print("\n❌  Build succeeded but 'sipp' is still not in PATH.")
        _print("    Try: export PATH=$PATH:/usr/local/bin")
        sys.exit(1)

    _print("\n✅  SIPp built and installed successfully!\n")
    _print("═" * 60 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Global state for jobs
# ─────────────────────────────────────────────────────────────────────────────
running_processes = {}
job_counter = 0
lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Build SIPp command from request params
# ───────────────────────────────────────────────────────��─────────────────────
def build_sipp_command(params: dict) -> list:
    cmd = ["sipp"]

    xml      = params.get("scenario_xml", "").strip()
    scenario = params.get("scenario",     "").strip()

    if xml:
        xml_path = "/tmp/sipp_scenario.xml"
        with open(xml_path, "w") as f:
            f.write(xml)
        cmd += ["-sf", xml_path]
    elif scenario:
        cmd += ["-sf", scenario]

    if params.get("sip_user"):   cmd += ["-s",  params["sip_user"]]
    if params.get("sip_pass"):   cmd += ["-ap", params["sip_pass"]]
    if params.get("local_ip"):   cmd += ["-i",  params["local_ip"]]
    if params.get("local_port"): cmd += ["-p",  params["local_port"]]

    cmd += ["-r", params.get("call_rate",  "1")]
    cmd += ["-m", params.get("max_calls",  "10")]
    cmd += ["-l", params.get("conc_calls", "10")]
    cmd += ["-t", params.get("transport",  "u")]

    extra = params.get("extra_args", "").strip()
    if extra:
        cmd += shlex.split(extra)

    remote = params["remote_host"] + ":" + params.get("remote_port", "5060")
    cmd.append(remote)
    return cmd


# ─────────────────────────────────────────────────────────────────────────────
# Stream process output into job record
# ─────────────────────────────────────────────────────────────────────────────
def stream_output(job_id: int, proc):
    job = running_processes[job_id]
    try:
        for line in iter(proc.stdout.readline, b""):
            with lock:
                job["output"].append(line.decode("utf-8", errors="replace"))
        proc.wait()
        with lock:
            job["status"] = "error" if proc.returncode != 0 else "done"
    except Exception as e:
        with lock:
            job["output"].append(f"\n[stream error: {e}]\n")
            job["status"] = "error"


# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>SIPp Web UI</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
header{background:#1e293b;padding:16px 32px;border-bottom:2px solid #334155;
       display:flex;align-items:center;gap:16px}
header h1{font-size:1.45rem;font-weight:700;color:#38bdf8;letter-spacing:.5px}
.sipp-ver{font-size:.78rem;background:#0ea5e940;color:#7dd3fc;padding:3px 10px;
          border-radius:20px;border:1px solid #0ea5e960}
.tabs{display:flex;gap:0;border-bottom:2px solid #334155;background:#1e293b;padding:0 28px}
.tab{padding:12px 22px;cursor:pointer;font-size:.88rem;color:#94a3b8;
     border-bottom:2px solid transparent;margin-bottom:-2px;transition:.15s;user-select:none}
.tab.active{color:#38bdf8;border-bottom-color:#38bdf8;font-weight:600}
.tab-content{display:none}.tab-content.active{display:block}
.container{max-width:1100px;margin:26px auto;padding:0 16px}
.card{background:#1e293b;border-radius:12px;padding:24px;margin-bottom:22px;border:1px solid #334155}
.card h2{font-size:1.05rem;color:#7dd3fc;margin-bottom:16px;
         border-bottom:1px solid #334155;padding-bottom:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
label{display:block;font-size:.82rem;color:#94a3b8;margin-bottom:4px}
input,select,textarea{width:100%;padding:8px 12px;border-radius:8px;border:1px solid #475569;
  background:#0f172a;color:#e2e8f0;font-size:.9rem;outline:none;transition:border-color .2s}
input:focus,select:focus,textarea:focus{border-color:#38bdf8}
textarea{resize:vertical;min-height:80px;font-family:monospace;font-size:.82rem}
.cmd-preview{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px;
  font-family:monospace;font-size:.82rem;color:#a3e635;word-break:break-all;
  margin-top:10px;min-height:38px;line-height:1.6}
.btn{padding:10px 22px;border-radius:8px;border:none;cursor:pointer;
     font-size:.88rem;font-weight:600;transition:all .15s}
.btn-primary{background:#0ea5e9;color:#fff}.btn-primary:hover{background:#0284c7}
.btn-danger{background:#ef4444;color:#fff}.btn-danger:hover{background:#dc2626}
.btn-sm{padding:5px 13px;font-size:.8rem}
.btn-row{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;align-items:center}
table{width:100%;border-collapse:collapse;font-size:.87rem}
th{background:#0f172a;color:#7dd3fc;padding:10px 12px;text-align:left}
td{padding:9px 12px;border-bottom:1px solid #1e293b55;vertical-align:middle}
tr:hover td{background:#1e293b99}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.74rem;font-weight:600}
.badge-running{background:#166534;color:#86efac}
.badge-done   {background:#1e3a5f;color:#93c5fd}
.badge-error  {background:#7f1d1d;color:#fca5a5}
.log-box{background:#020617;border:1px solid #334155;border-radius:8px;padding:14px;
  font-family:monospace;font-size:.78rem;color:#86efac;max-height:440px;
  overflow-y:auto;white-space:pre-wrap;word-break:break-all;margin-top:10px;line-height:1.5}
#logModal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;
  background:#0009;z-index:100;justify-content:center;align-items:center}
#logModal.open{display:flex}
.modal-box{background:#1e293b;border-radius:12px;padding:24px;width:92%;max-width:840px;
  max-height:86vh;display:flex;flex-direction:column;gap:10px;border:1px solid #334155}
.modal-box h3{color:#38bdf8}
.tip{background:#0f172a;border-left:3px solid #0ea5e9;padding:10px 14px;border-radius:6px;
     font-size:.83rem;color:#94a3b8;margin-bottom:14px;line-height:1.6}
.tip code{color:#a3e635;background:#1e293b;padding:1px 5px;border-radius:4px;font-size:.8rem}
</style>
</head>
<body>

<header>
  <h1>📞 SIPp Web UI</h1>
  <span class="sipp-ver" id="sippVer">checking...</span>
</header>

<div class="tabs">
  <div class="tab active"  onclick="switchTab('launch',this)">🚀 Launch Test</div>
  <div class="tab"         onclick="switchTab('jobs',  this)">📋 Jobs</div>
</div>

<!-- ══════════════ LAUNCH TAB ══════════════ -->
<div id="tab-launch" class="tab-content active">
<div class="container">
  <div class="card">
    <h2>🚀 Configure &amp; Launch SIPp Test</h2>

    <div class="tip">
      SIPp was built from source on server startup. Use the fields below to configure your test.
      The full command is previewed before you launch.
    </div>

    <div class="grid">
      <div><label>Remote Host / IP *</label>
           <input id="remoteHost" placeholder="192.168.1.100"/></div>
      <div><label>Remote Port</label>
           <input id="remotePort" value="5060" placeholder="5060"/></div>
      <div><label>SIP Username&nbsp;<code style="font-size:.75rem;color:#a3e635">-s</code></label>
           <input id="sipUser" placeholder="1001"/></div>
      <div><label>Auth Password&nbsp;<code style="font-size:.75rem;color:#a3e635">-ap</code></label>
           <input id="sipPass" type="password" placeholder="secret"/></div>
      <div><label>Local IP&nbsp;<code style="font-size:.75rem;color:#a3e635">-i</code></label>
           <input id="localIp" placeholder="auto-detect"/></div>
      <div><label>Local SIP Port&nbsp;<code style="font-size:.75rem;color:#a3e635">-p</code></label>
           <input id="localPort" placeholder="5060"/></div>
      <div><label>Calls / sec&nbsp;<code style="font-size:.75rem;color:#a3e635">-r</code></label>
           <input id="callRate" type="number" value="1" min="1"/></div>
      <div><label>Max Calls&nbsp;<code style="font-size:.75rem;color:#a3e635">-m</code></label>
           <input id="maxCalls" type="number" value="10" min="1"/></div>
      <div><label>Max Concurrent&nbsp;<code style="font-size:.75rem;color:#a3e635">-l</code></label>
           <input id="concCalls" type="number" value="10" min="1"/></div>
      <div><label>Transport&nbsp;<code style="font-size:.75rem;color:#a3e635">-t</code></label>
           <select id="transport">
             <option value="u">UDP</option>
             <option value="t">TCP</option>
             <option value="l">TLS</option>
             <option value="w">WebSocket</option>
             <option value="wss">Secure WebSocket</option>
           </select></div>
      <div><label>Built-in Scenario&nbsp;<code style="font-size:.75rem;color:#a3e635">-sf</code></label>
           <select id="scenarioSelect">
             <option value="">-- none / use XML below --</option>
             <option value="uac">uac  (UAC call flow)</option>
             <option value="uas">uas  (UAS auto-answer)</option>
             <option value="regexp">regexp</option>
           </select></div>
      <div><label>Extra Flags</label>
           <input id="extraArgs" placeholder="-trace_msg -aa -recv_timeout 5000"/></div>
    </div>

    <div style="margin-top:16px">
      <label>Custom Scenario XML
        <span style="color:#64748b;font-size:.78rem">(optional — overrides built-in scenario above)</span>
      </label>
      <textarea id="scenarioXml" placeholder="Paste your SIPp XML scenario here..."></textarea>
    </div>

    <div style="margin-top:14px">
      <label style="text-transform:uppercase;letter-spacing:1px;font-size:.76rem;color:#64748b">
        Command Preview
      </label>
      <div class="cmd-preview" id="cmdPreview">sipp ...</div>
    </div>

    <div class="btn-row">
      <button class="btn btn-primary" onclick="launchSipp()">▶&nbsp; Launch</button>
      <button class="btn" style="background:#334155;color:#e2e8f0" onclick="updatePreview()">
        🔄&nbsp;Refresh Preview
      </button>
    </div>
  </div>
</div>
</div>

<!-- ══════════════ JOBS TAB ══════════════ -->
<div id="tab-jobs" class="tab-content">
<div class="container">
  <div class="card">
    <h2>📋 Running / Recent Jobs</h2>
    <div id="jobsTable">
      <p style="color:#64748b;font-size:.9rem">No jobs yet — launch a test first.</p>
    </div>
    <div class="btn-row">
      <button class="btn btn-sm" style="background:#334155;color:#e2e8f0" onclick="refreshJobs()">
        🔄&nbsp;Refresh
      </button>
      <button class="btn btn-sm btn-danger" onclick="killAll()">⛔&nbsp;Kill All</button>
    </div>
  </div>
</div>
</div>

<!-- ══════════════ LOG MODAL ══════════════ -->
<div id="logModal">
  <div class="modal-box">
    <div style="display:flex;align-items:center;gap:10px">
      <h3 id="modalTitle">Output</h3>
      <button class="btn btn-sm" style="background:#334155;color:#e2e8f0;margin-left:auto"
              onclick="closeModal()">✕ Close</button>
    </div>
    <div class="log-box" id="modalLog">Loading...</div>
    <button class="btn btn-sm" style="background:#334155;color:#e2e8f0;width:fit-content"
            onclick="refreshModal()">🔄 Refresh</button>
  </div>
</div>

<script>
let currentJobId = null;

// ── version badge ─────────────────────────────────────────────────────────
fetch('/api/sipp_version').then(r=>r.json()).then(d=>{
  document.getElementById('sippVer').textContent = d.version || 'SIPp ready';
});

// ── tabs ──────────────────────────────────────────────────────────────────
function switchTab(name, el) {
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  el.classList.add('active');
  if (name==='jobs') refreshJobs();
}

// ── command builder ───────────────────────────────────────────────────────
function buildCmd() {
  const v = id => document.getElementById(id).value.trim();
  let cmd = 'sipp';
  const xml=v('scenarioXml'), scen=v('scenarioSelect');
  if (xml)       cmd += ' -sf /tmp/sipp_scenario.xml';
  else if (scen) cmd += ' -sf '+scen;
  if (v('sipUser'))   cmd += ' -s '  + v('sipUser');
  if (v('sipPass'))   cmd += ' -ap ' + v('sipPass');
  if (v('localIp'))   cmd += ' -i '  + v('localIp');
  if (v('localPort')) cmd += ' -p '  + v('localPort');
  cmd += ' -r '+(v('callRate')||'1');
  cmd += ' -m '+(v('maxCalls')||'10');
  cmd += ' -l '+(v('concCalls')||'10');
  cmd += ' -t '+v('transport');
  if (v('extraArgs')) cmd += ' '+v('extraArgs');
  cmd += ' '+(v('remoteHost')||'<remote_host>')+':'+(v('remotePort')||'5060');
  return cmd;
}
function updatePreview(){
  document.getElementById('cmdPreview').textContent = buildCmd();
}
document.querySelectorAll('input,select,textarea')
        .forEach(el=>el.addEventListener('input',updatePreview));
updatePreview();

// ── launch ────────────────────────────────────────────────────────────────
function launchSipp() {
  const host = document.getElementById('remoteHost').value.trim();
  if (!host){ alert('Remote Host / IP is required.'); return; }
  const payload = {
    remote_host:  host,
    remote_port:  document.getElementById('remotePort').value.trim()  || '5060',
    sip_user:     document.getElementById('sipUser').value.trim(),
    sip_pass:     document.getElementById('sipPass').value.trim(),
    local_ip:     document.getElementById('localIp').value.trim(),
    local_port:   document.getElementById('localPort').value.trim(),
    call_rate:    document.getElementById('callRate').value.trim()     || '1',
    max_calls:    document.getElementById('maxCalls').value.trim()     || '10',
    conc_calls:   document.getElementById('concCalls').value.trim()    || '10',
    transport:    document.getElementById('transport').value,
    scenario:     document.getElementById('scenarioSelect').value,
    scenario_xml: document.getElementById('scenarioXml').value.trim(),
    extra_args:   document.getElementById('extraArgs').value.trim(),
  };
  fetch('/api/launch',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)
  }).then(r=>r.json()).then(d=>{
    if(d.error){ alert('❌ '+d.error); return; }
    alert('✅ Job #'+d.job_id+' launched!\n\n'+d.cmd);
    // switch to jobs tab
    document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.getElementById('tab-jobs').classList.add('active');
    document.querySelectorAll('.tab')[1].classList.add('active');
    refreshJobs();
  });
}

// ── jobs ──────────────────────────────────────────────────────────────────
function refreshJobs(){
  fetch('/api/jobs').then(r=>r.json()).then(jobs=>{
    const el=document.getElementById('jobsTable');
    if(!jobs.length){
      el.innerHTML='<p style="color:#64748b;font-size:.9rem">No jobs yet.</p>';
      return;
    }
    let h='<table><thead><tr><th>#</th><th>Command</th><th>Status</th><th>PID</th><th>Actions</th></tr></thead><tbody>';
    jobs.forEach(j=>{
      const badge = j.status==='running'
        ? '<span class="badge badge-running">▶ Running</span>'
        : j.status==='error'
        ? '<span class="badge badge-error">✕ Error</span>'
        : '<span class="badge badge-done">✓ Done</span>';
      const kill = j.status==='running'
        ? `<button class="btn btn-sm btn-danger" onclick="killJob(${j.job_id})">Kill</button>`
        : '';
      h+=`<tr>
        <td>${j.job_id}</td>
        <td style="font-family:monospace;font-size:.75rem;max-width:420px;word-break:break-all">${j.cmd}</td>
        <td>${badge}</td>
        <td>${j.pid||'-'}</td>
        <td style="display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn btn-sm" style="background:#334155;color:#e2e8f0"
                  onclick="showLog(${j.job_id})">📄 Logs</button>
          ${kill}
        </td></tr>`;
    });
    el.innerHTML=h+'</tbody></table>';
  });
}
function killJob(id){
  fetch('/api/kill',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({job_id:id})}).then(()=>refreshJobs());
}
function killAll(){
  fetch('/api/kill_all',{method:'POST'}).then(()=>refreshJobs());
}

// ── log modal ─────────────────────────────────────────────────────────────
function showLog(id){
  currentJobId=id;
  document.getElementById('modalTitle').textContent='Job #'+id+' — Output';
  document.getElementById('logModal').classList.add('open');
  refreshModal();
}
function refreshModal(){
  if(currentJobId===null) return;
  fetch('/api/log?job_id='+currentJobId).then(r=>r.json()).then(d=>{
    const el=document.getElementById('modalLog');
    el.textContent=d.output||'(no output yet)';
    el.scrollTop=el.scrollHeight;
  });
}
function closeModal(){
  document.getElementById('logModal').classList.remove('open');
  currentJobId=null;
}

// auto-refresh jobs every 3 s
setInterval(refreshJobs,3000);
refreshJobs();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────────────────────────────────────
class SippHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}", flush=True)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path in ("/", "/index.html"):
            self.send_html(HTML_PAGE)

        elif path == "/api/sipp_version":
            try:
                r = subprocess.run(
                    ["sipp", "-v"],
                    capture_output=True, text=True, timeout=5
                )
                raw = (r.stdout + r.stderr).strip()
                ver = raw.splitlines()[0] if raw else "SIPp ready"
            except Exception:
                ver = shutil.which("sipp") or "SIPp ready"
            self.send_json({"version": ver})

        elif path == "/api/jobs":
            with lock:
                jobs = [
                    {"job_id": jid, "cmd": j["cmd"],
                     "status": j["status"], "pid": j.get("pid")}
                    for jid, j in running_processes.items()
                ]
            self.send_json(list(reversed(jobs)))

        elif path == "/api/log":
            qs = parse_qs(parsed.query)
            try:
                jid = int(qs["job_id"][0])
            except (KeyError, ValueError, IndexError):
                self.send_json({"error": "missing job_id"}, 400)
                return
            with lock:
                job = running_processes.get(jid)
            if not job:
                self.send_json({"error": "job not found"}, 404)
                return
            self.send_json({"output": "".join(job["output"])})

        else:
            self.send_response(404)
            self.end_headers()

    # ── POST ─────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/launch":
            global job_counter
            params = self.read_json_body()
            if not params.get("remote_host"):
                self.send_json({"error": "remote_host is required"}, 400)
                return
            try:
                cmd = build_sipp_command(params)
            except Exception as e:
                self.send_json({"error": str(e)}, 400)
                return
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            except FileNotFoundError:
                self.send_json({"error": "sipp binary not found — check server logs"}, 500)
                return
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return

            with lock:
                job_counter += 1
                jid = job_counter
                running_processes[jid] = {
                    "cmd":    " ".join(cmd),
                    "status": "running",
                    "pid":    proc.pid,
                    "output": [],
                    "proc":   proc,
                }
            threading.Thread(
                target=stream_output, args=(jid, proc), daemon=True
            ).start()
            self.send_json({"job_id": jid, "pid": proc.pid, "cmd": " ".join(cmd)})

        elif path == "/api/kill":
            params = self.read_json_body()
            with lock:
                job = running_processes.get(params.get("job_id"))
            if not job:
                self.send_json({"error": "job not found"}, 404)
                return
            try:
                job["proc"].terminate()
                job["status"] = "done"
                self.send_json({"killed": params["job_id"]})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/kill_all":
            with lock:
                jobs = list(running_processes.values())
            for j in jobs:
                try:
                    if j["status"] == "running":
                        j["proc"].terminate()
                        j["status"] = "done"
                except Exception:
                    pass
            self.send_json({"killed": "all"})

        else:
            self.send_response(404)
            self.end_headers()


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # ── 1. Build / verify SIPp BEFORE starting the HTTP server ──────────────
    ensure_sipp()

    # ── 2. Start HTTP server ─���───────────────────────────────────────────────
    PORT = 8000
    server = http.server.HTTPServer(("0.0.0.0", PORT), SippHandler)
    _print(f"🌐  SIPp Web UI  →  http://0.0.0.0:{PORT}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _print("\n🛑  Server stopped.")
        server.server_close()
