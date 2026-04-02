#!/usr/bin/env python3
"""
SIPp Web UI — Python-only, port 8000
SIPp binary is downloaded automatically on first run.
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
import urllib.request
import stat
import tarfile
from urllib.parse import parse_qs, urlparse

# ─────────────────────────────────────────────────────────────────────────────
# SIPp — download pre-built binary on startup
# ─────────────────────────────────────────────────────────────────────────────
SIPP_BIN_DIR = os.path.expanduser("~/.local/bin")
SIPP_BIN     = os.path.join(SIPP_BIN_DIR, "sipp")

# Pre-built static binary from the official SIPp GitHub releases
# (linux x86_64 static build — no deps needed)
SIPP_RELEASE_URL = (
    "https://github.com/SIPp/sipp/releases/download/v3.7.2/"
    "sipp-3.7.2-linux-amd64-static.tar.gz"
)


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


def download_sipp():
    """Download the pre-built static SIPp binary using only Python stdlib."""
    _print("\n" + "═" * 60)
    _print("  SIPp not found — downloading pre-built binary...")
    _print("═" * 60)

    os.makedirs(SIPP_BIN_DIR, exist_ok=True)

    tarball = "/tmp/sipp.tar.gz"
    _print(f"\n[1/3] Downloading SIPp from:\n  {SIPP_RELEASE_URL}")

    def _progress(block_num, block_size, total_size):
        if total_size > 0:
            pct = min(100, block_num * block_size * 100 // total_size)
            sys.stdout.write(f"\r    {pct}% ")
            sys.stdout.flush()

    urllib.request.urlretrieve(SIPP_RELEASE_URL, tarball, reporthook=_progress)
    sys.stdout.write("\n")

    _print(f"\n[2/3] Extracting archive to {SIPP_BIN_DIR} ...")
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar.getmembers():
            # extract only the sipp binary (ignore paths)
            if os.path.basename(member.name) == "sipp" and member.isfile():
                member.name = "sipp"
                tar.extract(member, SIPP_BIN_DIR)
                break
        else:
            raise RuntimeError("sipp binary not found inside the tarball")

    _print(f"\n[3/3] Making {SIPP_BIN} executable ...")
    st = os.stat(SIPP_BIN)
    os.chmod(SIPP_BIN, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Clean up
    os.remove(tarball)

    _print(f"\n✅  SIPp downloaded → {SIPP_BIN}\n")
    _print("═" * 60 + "\n")


def ensure_sipp():
    """
    Called once at startup.
    If sipp is already on PATH, skip download.
    Otherwise, download a pre-built static binary into ~/.local/bin.
    """
    if shutil.which("sipp"):
        path = shutil.which("sipp")
        _print(f"\n✅  SIPp already installed → {path}  (skipping download)\n")
        return

    # Also check our own download location
    if os.path.isfile(SIPP_BIN) and os.access(SIPP_BIN, os.X_OK):
        _print(f"\n✅  SIPp already downloaded → {SIPP_BIN}  (skipping download)\n")
        # Ensure it's on PATH for this process
        _prepend_bin_to_path()
        return

    try:
        download_sipp()
    except Exception as e:
        _print(f"\n❌  Download failed: {e}")
        _print("    Check your internet connection and restart the server.")
        sys.exit(1)

    _prepend_bin_to_path()

    if not shutil.which("sipp"):
        _print("\n❌  Download succeeded but 'sipp' is still not found.")
        _print(f"    Binary is at: {SIPP_BIN}")
        sys.exit(1)


def _prepend_bin_to_path():
    """Add ~/.local/bin to PATH for this process if not already there."""
    if SIPP_BIN_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = SIPP_BIN_DIR + os.pathsep + os.environ.get("PATH", "")


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

    remote_host = params.get("remote_host", "").strip()
    if remote_host:
        cmd.append(remote_host)

    mapping = {
        "transport":    "-t",
        "calls":        "-l",
        "rate":         "-r",
        "duration":     "-d",
        "local_port":   "-p",
        "local_ip":     "-i",
        "auth_uri":     "-au",
        "auth_passwd":  "-ap",
        "service":      "-s",
    }
    for key, flag in mapping.items():
        val = params.get(key, "").strip()
        if val:
            cmd += [flag, val]

    extra = params.get("extra_args", "").strip()
    if extra:
        cmd += shlex.split(extra)

    return cmd


# ─────────────────────────────────────────────────────────────────────────────
# Stream job output into memory
# ─────────────────────────────────────────────────────────────────────────────
def stream_output(job_id: int, proc):
    for line in iter(proc.stdout.readline, b""):
        decoded = line.decode("utf-8", errors="replace")
        with lock:
            if job_id in running_processes:
                running_processes[job_id]["output"].append(decoded)
    proc.wait()
    with lock:
        if job_id in running_processes:
            running_processes[job_id]["status"] = "done"


# ─────────────────────────────────────────────────────────────────────────────
# HTML UI
# ─────────────────────────────────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>SIPp Web UI</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding:2rem}
  h1{color:#38bdf8;margin-bottom:1.5rem;font-size:1.8rem}
  .card{background:#1e293b;border-radius:.75rem;padding:1.5rem;margin-bottom:1.5rem}
  h2{color:#94a3b8;font-size:1rem;text-transform:uppercase;letter-spacing:.05em;margin-bottom:1rem}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:.75rem}
  label{display:flex;flex-direction:column;gap:.25rem;font-size:.875rem;color:#94a3b8}
  input,select,textarea{background:#0f172a;border:1px solid #334155;border-radius:.375rem;
    color:#e2e8f0;padding:.5rem .75rem;font-size:.875rem;width:100%}
  textarea{resize:vertical;min-height:120px;font-family:monospace}
  .btn{cursor:pointer;border:none;border-radius:.5rem;padding:.6rem 1.4rem;
    font-size:.875rem;font-weight:600;transition:opacity .15s}
  .btn-blue{background:#0284c7;color:#fff} .btn-blue:hover{opacity:.85}
  .btn-red{background:#dc2626;color:#fff}  .btn-red:hover{opacity:.85}
  .btn-gray{background:#334155;color:#e2e8f0} .btn-gray:hover{opacity:.85}
  .btn-row{display:flex;gap:.75rem;flex-wrap:wrap;margin-top:1rem}
  #jobs{display:flex;flex-direction:column;gap:.75rem}
  .job{background:#0f172a;border:1px solid #334155;border-radius:.5rem;padding:1rem}
  .job-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:.5rem}
  .badge{display:inline-block;padding:.15rem .5rem;border-radius:9999px;font-size:.75rem;font-weight:600}
  .badge-running{background:#166534;color:#bbf7d0} .badge-done{background:#334155;color:#94a3b8}
  .log{background:#020617;color:#a3e635;font-family:monospace;font-size:.75rem;
    border-radius:.375rem;padding:.75rem;max-height:220px;overflow-y:auto;white-space:pre-wrap;margin-top:.5rem}
  #ver{color:#64748b;font-size:.8rem;margin-bottom:1rem}
</style>
</head>
<body>
<h1>🚀 SIPp Web UI</h1>
<div id="ver">Checking SIPp version…</div>

<div class="card">
  <h2>Launch SIPp Test</h2>
  <div class="grid">
    <label>Remote Host (required)
      <input id="remote_host" placeholder="192.168.1.1:5060"/>
    </label>
    <label>Transport
      <select id="transport">
        <option value="">default</option>
        <option value="u1">UDP (u1)</option>
        <option value="t1">TCP (t1)</option>
        <option value="l1">TLS (l1)</option>
      </select>
    </label>
    <label>Max Calls (-l)
      <input id="calls" placeholder="100"/>
    </label>
    <label>Call Rate (-r)
      <input id="rate" placeholder="10"/>
    </label>
    <label>Duration ms (-d)
      <input id="duration" placeholder="5000"/>
    </label>
    <label>Local Port (-p)
      <input id="local_port" placeholder="5060"/>
    </label>
    <label>Local IP (-i)
      <input id="local_ip" placeholder=""/>
    </label>
    <label>Auth User (-au)
      <input id="auth_uri" placeholder=""/>
    </label>
    <label>Auth Pass (-ap)
      <input id="auth_passwd" type="password" placeholder=""/>
    </label>
    <label>Service (-s)
      <input id="service" placeholder="service"/>
    </label>
  </div>
  <label style="margin-top:.75rem">Scenario file path (-sf) — leave blank to use inline XML below
    <input id="scenario" placeholder="/path/to/scenario.xml"/>
  </label>
  <label style="margin-top:.75rem">Inline Scenario XML (overrides path above)
    <textarea id="scenario_xml" placeholder="Paste XML here…"></textarea>
  </label>
  <label style="margin-top:.75rem">Extra CLI args
    <input id="extra_args" placeholder="-timeout 30s -aa"/>
  </label>
  <div class="btn-row">
    <button class="btn btn-blue" onclick="launch()">▶ Launch</button>
    <button class="btn btn-red"  onclick="killAll()">⏹ Kill All</button>
  </div>
</div>

<div class="card">
  <h2>Active / Recent Jobs</h2>
  <div id="jobs"><em style="color:#64748b">No jobs yet.</em></div>
</div>

<script>
const $=id=>document.getElementById(id);
async function api(path,body){
  const opts=body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{};
  const r=await fetch(path,opts);return r.json();
}
async function launch(){
  const params={
    remote_host:$('remote_host').value.trim(),
    transport:$('transport').value,
    calls:$('calls').value.trim(),
    rate:$('rate').value.trim(),
    duration:$('duration').value.trim(),
    local_port:$('local_port').value.trim(),
    local_ip:$('local_ip').value.trim(),
    auth_uri:$('auth_uri').value.trim(),
    auth_passwd:$('auth_passwd').value.trim(),
    service:$('service').value.trim(),
    scenario:$('scenario').value.trim(),
    scenario_xml:$('scenario_xml').value.trim(),
    extra_args:$('extra_args').value.trim(),
  };
  const res=await api('/api/launch',params);
  if(res.error){alert('Error: '+res.error);return;}
  alert('Launched job #'+res.job_id+' PID '+res.pid);
  refreshJobs();
}
async function killJob(id){await api('/api/kill',{job_id:id});refreshJobs();}
async function killAll(){await api('/api/kill_all',{});refreshJobs();}
async function refreshJobs(){
  const jobs=await api('/api/jobs');
  const el=$('jobs');
  if(!jobs.length){el.innerHTML='<em style="color:#64748b">No jobs yet.</em>';return;}
  el.innerHTML=jobs.map(j=>`
    <div class="job">
      <div class="job-header">
        <span><strong>#${j.job_id}</strong> PID ${j.pid??'—'}</span>
        <span class="badge badge-${j.status}">${j.status}</span>
      </div>
      <code style="font-size:.75rem;color:#64748b;word-break:break-all">${j.cmd}</code>
      <div class="btn-row">
        <button class="btn btn-gray" onclick="showLog(${j.job_id})">📋 Log</button>
        ${j.status==='running'?`<button class="btn btn-red" onclick="killJob(${j.job_id})">⏹ Kill</button>`:''}
      </div>
      <div id="log-${j.job_id}" class="log" style="display:none"></div>
    </div>`).join('');
}
async function showLog(id){
  const el=document.getElementById('log-'+id);
  if(el.style.display==='none'){
    const r=await api('/api/log?job_id='+id);
    el.textContent=r.output||'(no output yet)';
    el.style.display='block';
    el.scrollTop=el.scrollHeight;
  } else {el.style.display='none';}
}
(async()=>{
  const r=await api('/api/sipp_version').catch(()=>({version:'SIPp ready'}));
  $('ver').textContent='SIPp: '+r.version;
})();
setInterval(refreshJobs,3000);
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
                ver = shutil.which("sipp") or SIPP_BIN
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
