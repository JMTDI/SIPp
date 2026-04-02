#!/usr/bin/env python3
"""
SIPp Web UI - Python-only server on port 8000
Run: python3 server.py
"""

import http.server
import json
import subprocess
import threading
import os
import signal
import shlex
from urllib.parse import parse_qs, urlparse

# ─────────────────────────────────────────────
# Global state
# ─────────────────────────────────────────────
running_processes = {}   # { job_id: {"proc": Popen, "cmd": str, "output": [str]} }
job_counter = 0
lock = threading.Lock()

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>SIPp Web UI</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  header { background: #1e293b; padding: 18px 32px; border-bottom: 2px solid #334155;
           display: flex; align-items: center; gap: 14px; }
  header h1 { font-size: 1.5rem; font-weight: 700; color: #38bdf8; letter-spacing: 1px; }
  header span { background: #0ea5e9; color: #fff; border-radius: 6px; padding: 2px 10px; font-size: 0.78rem; }
  .container { max-width: 1100px; margin: 30px auto; padding: 0 16px; }
  .card { background: #1e293b; border-radius: 12px; padding: 24px; margin-bottom: 24px;
          border: 1px solid #334155; }
  .card h2 { font-size: 1.1rem; color: #7dd3fc; margin-bottom: 16px; border-bottom: 1px solid #334155;
             padding-bottom: 8px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
  label { display: block; font-size: 0.82rem; color: #94a3b8; margin-bottom: 4px; }
  input, select, textarea {
    width: 100%; padding: 8px 12px; border-radius: 8px; border: 1px solid #475569;
    background: #0f172a; color: #e2e8f0; font-size: 0.9rem; outline: none;
    transition: border-color 0.2s;
  }
  input:focus, select:focus, textarea:focus { border-color: #38bdf8; }
  textarea { resize: vertical; min-height: 70px; font-family: monospace; font-size: 0.82rem; }
  .cmd-preview { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 12px;
                 font-family: monospace; font-size: 0.82rem; color: #a3e635; word-break: break-all;
                 margin-top: 10px; min-height: 38px; }
  .btn { padding: 10px 24px; border-radius: 8px; border: none; cursor: pointer; font-size: 0.9rem;
         font-weight: 600; transition: all 0.15s; }
  .btn-primary { background: #0ea5e9; color: #fff; }
  .btn-primary:hover { background: #0284c7; }
  .btn-danger  { background: #ef4444; color: #fff; }
  .btn-danger:hover  { background: #dc2626; }
  .btn-sm { padding: 5px 14px; font-size: 0.8rem; }
  .btn-row { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; align-items: center; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  th { background: #0f172a; color: #7dd3fc; padding: 10px 12px; text-align: left; }
  td { padding: 9px 12px; border-bottom: 1px solid #1e293b; vertical-align: middle; }
  tr:hover td { background: #1e293b44; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 600; }
  .badge-running { background: #166534; color: #86efac; }
  .badge-done    { background: #1e3a5f; color: #93c5fd; }
  .badge-error   { background: #7f1d1d; color: #fca5a5; }
  .log-box { background: #020617; border: 1px solid #334155; border-radius: 8px; padding: 12px;
             font-family: monospace; font-size: 0.78rem; color: #86efac; max-height: 340px;
             overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin-top: 10px; }
  .section-title { color: #94a3b8; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 1px;
                   margin-bottom: 8px; }
  #logModal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:#000a;
              z-index:100; justify-content: center; align-items: center; }
  #logModal.open { display:flex; }
  .modal-box { background:#1e293b; border-radius:12px; padding:24px; width:90%; max-width:800px;
               max-height:85vh; display:flex; flex-direction:column; gap:10px; }
  .modal-box h3 { color:#38bdf8; }
  .modal-close { margin-left:auto; }
  #scenarioFile { display:none; }
  .file-label { display:inline-block; padding:8px 16px; background:#334155; border-radius:8px;
                cursor:pointer; font-size:0.85rem; color:#cbd5e1; border:1px solid #475569; }
  .file-label:hover { background:#475569; }
  #fileNameDisplay { font-size:0.82rem; color:#94a3b8; margin-left:8px; }
</style>
</head>
<body>
<header>
  <h1>📞 SIPp Web UI</h1>
  <span>Port 8000</span>
</header>
<div class="container">

  <!-- Quick Launch Card -->
  <div class="card">
    <h2>🚀 Launch SIPp Test</h2>
    <div class="grid">
      <div>
        <label>Remote Host / IP *</label>
        <input id="remoteHost" placeholder="192.168.1.100" />
      </div>
      <div>
        <label>Remote Port</label>
        <input id="remotePort" placeholder="5060" value="5060" />
      </div>
      <div>
        <label>SIP Username (-s)</label>
        <input id="sipUser" placeholder="1001" />
      </div>
      <div>
        <label>Password (-ap)</label>
        <input id="sipPass" type="password" placeholder="secret" />
      </div>
      <div>
        <label>Local IP (-i)</label>
        <input id="localIp" placeholder="auto" />
      </div>
      <div>
        <label>Local Port (-p)</label>
        <input id="localPort" placeholder="5060" />
      </div>
      <div>
        <label>Calls/sec (-r)</label>
        <input id="callRate" type="number" placeholder="1" value="1" min="1" />
      </div>
      <div>
        <label>Max Calls (-m)</label>
        <input id="maxCalls" type="number" placeholder="10" value="10" min="1" />
      </div>
      <div>
        <label>Max Concurrent (-l)</label>
        <input id="concCalls" type="number" placeholder="10" value="10" min="1" />
      </div>
      <div>
        <label>Transport (-t)</label>
        <select id="transport">
          <option value="u">UDP</option>
          <option value="t">TCP</option>
          <option value="l">TLS</option>
          <option value="w">WebSocket</option>
          <option value="wss">Secure WebSocket</option>
        </select>
      </div>
      <div>
        <label>Scenario (-sf)</label>
        <div style="display:flex;align-items:center;gap:8px;">
          <select id="scenarioSelect" style="flex:1">
            <option value="">-- built-in / file --</option>
            <option value="uac">uac (built-in)</option>
            <option value="uas">uas (built-in)</option>
            <option value="regexp">regexp (built-in)</option>
          </select>
        </div>
      </div>
      <div>
        <label>Extra Args</label>
        <input id="extraArgs" placeholder="-trace_msg -aa" />
      </div>
    </div>

    <div style="margin-top:14px;">
      <label>Inline Scenario XML (optional – overrides -sf)</label>
      <textarea id="scenarioXml" placeholder="Paste SIPp XML scenario here (optional)..."></textarea>
    </div>

    <div style="margin-top:10px;">
      <div class="section-title">Command Preview</div>
      <div class="cmd-preview" id="cmdPreview">sipp ...</div>
    </div>

    <div class="btn-row">
      <button class="btn btn-primary" onclick="launchSipp()">▶ Launch</button>
      <button class="btn" style="background:#334155;color:#e2e8f0" onclick="updatePreview()">🔄 Refresh Preview</button>
    </div>
  </div>

  <!-- Running Jobs -->
  <div class="card">
    <h2>📋 Running / Recent Jobs</h2>
    <div id="jobsTable">
      <p style="color:#64748b;font-size:0.9rem;">No jobs yet. Launch a test above.</p>
    </div>
    <div class="btn-row">
      <button class="btn btn-sm" style="background:#334155;color:#e2e8f0" onclick="refreshJobs()">🔄 Refresh</button>
      <button class="btn btn-sm btn-danger" onclick="killAll()">⛔ Kill All</button>
    </div>
  </div>

</div>

<!-- Log Modal -->
<div id="logModal">
  <div class="modal-box">
    <div style="display:flex;align-items:center;">
      <h3 id="modalTitle">Output</h3>
      <button class="btn btn-sm modal-close" style="background:#334155;color:#e2e8f0" onclick="closeModal()">✕ Close</button>
    </div>
    <div class="log-box" id="modalLog">Loading...</div>
    <button class="btn btn-sm" style="background:#334155;color:#e2e8f0;width:fit-content" onclick="refreshModal()">🔄 Refresh</button>
  </div>
</div>

<script>
let currentJobId = null;

function buildCmd() {
  const host  = document.getElementById('remoteHost').value.trim();
  const port  = document.getElementById('remotePort').value.trim() || '5060';
  const user  = document.getElementById('sipUser').value.trim();
  const pass  = document.getElementById('sipPass').value.trim();
  const lIp   = document.getElementById('localIp').value.trim();
  const lPort = document.getElementById('localPort').value.trim();
  const rate  = document.getElementById('callRate').value.trim() || '1';
  const maxC  = document.getElementById('maxCalls').value.trim() || '10';
  const conc  = document.getElementById('concCalls').value.trim() || '10';
  const trans = document.getElementById('transport').value;
  const scen  = document.getElementById('scenarioSelect').value;
  const extra = document.getElementById('extraArgs').value.trim();
  const xml   = document.getElementById('scenarioXml').value.trim();

  let cmd = 'sipp';
  if (xml)        cmd += ' -sf /tmp/sipp_scenario.xml';
  else if (scen)  cmd += ` -sf ${scen}`;
  if (user)       cmd += ` -s ${user}`;
  if (pass)       cmd += ` -ap ${pass}`;
  if (lIp)        cmd += ` -i ${lIp}`;
  if (lPort)      cmd += ` -p ${lPort}`;
  cmd += ` -r ${rate} -m ${maxC} -l ${conc} -t ${trans}`;
  if (extra)      cmd += ` ${extra}`;
  if (host)       cmd += ` ${host}:${port}`;
  else            cmd += ' <remote_host>:' + port;
  return cmd;
}

function updatePreview() {
  document.getElementById('cmdPreview').textContent = buildCmd();
}

// Auto-update preview on any input change
document.querySelectorAll('input,select,textarea').forEach(el => {
  el.addEventListener('input', updatePreview);
});
updatePreview();

function launchSipp() {
  const host = document.getElementById('remoteHost').value.trim();
  if (!host) { alert('Remote Host is required.'); return; }

  const payload = {
    remote_host:   host,
    remote_port:   document.getElementById('remotePort').value.trim()   || '5060',
    sip_user:      document.getElementById('sipUser').value.trim(),
    sip_pass:      document.getElementById('sipPass').value.trim(),
    local_ip:      document.getElementById('localIp').value.trim(),
    local_port:    document.getElementById('localPort').value.trim(),
    call_rate:     document.getElementById('callRate').value.trim()      || '1',
    max_calls:     document.getElementById('maxCalls').value.trim()      || '10',
    conc_calls:    document.getElementById('concCalls').value.trim()     || '10',
    transport:     document.getElementById('transport').value,
    scenario:      document.getElementById('scenarioSelect').value,
    scenario_xml:  document.getElementById('scenarioXml').value.trim(),
    extra_args:    document.getElementById('extraArgs').value.trim(),
  };

  fetch('/api/launch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  })
  .then(r => r.json())
  .then(d => {
    if (d.error) { alert('Error: ' + d.error); return; }
    alert('Job #' + d.job_id + ' launched!');
    refreshJobs();
  });
}

function refreshJobs() {
  fetch('/api/jobs')
    .then(r => r.json())
    .then(jobs => {
      const el = document.getElementById('jobsTable');
      if (!jobs.length) {
        el.innerHTML = '<p style="color:#64748b;font-size:0.9rem;">No jobs yet.</p>';
        return;
      }
      let html = '<table><thead><tr>'
        + '<th>#</th><th>Command</th><th>Status</th><th>PID</th><th>Actions</th>'
        + '</tr></thead><tbody>';
      jobs.forEach(j => {
        const badge = j.status === 'running'
          ? '<span class="badge badge-running">▶ Running</span>'
          : j.status === 'error'
          ? '<span class="badge badge-error">✕ Error</span>'
          : '<span class="badge badge-done">✓ Done</span>';
        const killBtn = j.status === 'running'
          ? `<button class="btn btn-sm btn-danger" onclick="killJob(${j.job_id})">Kill</button>`
          : '';
        html += `<tr>
          <td>${j.job_id}</td>
          <td style="font-family:monospace;font-size:0.78rem;max-width:400px;word-break:break-all">${j.cmd}</td>
          <td>${badge}</td>
          <td>${j.pid || '-'}</td>
          <td style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="btn btn-sm" style="background:#334155;color:#e2e8f0" onclick="showLog(${j.job_id})">Logs</button>
            ${killBtn}
          </td>
        </tr>`;
      });
      html += '</tbody></table>';
      el.innerHTML = html;
    });
}

function killJob(id) {
  fetch('/api/kill', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({job_id: id})
  }).then(() => refreshJobs());
}

function killAll() {
  fetch('/api/kill_all', {method:'POST'}).then(() => refreshJobs());
}

function showLog(id) {
  currentJobId = id;
  document.getElementById('modalTitle').textContent = 'Job #' + id + ' Output';
  document.getElementById('logModal').classList.add('open');
  refreshModal();
}

function refreshModal() {
  if (currentJobId === null) return;
  fetch('/api/log?job_id=' + currentJobId)
    .then(r => r.json())
    .then(d => {
      const el = document.getElementById('modalLog');
      el.textContent = d.output || '(no output yet)';
      el.scrollTop = el.scrollHeight;
    });
}

function closeModal() {
  document.getElementById('logModal').classList.remove('open');
  currentJobId = null;
}

// Auto-refresh jobs every 3s
setInterval(refreshJobs, 3000);
refreshJobs();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
# Build SIPp command from params
# ─────────────────────────────────────────────
def build_sipp_command(params: dict) -> list:
    cmd = ["sipp"]

    xml = params.get("scenario_xml", "").strip()
    scenario = params.get("scenario", "").strip()

    if xml:
        # Write XML to temp file
        xml_path = "/tmp/sipp_scenario.xml"
        with open(xml_path, "w") as f:
            f.write(xml)
        cmd += ["-sf", xml_path]
    elif scenario:
        cmd += ["-sf", scenario]

    if params.get("sip_user"):
        cmd += ["-s", params["sip_user"]]
    if params.get("sip_pass"):
        cmd += ["-ap", params["sip_pass"]]
    if params.get("local_ip"):
        cmd += ["-i", params["local_ip"]]
    if params.get("local_port"):
        cmd += ["-p", params["local_port"]]

    cmd += ["-r",  params.get("call_rate",  "1")]
    cmd += ["-m",  params.get("max_calls",  "10")]
    cmd += ["-l",  params.get("conc_calls", "10")]
    cmd += ["-t",  params.get("transport",  "u")]

    extra = params.get("extra_args", "").strip()
    if extra:
        cmd += shlex.split(extra)

    remote = params["remote_host"] + ":" + params.get("remote_port", "5060")
    cmd.append(remote)

    return cmd


# ─────────────────────────────────────────────
# Stream output of a process into job record
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
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    # ── GET ──────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self.send_html(HTML_PAGE)

        elif path == "/api/jobs":
            with lock:
                jobs = []
                for jid, job in running_processes.items():
                    jobs.append({
                        "job_id": jid,
                        "cmd":    job["cmd"],
                        "status": job["status"],
                        "pid":    job.get("pid"),
                    })
            self.send_json(list(reversed(jobs)))  # newest first

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

    # ── POST ─────────────────────────────────
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
                self.send_json({"error": "sipp not found. Is SIPp installed and in PATH?"}, 500)
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

            t = threading.Thread(target=stream_output, args=(jid, proc), daemon=True)
            t.start()

            self.send_json({"job_id": jid, "pid": proc.pid, "cmd": " ".join(cmd)})

        elif path == "/api/kill":
            params = self.read_json_body()
            jid = params.get("job_id")
            with lock:
                job = running_processes.get(jid)
            if not job:
                self.send_json({"error": "job not found"}, 404)
                return
            try:
                job["proc"].terminate()
                job["status"] = "done"
                self.send_json({"killed": jid})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/kill_all":
            with lock:
                jobs = list(running_processes.values())
            for job in jobs:
                try:
                    if job["status"] == "running":
                        job["proc"].terminate()
                        job["status"] = "done"
                except Exception:
                    pass
            self.send_json({"killed": "all"})

        else:
            self.send_response(404)
            self.end_headers()


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    PORT = 8000
    server = http.server.HTTPServer(("0.0.0.0", PORT), SippHandler)
    print(f"✅  SIPp Web UI running at  http://0.0.0.0:{PORT}")
    print("    Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑  Server stopped.")
        server.server_close()
