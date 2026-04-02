#!/usr/bin/env python3
"""
SIPp Web UI - Python-only server on port 8000
Includes: auto-install SIPp via apt or compile from source
Run: python3 server.py
"""

import http.server
import json
import subprocess
import threading
import os
import shlex
import shutil
from urllib.parse import parse_qs, urlparse

# ─────────────────────────────────────────────
# Global state
# ─────────────────────────────────────────────
running_processes = {}   # { job_id: {"proc", "cmd", "output", "status", "pid"} }
install_log       = []   # lines from installer
install_status    = "idle"   # idle | running | done | error
job_counter       = 0
lock              = threading.Lock()

# ─────────────────────────────────────────────
# SIPp installer
# ─────────────────────────────────────────────
def sipp_is_installed() -> bool:
    return shutil.which("sipp") is not None

def _run_install_step(cmd: list, shell=False):
    """Run one install step, stream output into install_log."""
    global install_status
    with lock:
        install_log.append(f"\n$ {' '.join(cmd)}\n")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=shell,
    )
    for line in iter(proc.stdout.readline, b""):
        with lock:
            install_log.append(line.decode("utf-8", errors="replace"))
    proc.wait()
    return proc.returncode

def _install_worker():
    global install_status
    with lock:
        install_log.clear()
        install_log.append("=== SIPp Installer Started ===\n")
        install_status = "running"

    # ── Step 1: try apt ──────────────────────
    with lock:
        install_log.append("\n[1/3] Trying apt-get install sipp ...\n")

    rc = _run_install_step(["sudo", "apt-get", "update", "-y"])
    if rc == 0:
        rc = _run_install_step(["sudo", "apt-get", "install", "-y", "sipp"])
        if rc == 0 and sipp_is_installed():
            with lock:
                install_log.append("\n✅  SIPp installed via apt!\n")
                install_status = "done"
            return

    # ── Step 2: build from source ────────────
    with lock:
        install_log.append("\n[2/3] apt failed or sipp not found. Building from source...\n")

    deps = [
        "sudo", "apt-get", "install", "-y",
        "build-essential", "git", "cmake",
        "libssl-dev", "libpcap-dev", "libsctp-dev",
        "lksctp-tools",
    ]
    rc = _run_install_step(deps)
    if rc != 0:
        with lock:
            install_log.append("\n❌  Failed to install build dependencies.\n")
            install_status = "error"
        return

    clone_dir = "/tmp/sipp_src"
    if os.path.exists(clone_dir):
        _run_install_step(["rm", "-rf", clone_dir])

    rc = _run_install_step(["git", "clone", "--depth=1",
                             "https://github.com/SIPp/sipp.git", clone_dir])
    if rc != 0:
        with lock:
            install_log.append("\n❌  git clone failed.\n")
            install_status = "error"
        return

    # cmake + make + install
    for step_cmd in [
        ["cmake", "-DUSE_SSL=1", "-DUSE_SCTP=1", "-DUSE_PCAP=1", "."],
        ["make", "-j4"],
        ["sudo", "make", "install"],
    ]:
        old_dir = os.getcwd()
        os.chdir(clone_dir)
        rc = _run_install_step(step_cmd)
        os.chdir(old_dir)
        if rc != 0:
            with lock:
                install_log.append(f"\n❌  Step failed: {' '.join(step_cmd)}\n")
                install_status = "error"
            return

    if sipp_is_installed():
        with lock:
            install_log.append("\n✅  SIPp built and installed from source!\n")
            install_status = "done"
    else:
        with lock:
            install_log.append("\n❌  Build finished but sipp not found in PATH.\n")
            install_status = "error"


def start_install():
    global install_status
    with lock:
        if install_status == "running":
            return False   # already running
    t = threading.Thread(target=_install_worker, daemon=True)
    t.start()
    return True


