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
        shutil.rmtree(SIPP_SRC_DIR)
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
# ─────────────────────────────────────────────────────────────────────────────
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
.tab{padding:12px 24px;cursor:pointer;font-size:.9rem;color:#94a3b8;border-bottom:3px solid transparent;transition:.2s}
.tab.active,.tab:hover{color:#38bdf8;border-bottom-color:#38bdf8}
.tab-content{display:none}.tab-content.active{display:block}
.container{max-width:900px;margin:32px auto;padding:0 20px}
.card{background:#1e293b;border-radius:12px;padding:28px;margin-bottom:24px;border:1px solid #334155}
.card h2{font-size:1.1rem;color:#38bdf8;margin-bottom:18px;font-weight:600}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:600px){.form-grid{grid-template-columns:1fr}}
label{display:block;font-size:.78rem;text-transform:uppercase;letter-spacing:1px;
      color:#64748b;margin-bottom:4px}
input,select,textarea{width:100%;background:#0f172a;border:1px solid #334155;
  color:#e2e8f0;border-radius:7px;padding:9px 12px;font-size:.92rem;outline:none;
  transition:border .2s}
input:focus,select:focus,textarea:focus{border-color:#38bdf8}
textarea{resize:vertical;min-height:80px;font-family:monospace;font-size:.82rem}
.btn{padding:10px 22px;border:none;border-radius:8px;cursor:pointer;font-size:.9rem;
     font-weight:600;transition:.2s}
.btn-primary{background:#0ea5e9;color:#fff}.btn-primary:hover{background:#38bdf8}
.btn-danger{background:#ef4444;color:#fff}.btn-danger:hover{background:#f87171}
.btn-sm{padding:6px 14px;font-size:.8rem}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.cmd-preview{background:#0f172a;border:1px solid #334155;border-radius:7px;
             padding:10px 14px;font-family:monospace;font-size:.8rem;color:#7dd3fc;
             word-break:break-all;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th{text-align:left;padding:10px 12px;color:#64748b;border-bottom:1px solid #334155;
   font-size:.75rem;text-transform:uppercase;letter-spacing:.8px}
td{padding:10px 12px;border-bottom:1px solid #1e293b;vertical-align:middle}
tr:hover td{background:#0f172a30}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.75rem;font-weight:600}
.badge-running{background:#0ea5e920;color:#38bdf8;border:1px solid #0ea5e940}
.badge-done{background:#10b98120;color:#34d399;border:1px solid #10b98140}
.badge-error{background:#ef444420;color:#f87171;border:1px solid #ef444440}
#logModal{display:none;position:fixed;inset:0;background:#000a;z-index:100;
          align-items:center;justify-content:center}
#logModal.open{display:flex}
.modal-box{background:#1e293b;border-radius:12px;padding:24px;width:90%;max-width:780px;
           border:1px solid #334155;max-height:85vh;display:flex;flex-direction:column;gap:12px}
.log-box{background:#0f172a;border-radius:8px;padding:14px;font-family:monospace;
         font-size:.8rem;color:#94a3b8;overflow-y:auto;flex:1;white-space:pre-wrap;
         max-height:55vh}
</style>
</head>
<body>
<header>
  <h1>📡 SIPp Web UI</h1>
  <span class="sipp-ver" id="sippVer">loading…</span>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('launch',this)">🚀 Launch</div>
  <div class="tab" onclick="switchTab('jobs',this)">📋 Jobs</div>
</div>

<!-- ══════════════ LAUNCH TAB ══════════════ -->
<div id="tab-launch" class="tab-content active">
<div class="container">
  <div class="card">
    <h2>🎯 Target</h2>
    <div class="form-grid">
      <div>
        <label>Remote Host *</label>
        <input id="remoteHost" placeholder="192.168.1.100" oninput="updatePreview()"/>
      </div>
      <div>
        <label>Remote Port</label>
        <input id="remotePort" value="5060" oninput="updatePreview()"/>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>👤 SIP Credentials</h2>
    <div class="form-grid">
      <div>
        <label>SIP User (-s)</label>
        <input id="sipUser" placeholder="1000" oninput="updatePreview()"/>
      </div>
      <div>
        <label>SIP Password (-ap)</label>
        <input id="sipPass" placeholder="secret" type="password" oninput="updatePreview()"/>
      </div>
      <div>
        <label>Local IP (-i)</label>
        <input id="localIp" placeholder="auto" oninput="updatePreview()"/>
      </div>
      <div>
        <label>Local Port (-p)</label>
        <input id="localPort" placeholder="5080" oninput="updatePreview()"/>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>⚙️ Load Parameters</h2>
    <div class="form-grid">
      <div>
        <label>Call Rate / s (-r)</label>
        <input id="callRate" value="1" oninput="updatePreview()"/>
      </div>
      <div>
        <label>Max Calls (-m)</label>
        <input id="maxCalls" value="10" oninput="updatePreview()"/>
      </div>
      <div>
        <label>Concurrent Calls (-l)</label>
        <input id="concCalls" value="10" oninput="updatePreview()"/>
      </div>
      <div>
        <label>Transport (-t)</label>
        <select id="transport" onchange="updatePreview()">
          <option value="u">UDP (u)</option>
          <option value="t">TCP (t)</option>
          <option value="l">TLS (l)</option>
        </select>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>📄 Scenario</h2>
    <div>
      <label>Scenario File Path (-sf)</label>
      <input id="scenario" placeholder="/path/to/scenario.xml  (leave blank to paste XML below)"
             oninput="updatePreview()"/>
    </div>
    <div style="margin-top:14px">
      <label>Inline Scenario XML (paste here)</label>
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
  const host = document.getElementById('remoteHost').value.trim();
  if (!host) return 'sipp ...';
  const port   = document.getElementById('remotePort').value.trim() || '5060';
  const user   = document.getElementById('sipUser').value.trim();
  const pass   = document.getElementById('sipPass').value.trim();
  const lip    = document.getElementById('localIp').value.trim();
  const lport  = document.getElementById('localPort').value.trim();
  const rate   = document.getElementById('callRate').value.trim() || '1';
  const maxc   = document.getElementById('maxCalls').value.trim() || '10';
  const conc   = document.getElementById('concCalls').value.trim() || '10';
  const trans  = document.getElementById('transport').value;
  const scen   = document.getElementById('scenario').value.trim();
  const xml    = document.getElementById('scenarioXml').value.trim();
  let cmd = 'sipp';
  if (xml)   cmd += ' -sf /tmp/sipp_scenario.xml';
  else if (scen) cmd += ` -sf ${scen}`;
  if (user)  cmd += ` -s ${user}`;
  if (pass)  cmd += ` -ap ${pass}`;
  if (lip)   cmd += ` -i ${lip}`;
  if (lport) cmd += ` -p ${lport}`;
  cmd += ` -r ${rate} -m ${maxc} -l ${conc} -t ${trans}`;
  cmd += ` ${host}:${port}`;
  return cmd;
}
function updatePreview() {
  document.getElementById('cmdPreview').textContent = buildCmd();
}

// ── launch ────────────────────────────────────────────────────────────────
function launchSipp() {
  const host = document.getElementById('remoteHost').value.trim();
  if (!host) { alert('Remote Host is required'); return; }
  const payload = {
    remote_host:  host,
    remote_port:  document.getElementById('remotePort').value.trim() || '5060',
    sip_user:     document.getElementById('sipUser').value.trim(),
    sip_pass:     document.getElementById('sipPass').value.trim(),
    local_ip:     document.getElementById('localIp').value.trim(),
    local_port:   document.getElementById('localPort').value.trim(),
    call_rate:    document.getElementById('callRate').value.trim() || '1',
    max_calls:    document.getElementById('maxCalls').value.trim() || '10',
    conc_calls:   document.getElementById('concCalls').value.trim() || '10',
    transport:    document.getElementById('transport').value,
    scenario:     document.getElementById('scenario').value.trim(),
    scenario_xml: document.getElementById('scenarioXml').value.trim(),
  };
  fetch('/api/launch', {method:'POST', headers:{'Content-Type':'application/json'},
                        body:JSON.stringify(payload)})
    .then(r=>r.json()).then(d=>{
      if (d.error) { alert('Error: '+d.error); return; }
      alert(`✅ Job #${d.job_id} started (PID ${d.pid})`);
      switchTab('jobs', document.querySelectorAll('.tab')[1]);
    });
}

// ── jobs table ────────────────────────────────────────────────────────────
function refreshJobs() {
  fetch('/api/jobs').then(r=>r.json()).then(jobs=>{
    const el = document.getElementById('jobsTable');
    if (!jobs.length) {
      el.innerHTML = '<p style="color:#64748b;font-size:.9rem">No jobs yet — launch a test first.</p>';
      return;
    }
    let html = `<table><thead><tr>
      <th>#</th><th>Command</th><th>PID</th><th>Status</th><th>Actions</th>
    </tr></thead><tbody>`;
    for (const j of jobs) {
      const badge = j.status==='running'
        ? '<span class="badge badge-running">running</span>'
        : j.status==='done'
        ? '<span class="badge badge-done">done</span>'
        : '<span class="badge badge-error">error</span>';
      const cmd = j.cmd.length>60 ? j.cmd.slice(0,60)+'…' : j.cmd;
      html += `<tr>
        <td>${j.job_id}</td>
        <td style="font-family:monospace;font-size:.78rem;color:#7dd3fc">${cmd}</td>
        <td>${j.pid||'—'}</td>
        <td>${badge}</td>
        <td>
          <button class="btn btn-sm" style="background:#334155;color:#e2e8f0"
                  onclick="showLog(${j.job_id})">📄 Log</button>
          ${j.status==='running'
            ? `<button class="btn btn-sm btn-danger" onclick="killJob(${j.job_id})">⛔ Kill</button>`
            : ''}
        </td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  });
}

function killJob(jid) {
  fetch('/api/kill', {method:'POST', headers:{'Content-Type':'application/json'},
                      body:JSON.stringify({job_id:jid})})
    .then(()=>refreshJobs());
}
function killAll() {
  fetch('/api/kill_all', {method:'POST'}).then(()=>refreshJobs());
}

// ── log modal ─────────────────────────────────────────────────────────────
function showLog(jid) {
  currentJobId = jid;
  document.getElementById('modalTitle').textContent = `Job #${jid} Output`;
  document.getElementById('logModal').classList.add('open');
  refreshModal();
}
function refreshModal() {
  if (!currentJobId) return;
  fetch(`/api/log?job_id=${currentJobId}`).then(r=>r.json()).then(d=>{
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
setInterval(refreshJobs, 3000);
refreshJobs();
</script>
</body>
</html>"""

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
                    {
                        "job_id": jid,
                        "cmd":    j["cmd"],
                        "status": j["status"],
                        "pid":    j.get("pid"),
                    }
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
    ensure_sipp()

    PORT = 8000
    server = http.server.HTTPServer(("0.0.0.0", PORT), SippHandler)
    _print(f"🌐  SIPp Web UI  →  http://0.0.0.0:{PORT}")
    server.serve_forever()
