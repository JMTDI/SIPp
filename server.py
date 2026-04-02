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
from urllib.parse import parse_qs, urlparse

# ─────────────────────────────────────────────────────────────────────────────
# SIPp — download pre-built binary on startup
# ─────────────────────────────────────────────────────────────────────────────
SIPP_BIN_DIR = os.path.expanduser("~/.local/bin")
SIPP_BIN     = os.path.join(SIPP_BIN_DIR, "sipp")

# Direct pre-built binary from the official SIPp GitHub releases (v3.7.7)
# Asset name is just "sipp" — no tar, no zip, no static suffix
SIPP_RELEASE_URL = (
    "https://github.com/SIPp/sipp/releases/download/v3.7.7/sipp"
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
    """Download the pre-built SIPp binary from GitHub releases using urllib (pure Python)."""
    _print("\n" + "═" * 60)
    _print("  SIPp not found — downloading pre-built binary...")
    _print("═" * 60)

    os.makedirs(SIPP_BIN_DIR, exist_ok=True)

    _print(f"\n[1/3] Downloading SIPp binary from:")
    _print(f"  {SIPP_RELEASE_URL}")

    tmp_path = SIPP_BIN + ".tmp"
    try:
        urllib.request.urlretrieve(SIPP_RELEASE_URL, tmp_path)
    except Exception as e:
        raise RuntimeError(f"Download failed: {e}")

    _print(f"\n[2/3] Installing to {SIPP_BIN} ...")
    os.replace(tmp_path, SIPP_BIN)

    _print("\n[3/3] Making binary executable...")
    current = os.stat(SIPP_BIN).st_mode
    os.chmod(SIPP_BIN, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    _print(f"\n✅  SIPp downloaded and ready → {SIPP_BIN}\n")


def ensure_sipp():
    """
    Called once at startup.
    If sipp is already on PATH, skip the download entirely.
    Otherwise, download the pre-built binary from GitHub releases.
    """
    # Also check our own bin dir in case it was downloaded before
    if shutil.which("sipp") or os.path.isfile(SIPP_BIN):
        path = shutil.which("sipp") or SIPP_BIN
        _print(f"\n✅  SIPp already installed → {path}  (skipping download)\n")
        # Ensure our bin dir is on PATH for this process
        _prepend_local_bin()
        return

    download_sipp()
    _prepend_local_bin()


def _prepend_local_bin():
    """Ensure ~/.local/bin is on PATH so `sipp` can be found by subprocess calls."""
    if SIPP_BIN_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = SIPP_BIN_DIR + os.pathsep + os.environ.get("PATH", "")


# ─────────────────────────────────────────────────────────────────────────────
# Job tracking
# ─────────────────────────────────────────────────────────────────────────────
running_processes: dict = {}
job_counter = 0
lock = threading.Lock()


def build_sipp_command(params: dict) -> list:
    cmd = [SIPP_BIN if os.path.isfile(SIPP_BIN) else "sipp"]

    remote_host = params.get("remote_host", "").strip()
    if not remote_host:
        raise ValueError("remote_host is required")
    cmd.append(remote_host)

    if params.get("scenario"):
        cmd += ["-sf", params["scenario"]]
    if params.get("transport"):
        cmd += ["-t", params["transport"]]
    if params.get("local_port"):
        cmd += ["-p", str(params["local_port"])]
    if params.get("call_rate"):
        cmd += ["-r", str(params["call_rate"])]
    if params.get("call_limit"):
        cmd += ["-l", str(params["call_limit"])]
    if params.get("call_count"):
        cmd += ["-m", str(params["call_count"])]
    if params.get("timeout"):
        cmd += ["-timeout", str(params["timeout"])]
    if params.get("extra_args"):
        cmd += shlex.split(params["extra_args"])

    return cmd


def stream_output(job_id: int, proc):
    for line in iter(proc.stdout.readline, b""):
        text = line.decode("utf-8", errors="replace")
        with lock:
            running_processes[job_id]["output"].append(text)
    proc.wait()
    with lock:
        running_processes[job_id]["status"] = (
            "done" if proc.returncode == 0 else f"failed({proc.returncode})"
        )


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
  body{font-family:monospace;background:#1e1e2e;color:#cdd6f4;margin:0;padding:1rem}
  h1{color:#89b4fa}
  label{display:block;margin:.4rem 0 .1rem;color:#a6e3a1}
  input,select,textarea{background:#313244;color:#cdd6f4;border:1px solid #585b70;
    border-radius:4px;padding:.3rem .5rem;width:100%;box-sizing:border-box}
  button{margin:.3rem .2rem 0 0;padding:.4rem 1rem;border:none;border-radius:4px;
    cursor:pointer;font-family:monospace}
  .btn-go{background:#89b4fa;color:#1e1e2e}
  .btn-stop{background:#f38ba8;color:#1e1e2e}
  .btn-refresh{background:#a6e3a1;color:#1e1e2e}
  #log{background:#11111b;padding:.6rem;border-radius:4px;height:320px;
    overflow-y:auto;white-space:pre-wrap;font-size:.82rem;margin-top:.6rem}
  #jobs{margin-top:1rem}
  table{border-collapse:collapse;width:100%}
  th,td{text-align:left;padding:.3rem .6rem;border-bottom:1px solid #313244}
  th{color:#89b4fa}
  .status-done{color:#a6e3a1}.status-running{color:#f9e2af}.status-failed{color:#f38ba8}
  #version{font-size:.8rem;color:#6c7086;margin-bottom:.8rem}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
</style>
</head>
<body>
<h1>🔭 SIPp Web UI</h1>
<div id="version">checking sipp version…</div>

<div class="grid">
  <div>
    <label>Remote Host (required)</label>
    <input id="remote_host" placeholder="sip.example.com or 192.168.1.1[:5060]"/>
  </div>
  <div>
    <label>Scenario file (-sf)</label>
    <input id="scenario" placeholder="uac.xml  (leave blank for built-in UAC)"/>
  </div>
  <div>
    <label>Transport (-t)</label>
    <select id="transport">
      <option value="">default (u1)</option>
      <option value="u1">UDP mono (u1)</option>
      <option value="un">UDP multi (un)</option>
      <option value="t1">TCP mono (t1)</option>
      <option value="tn">TCP multi (tn)</option>
      <option value="l1">TLS mono (l1)</option>
    </select>
  </div>
  <div>
    <label>Local port (-p)</label>
    <input id="local_port" placeholder="5060"/>
  </div>
  <div>
    <label>Call rate / sec (-r)</label>
    <input id="call_rate" placeholder="10"/>
  </div>
  <div>
    <label>Max concurrent calls (-l)</label>
    <input id="call_limit" placeholder="100"/>
  </div>
  <div>
    <label>Total calls (-m)</label>
    <input id="call_count" placeholder="1000"/>
  </div>
  <div>
    <label>Timeout seconds (-timeout)</label>
    <input id="timeout" placeholder="30"/>
  </div>
</div>
<label>Extra args</label>
<input id="extra_args" placeholder="-trace_msg -aa …"/>

<div>
  <button class="btn-go" onclick="launch()">▶ Launch</button>
  <button class="btn-stop" onclick="stopJob()">■ Stop job</button>
  <button class="btn-refresh" onclick="refreshJobs()">↻ Refresh jobs</button>
</div>

<div id="jobs">
  <h3>Jobs</h3>
  <table><thead><tr><th>ID</th><th>Status</th><th>PID</th><th>Command</th><th>Log</th></tr></thead>
  <tbody id="job_rows"></tbody></table>
</div>

<div>
  <b>Log — job <span id="log_job_id">—</span></b>
  <button class="btn-refresh" onclick="pollLog()">↻ Refresh log</button>
</div>
<div id="log">Select a job log above…</div>

<script>
let currentJobId = null;

async function api(method, path, body){
  const opts = {method, headers:{"Content-Type":"application/json"}};
  if(body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

async function launch(){
  const params = {
    remote_host: document.getElementById("remote_host").value.trim(),
    scenario:    document.getElementById("scenario").value.trim(),
    transport:   document.getElementById("transport").value,
    local_port:  document.getElementById("local_port").value.trim(),
    call_rate:   document.getElementById("call_rate").value.trim(),
    call_limit:  document.getElementById("call_limit").value.trim(),
    call_count:  document.getElementById("call_count").value.trim(),
    timeout:     document.getElementById("timeout").value.trim(),
    extra_args:  document.getElementById("extra_args").value.trim(),
  };
  const res = await api("POST", "/api/launch", params);
  if(res.error){ alert("Error: "+res.error); return; }
  currentJobId = res.job_id;
  document.getElementById("log_job_id").textContent = currentJobId;
  refreshJobs();
  setTimeout(pollLog, 800);
}

async function stopJob(){
  if(currentJobId === null){ alert("No job selected"); return; }
  const res = await api("POST", "/api/stop", {job_id: currentJobId});
  alert(res.message || res.error);
  refreshJobs();
}

async function refreshJobs(){
  const jobs = await api("GET", "/api/jobs");
  const tbody = document.getElementById("job_rows");
  tbody.innerHTML = "";
  jobs.forEach(j => {
    const cls = j.status === "running" ? "status-running"
              : j.status === "done"    ? "status-done" : "status-failed";
    tbody.innerHTML += `<tr>
      <td>${j.job_id}</td>
      <td class="${cls}">${j.status}</td>
      <td>${j.pid ?? "—"}</td>
      <td style="font-size:.75rem;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${j.cmd.join(" ")}</td>
      <td><button class="btn-refresh" onclick="showLog(${j.job_id})">view</button></td>
    </tr>`;
  });
}

async function showLog(jid){
  currentJobId = jid;
  document.getElementById("log_job_id").textContent = jid;
  await pollLog();
}

async function pollLog(){
  if(currentJobId === null) return;
  const res = await api("GET", `/api/log?job_id=${currentJobId}`);
  const el = document.getElementById("log");
  el.textContent = res.output ?? res.error;
  el.scrollTop = el.scrollHeight;
}

// version banner
(async()=>{
  try{
    const v = await api("GET","/api/sipp_version");
    document.getElementById("version").textContent = v.version;
  }catch(e){
    document.getElementById("version").textContent = "version unknown";
  }
})();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# HTTP handler
# ────────────────────────────────────────���────────────────────────────────────
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
                    [SIPP_BIN if os.path.isfile(SIPP_BIN) else "sipp", "-v"],
                    capture_output=True, text=True, timeout=5
                )
                raw = (r.stdout + r.stderr).strip()
                ver = raw.splitlines()[0] if raw else "SIPp ready"
            except Exception:
                ver = shutil.which("sipp") or SIPP_BIN if os.path.isfile(SIPP_BIN) else "SIPp ready"
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
            except Exception as e:
                self.send_json({"error": f"Failed to start sipp: {e}"}, 500)
                return
            with lock:
                job_counter += 1
                jid = job_counter
                running_processes[jid] = {
                    "cmd": cmd,
                    "status": "running",
                    "pid": proc.pid,
                    "output": [],
                }
            t = threading.Thread(target=stream_output, args=(jid, proc), daemon=True)
            t.start()
            self.send_json({"job_id": jid, "pid": proc.pid, "cmd": cmd})

        elif path == "/api/stop":
            body = self.read_json_body()
            try:
                jid = int(body.get("job_id", -1))
            except (TypeError, ValueError):
                self.send_json({"error": "invalid job_id"}, 400)
                return
            with lock:
                job = running_processes.get(jid)
            if not job:
                self.send_json({"error": "job not found"}, 404)
                return
            try:
                pid = job.get("pid")
                if pid:
                    os.kill(pid, 15)  # SIGTERM
                with lock:
                    running_processes[jid]["status"] = "stopped"
                self.send_json({"message": f"Job {jid} stopped"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ensure_sipp()

    PORT = int(os.environ.get("PORT", 8000))
    server = http.server.HTTPServer(("0.0.0.0", PORT), SippHandler)
    _print(f"🌐  SIPp Web UI listening on http://0.0.0.0:{PORT}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _print("\nShutting down.")