# ─────────────────────────────────────────────
# Build SIPp command from form params
# ─────────────────────────────────────────────
def build_sipp_command(params: dict) -> list:
    cmd = ["sipp"]
    xml = params.get("scenario_xml", "").strip()
    scenario = params.get("scenario", "").strip()

    if xml:
        xml_path = "/tmp/sipp_scenario.xml"
        with open(xml_path, "w") as f:
            f.write(xml)
        cmd += ["-sf", xml_path]
    elif scenario:
        cmd += ["-sf", scenario]

    if params.get("sip_user"):  cmd += ["-s",  params["sip_user"]]
    if params.get("sip_pass"):  cmd += ["-ap", params["sip_pass"]]
    if params.get("local_ip"):  cmd += ["-i",  params["local_ip"]]
    if params.get("local_port"): cmd += ["-p", params["local_port"]]

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


# ─────────────────────────────────────────────
# Stream process output into job record
# ─────────────────────────────────────────────
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


# ────────────────────���────────────────────────
# HTML
# ─────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>SIPp Web UI</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
  header{background:#1e293b;padding:16px 32px;border-bottom:2px solid #334155;
         display:flex;align-items:center;gap:14px}
  header h1{font-size:1.45rem;font-weight:700;color:#38bdf8}
  .tabs{display:flex;gap:0;border-bottom:2px solid #334155;background:#1e293b;padding:0 32px}
  .tab{padding:12px 24px;cursor:pointer;font-size:0.9rem;color:#94a3b8;border-bottom:2px solid transparent;
       margin-bottom:-2px;transition:.15s}
  .tab.active{color:#38bdf8;border-bottom-color:#38bdf8;font-weight:600}
  .tab-content{display:none}.tab-content.active{display:block}
  .container{max-width:1100px;margin:28px auto;padding:0 16px}
  .card{background:#1e293b;border-radius:12px;padding:24px;margin-bottom:22px;border:1px solid #334155}
  .card h2{font-size:1.05rem;color:#7dd3fc;margin-bottom:16px;border-bottom:1px solid #334155;padding-bottom:8px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
  label{display:block;font-size:0.82rem;color:#94a3b8;margin-bottom:4px}
  input,select,textarea{width:100%;padding:8px 12px;border-radius:8px;border:1px solid #475569;
    background:#0f172a;color:#e2e8f0;font-size:0.9rem;outline:none;transition:border-color .2s}
  input:focus,select:focus,textarea:focus{border-color:#38bdf8}
  textarea{resize:vertical;min-height:70px;font-family:monospace;font-size:0.82rem}
  .cmd-preview{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px;
    font-family:monospace;font-size:0.82rem;color:#a3e635;word-break:break-all;margin-top:10px;min-height:38px}
  .btn{padding:10px 22px;border-radius:8px;border:none;cursor:pointer;font-size:0.88rem;
       font-weight:600;transition:all .15s}
  .btn-primary{background:#0ea5e9;color:#fff}.btn-primary:hover{background:#0284c7}
  .btn-danger{background:#ef4444;color:#fff}.btn-danger:hover{background:#dc2626}
  .btn-success{background:#16a34a;color:#fff}.btn-success:hover{background:#15803d}
  .btn-sm{padding:5px 13px;font-size:0.8rem}
  .btn-row{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;align-items:center}
  table{width:100%;border-collapse:collapse;font-size:0.87rem}
  th{background:#0f172a;color:#7dd3fc;padding:10px 12px;text-align:left}
  td{padding:9px 12px;border-bottom:1px solid #1e293b44;vertical-align:middle}
  tr:hover td{background:#1e293b88}
  .badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.75rem;font-weight:600}
  .badge-running{background:#166534;color:#86efac}
  .badge-done{background:#1e3a5f;color:#93c5fd}
  .badge-error{background:#7f1d1d;color:#fca5a5}
  .badge-idle{background:#292524;color:#a8a29e}
  .log-box{background:#020617;border:1px solid #334155;border-radius:8px;padding:12px;
    font-family:monospace;font-size:0.78rem;color:#86efac;max-height:420px;
    overflow-y:auto;white-space:pre-wrap;word-break:break-all;margin-top:10px}
  #logModal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:#000a;
    z-index:100;justify-content:center;align-items:center}
  #logModal.open{display:flex}
  .modal-box{background:#1e293b;border-radius:12px;padding:24px;width:90%;max-width:820px;
    max-height:85vh;display:flex;flex-direction:column;gap:10px}
  .modal-box h3{color:#38bdf8}
  .install-status{display:flex;align-items:center;gap:12px;padding:12px 16px;
    border-radius:8px;background:#0f172a;border:1px solid #334155;margin-bottom:14px}
  .dot{width:12px;height:12px;border-radius:50%;flex-shrink:0}
  .dot-idle{background:#78716c}.dot-running{background:#facc15;animation:pulse 1s infinite}
  .dot-done{background:#22c55e}.dot-error{background:#ef4444}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<header>
  <h1>📞 SIPp Web UI</h1>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('launch')">🚀 Launch Test</div>
  <div class="tab" onclick="switchTab('jobs')">📋 Jobs</div>
  <div class="tab" onclick="switchTab('install')">⚙️ Install SIPp</div>
</div>

<!-- ═══════════ TAB: LAUNCH ═══════════ -->
<div id="tab-launch" class="tab-content active">
<div class="container">
  <div class="card">
    <h2>🚀 Launch SIPp Test</h2>
    <div class="grid">
      <div><label>Remote Host / IP *</label><input id="remoteHost" placeholder="192.168.1.100"/></div>
      <div><label>Remote Port</label><input id="remotePort" placeholder="5060" value="5060"/></div>
      <div><label>SIP Username (-s)</label><input id="sipUser" placeholder="1001"/></div>
      <div><label>Password (-ap)</label><input id="sipPass" type="password" placeholder="secret"/></div>
      <div><label>Local IP (-i)</label><input id="localIp" placeholder="auto"/></div>
      <div><label>Local Port (-p)</label><input id="localPort" placeholder="5060"/></div>
      <div><label>Calls/sec (-r)</label><input id="callRate" type="number" placeholder="1" value="1" min="1"/></div>
      <div><label>Max Calls (-m)</label><input id="maxCalls" type="number" placeholder="10" value="10" min="1"/></div>
      <div><label>Max Concurrent (-l)</label><input id="concCalls" type="number" placeholder="10" value="10" min="1"/></div>
      <div><label>Transport (-t)</label>
        <select id="transport">
          <option value="u">UDP</option><option value="t">TCP</option>
          <option value="l">TLS</option><option value="w">WebSocket</option>
          <option value="wss">Secure WebSocket</option>
        </select>
      </div>
      <div><label>Scenario (-sf)</label>
        <select id="scenarioSelect">
          <option value="">-- built-in / file --</option>
          <option value="uac">uac (built-in)</option>
          <option value="uas">uas (built-in)</option>
          <option value="regexp">regexp (built-in)</option>
        </select>
      </div>
      <div><label>Extra Args</label><input id="extraArgs" placeholder="-trace_msg -aa"/></div>
    </div>
    <div style="margin-top:14px">
      <label>Inline Scenario XML (optional – overrides -sf)</label>
      <textarea id="scenarioXml" placeholder="Paste SIPp XML scenario here (optional)..."></textarea>
    </div>
    <div style="margin-top:10px">
      <div style="font-size:.78rem;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Command Preview</div>
      <div class="cmd-preview" id="cmdPreview">sipp ...</div>
    </div>
    <div class="btn-row">
      <button class="btn btn-primary" onclick="launchSipp()">▶ Launch</button>
      <button class="btn" style="background:#334155;color:#e2e8f0" onclick="updatePreview()">🔄 Preview</button>
    </div>
  </div>
</div>
</div>

<!-- ═══════════ TAB: JOBS ═══════════ -->
<div id="tab-jobs" class="tab-content">
<div class="container">
  <div class="card">
    <h2>📋 Running / Recent Jobs</h2>
    <div id="jobsTable"><p style="color:#64748b;font-size:.9rem">No jobs yet.</p></div>
    <div class="btn-row">
      <button class="btn btn-sm" style="background:#334155;color:#e2e8f0" onclick="refreshJobs()">�� Refresh</button>
      <button class="btn btn-sm btn-danger" onclick="killAll()">⛔ Kill All</button>
    </div>
  </div>
</div>
</div>

<!-- ═══════════ TAB: INSTALL ═══════════ -->
<div id="tab-install" class="tab-content">
<div class="container">
  <div class="card">
    <h2>⚙️ Install SIPp</h2>

    <div class="install-status" id="installStatusBar">
      <div class="dot dot-idle" id="installDot"></div>
      <div>
        <div id="installStatusText" style="font-weight:600;font-size:.95rem">Checking...</div>
        <div id="installStatusSub" style="font-size:.8rem;color:#94a3b8;margin-top:2px">Click "Check" to verify SIPp status</div>
      </div>
    </div>

    <p style="font-size:.88rem;color:#94a3b8;line-height:1.6;margin-bottom:14px">
      The installer will first attempt <code style="color:#a3e635">sudo apt-get install sipp</code>.
      If that fails, it will clone the <a href="https://github.com/SIPp/sipp" target="_blank" style="color:#38bdf8">SIPp GitHub repo</a>
      and compile it from source with SSL, SCTP, and PCAP support.
      <br/><strong style="color:#fbbf24">⚠ Requires sudo &amp; internet access on the server.</strong>
    </p>

    <div class="btn-row" style="margin-top:0">
      <button class="btn btn-success" onclick="startInstall()" id="btnInstall">⬇ Install SIPp</button>
      <button class="btn" style="background:#334155;color:#e2e8f0" onclick="checkSipp()">🔍 Check Status</button>
      <button class="btn" style="background:#334155;color:#e2e8f0" onclick="refreshInstallLog()">🔄 Refresh Log</button>
    </div>

    <div class="log-box" id="installLog">(install log will appear here)</div>
  </div>
</div>
</div>

<!-- Log Modal -->
<div id="logModal">
  <div class="modal-box">
    <div style="display:flex;align-items:center">
      <h3 id="modalTitle">Output</h3>
      <button class="btn btn-sm modal-close" style="background:#334155;color:#e2e8f0;margin-left:auto" onclick="closeModal()">✕ Close</button>
    </div>
    <div class="log-box" id="modalLog">Loading...</div>
    <button class="btn btn-sm" style="background:#334155;color:#e2e8f0;width:fit-content" onclick="refreshModal()">🔄 Refresh</button>
  </div>
</div>

<script>
let currentJobId = null;

// ── Tabs ─────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'jobs') refreshJobs();
  if (name === 'install') { checkSipp(); refreshInstallLog(); }
}

// ── Command Builder ───────────────────
function buildCmd() {
  const v = id => document.getElementById(id).value.trim();
  let cmd = 'sipp';
  const xml = v('scenarioXml'), scen = v('scenarioSelect');
  if (xml)       cmd += ' -sf /tmp/sipp_scenario.xml';
  else if (scen) cmd += ` -sf ${scen}`;
  if (v('sipUser'))   cmd += ` -s ${v('sipUser')}`;
  if (v('sipPass'))   cmd += ` -ap ${v('sipPass')}`;
  if (v('localIp'))   cmd += ` -i ${v('localIp')}`;
  if (v('localPort')) cmd += ` -p ${v('localPort')}`;
  cmd += ` -r ${v('callRate')||'1'} -m ${v('maxCalls')||'10'} -l ${v('concCalls')||'10'}`;
  cmd += ` -t ${v('transport')}`;
  if (v('extraArgs')) cmd += ` ${v('extraArgs')}`;
  const host = v('remoteHost') || '<remote_host>';
  cmd += ` ${host}:${v('remotePort')||'5060'}`;
  return cmd;
}
function updatePreview() {
  document.getElementById('cmdPreview').textContent = buildCmd();
}
document.querySelectorAll('input,select,textarea').forEach(el => el.addEventListener('input', updatePreview));
updatePreview();

// ── Launch ────────────────────────────
function launchSipp() {
  const host = document.getElementById('remoteHost').value.trim();
  if (!host) { alert('Remote Host is required.'); return; }
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
  fetch('/api/launch', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).then(r=>r.json()).then(d => {
    if (d.error) { alert('Error: ' + d.error); return; }
    alert('✅ Job #' + d.job_id + ' launched!\n' + d.cmd);
    switchTab('jobs');
    document.querySelectorAll('.tab')[1].classList.add('active');
    document.querySelectorAll('.tab')[0].classList.remove('active');
  });
}

// ── Jobs ──────────────────────────────
function refreshJobs() {
  fetch('/api/jobs').then(r=>r.json()).then(jobs => {
    const el = document.getElementById('jobsTable');
    if (!jobs.length) { el.innerHTML='<p style="color:#64748b;font-size:.9rem">No jobs yet.</p>'; return; }
    let h = '<table><thead><tr><th>#</th><th>Command</th><th>Status</th><th>PID</th><th>Actions</th></tr></thead><tbody>';
    jobs.forEach(j => {
      const badge = j.status==='running'
        ? '<span class="badge badge-running">▶ Running</span>'
        : j.status==='error'
        ? '<span class="badge badge-error">✕ Error</span>'
        : '<span class="badge badge-done">✓ Done</span>';
      const kill = j.status==='running'
        ? `<button class="btn btn-sm btn-danger" onclick="killJob(${j.job_id})">Kill</button>` : '';
      h += `<tr>
        <td>${j.job_id}</td>
        <td style="font-family:monospace;font-size:.76rem;max-width:400px;word-break:break-all">${j.cmd}</td>
        <td>${badge}</td><td>${j.pid||'-'}</td>
        <td style="display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn btn-sm" style="background:#334155;color:#e2e8f0" onclick="showLog(${j.job_id})">Logs</button>
          ${kill}
        </td></tr>`;
    });
    el.innerHTML = h + '</tbody></table>';
  });
}
function killJob(id) {
  fetch('/api/kill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:id})})
    .then(()=>refreshJobs());
}
function killAll() {
  fetch('/api/kill_all',{method:'POST'}).then(()=>refreshJobs());
}
function showLog(id) {
  currentJobId = id;
  document.getElementById('modalTitle').textContent = 'Job #' + id + ' Output';
  document.getElementById('logModal').classList.add('open');
  refreshModal();
}
function refreshModal() {
  if (currentJobId===null) return;
  fetch('/api/log?job_id='+currentJobId).then(r=>r.json()).then(d=>{
    const el = document.getElementById('modalLog');
    el.textContent = d.output||'(no output yet)';
    el.scrollTop = el.scrollHeight;
  });
}
function closeModal() {
  document.getElementById('logModal').classList.remove('open');
  currentJobId = null;
}

// ── Install ───────────────────────────
function checkSipp() {
  fetch('/api/sipp_status').then(r=>r.json()).then(d=>{
    const dot  = document.getElementById('installDot');
    const text = document.getElementById('installStatusText');
    const sub  = document.getElementById('installStatusSub');
    dot.className = 'dot ' + (d.installed ? 'dot-done' : 'dot-error');
    text.textContent = d.installed ? '✅  SIPp is installed' : '❌  SIPp is NOT installed';
    sub.textContent  = d.installed ? `Path: ${d.path}  |  Version: ${d.version}` : 'Use the button below to install it.';
    document.getElementById('btnInstall').disabled = d.installed;
    if (d.installed) document.getElementById('btnInstall').textContent = '✓ Already Installed';
  });
}
function startInstall() {
  fetch('/api/install_sipp',{method:'POST'}).then(r=>r.json()).then(d=>{
    if (d.error) { alert(d.error); return; }
    alert('Installation started! Watch the log below.');
    pollInstallLog();
  });
}
function refreshInstallLog() {
  fetch('/api/install_log').then(r=>r.json()).then(d=>{
    const el = document.getElementById('installLog');
    el.textContent = d.log || '(no log yet)';
    el.scrollTop = el.scrollHeight;
    const dot  = document.getElementById('installDot');
    const text = document.getElementById('installStatusText');
    if (d.status==='running') { dot.className='dot dot-running'; text.textContent='⏳ Installing...'; }
    else if (d.status==='done')  { dot.className='dot dot-done';    text.textContent='✅ Installation complete!'; checkSipp(); }
    else if (d.status==='error') { dot.className='dot dot-error';   text.textContent='❌ Installation failed'; }
  });
}
function pollInstallLog() {
  refreshInstallLog();
  fetch('/api/install_log').then(r=>r.json()).then(d=>{
    if (d.status==='running') setTimeout(pollInstallLog, 1500);
    else checkSipp();
  });
}

// Auto-refresh
setInterval(refreshJobs, 3000);
refreshJobs();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────
class SippHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

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

    # ── GET ──────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path in ("/", "/index.html"):
            self.send_html(HTML_PAGE)

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
                self.send_json({"error": "missing job_id"}, 400); return
            with lock:
                job = running_processes.get(jid)
            if not job:
                self.send_json({"error": "job not found"}, 404); return
            self.send_json({"output": "".join(job["output"])})

        elif path == "/api/sipp_status":
            installed = sipp_is_installed()
            path_str  = shutil.which("sipp") or ""
            version   = ""
            if installed:
                try:
                    r = subprocess.run(["sipp", "-v"], capture_output=True, text=True, timeout=5)
                    version = (r.stdout + r.stderr).strip().splitlines()[0]
                except Exception:
                    version = "unknown"
            self.send_json({"installed": installed, "path": path_str, "version": version})

        elif path == "/api/install_log":
            with lock:
                log_copy = "".join(install_log)
                st       = install_status
            self.send_json({"log": log_copy, "status": st})

        else:
            self.send_response(404); self.end_headers()

    # ── POST ─────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path

        # ── Launch SIPp job ──
        if path == "/api/launch":
            global job_counter
            params = self.read_json_body()
            if not params.get("remote_host"):
                self.send_json({"error": "remote_host is required"}, 400); return
            try:
                cmd = build_sipp_command(params)
            except Exception as e:
                self.send_json({"error": str(e)}, 400); return
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except FileNotFoundError:
                self.send_json({"error": "sipp not found. Go to the Install tab to install it."}, 500); return
            except Exception as e:
                self.send_json({"error": str(e)}, 500); return
            with lock:
                job_counter += 1
                jid = job_counter
                running_processes[jid] = {
                    "cmd": " ".join(cmd), "status": "running",
                    "pid": proc.pid, "output": [], "proc": proc,
                }
            threading.Thread(target=stream_output, args=(jid, proc), daemon=True).start()
            self.send_json({"job_id": jid, "pid": proc.pid, "cmd": " ".join(cmd)})

        # ── Kill one job ──
        elif path == "/api/kill":
            params = self.read_json_body()
            with lock:
                job = running_processes.get(params.get("job_id"))
            if not job:
                self.send_json({"error": "job not found"}, 404); return
            try:
                job["proc"].terminate()
                job["status"] = "done"
                self.send_json({"killed": params["job_id"]})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        # ── Kill all jobs ──
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

        # ── Install SIPp ──
        elif path == "/api/install_sipp":
            if sipp_is_installed():
                self.send_json({"error": "SIPp is already installed."}); return
            ok = start_install()
            if not ok:
                self.send_json({"error": "Installation is already running."}); return
            self.send_json({"started": True})

        else:
            self.send_response(404); self.end_headers()


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    PORT = 8000
    server = http.server.HTTPServer(("0.0.0.0", PORT), SippHandler)
    print(f"✅  SIPp Web UI  →  http://0.0.0.0:{PORT}")
    installed = sipp_is_installed()
    print(f"   SIPp installed: {'YES (' + shutil.which('sipp') + ')' if installed else 'NO  → open the Install tab'}")
    print("   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑  Stopped.")
        server.server_close()
