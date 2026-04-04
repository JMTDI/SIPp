#!/usr/bin/env python3
"""
server.py
---------
Self-bootstrapping easySIPp launcher — NO Docker, NO sudo, NO apt required.

devpush.app pattern (same as Odoo):
  - Python reverse proxy binds :8000 IMMEDIATELY (devpush health-check passes)
  - uvicorn runs internally on :8080
  - Proxy forwards every request :8000 → :8080

What it does:
  1. Binds :8000 right away with a loading-page proxy (devpush won't time out).
  2. Installs all Python dependencies via `python -m pip` only.
  3. Downloads the easySIPp Django/ASGI source from GitHub (pure Python urllib).
  4. Patches settings to disable CSRF (local-use tool — safe and intended).
  5. Runs migrations + load_initial_if_empty + collectstatic.
  6. Starts uvicorn (ASGI) on 0.0.0.0:8080 (internal).
  7. Proxy flips to live-forward mode once uvicorn is ready.

Requirements on the host:
  - python3 (3.6+)
  - Internet access

Usage:
  python server.py
"""

import sys
import subprocess
import importlib
import os
import zipfile
import shutil
import io
import threading
import time
import signal
import logging
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

EASYSIPP_REPO_ZIP  = "https://github.com/kiran-daware/easySIPp/archive/refs/heads/main.zip"
EASYSIPP_DIR_NAME  = "easySIPp-main"
WORK_DIR           = os.path.join(os.path.dirname(os.path.abspath(__file__)), "easysipp_app")
MANAGE_PY          = os.path.join(WORK_DIR, "manage.py")
SETTINGS_PY        = os.path.join(WORK_DIR, "easySIPp_project", "settings.py")
ASGI_APP           = "easySIPp_project.asgi:application"
HOST               = "0.0.0.0"
PUBLIC_PORT        = 8000   # devpush always probes/routes this — Python owns it immediately
INTERNAL_PORT      = 8080   # uvicorn listens here

BOOTSTRAP_PACKAGES = {
    "django":   "django==4.2.21",
    "requests": "requests",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("easysipp")

# ─────────────────────────────────────────────────────────────
# REVERSE PROXY  (lives on :8000 the entire time)
# ─────────────────────────────────────────────────────────────

class ReverseProxyHandler(BaseHTTPRequestHandler):
    """
    Before uvicorn is ready → returns a friendly 'starting…' page.
    After uvicorn is ready  → forwards everything to :8080.
    """
    ready = False   # flipped to True once uvicorn responds

    def _forward(self):
        if not ReverseProxyHandler.ready:
            body = (
                b"<html><body style='font-family:sans-serif;padding:2rem'>"
                b"<h2>easySIPp is starting, please wait\xe2\x80\xa6</h2>"
                b"<p>The app is being set up. This page refreshes automatically.</p>"
                b"<script>setTimeout(()=>location.reload(),3000)</script>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        target = f"http://127.0.0.1:{INTERNAL_PORT}{self.path}"
        try:
            skip = {"connection", "keep-alive", "transfer-encoding",
                    "te", "trailers", "upgrade",
                    "proxy-authorization", "proxy-authenticate"}
            fwd_headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in skip}

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else None

            req = urllib.request.Request(
                target, data=body, headers=fwd_headers, method=self.command
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("connection", "transfer-encoding", "keep-alive"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except Exception as exc:
            msg = f"<html><body><h2>Proxy error: {exc}</h2></body></html>".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    do_GET     = _forward
    do_POST    = _forward
    do_PUT     = _forward
    do_DELETE  = _forward
    do_PATCH   = _forward
    do_HEAD    = _forward
    do_OPTIONS = _forward

    def log_message(self, fmt, *args):
        pass  # silence access logs


def start_proxy():
    server = HTTPServer((HOST, PUBLIC_PORT), ReverseProxyHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Reverse proxy listening on :%d  (loading page until uvicorn is ready)", PUBLIC_PORT)

# ─────────────────────────────────────────────────────────────
# STEP 1 — Bootstrap pip + install packages
# ─────────────────────────────────────────────────────────────

def pip_install(*pkg_args):
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + list(pkg_args)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        subprocess.run(cmd, check=True)


def ensure_packages(packages):
    subprocess.run(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for import_name, pip_spec in packages.items():
        try:
            importlib.import_module(import_name)
            print("[bootstrap] '" + import_name + "' OK")
        except ImportError:
            print("[bootstrap] Installing '" + pip_spec + "' ...")
            pip_install(pip_spec)
            import site; importlib.reload(site)
    print("[bootstrap] Bootstrap complete.")


# ─────────────────────────────────────────────────────────────
# STEP 2 — Download & extract easySIPp source
# ─────────────────────────────────────────────────────────────

def download_easysipp():
    if os.path.isfile(MANAGE_PY):
        log.info("easySIPp source already present — skipping download.")
        return

    log.info("Downloading easySIPp source from GitHub ...")
    data = urllib.request.urlopen(EASYSIPP_REPO_ZIP).read()
    log.info("Downloaded %d KB. Extracting ...", len(data) // 1024)

    tmp = WORK_DIR + "_tmp"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(tmp)

    src = os.path.join(tmp, EASYSIPP_DIR_NAME)
    if not os.path.isdir(src):
        entries = [e for e in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, e))]
        if entries:
            src = os.path.join(tmp, entries[0])

    shutil.move(src, WORK_DIR)
    shutil.rmtree(tmp, ignore_errors=True)
    log.info("Extracted to: %s", WORK_DIR)


def install_requirements():
    req = os.path.join(WORK_DIR, "requirements.txt")
    if not os.path.isfile(req):
        log.warning("No requirements.txt — skipping.")
        return
    log.info("Installing easySIPp requirements ...")
    pip_install("-r", req)
    log.info("Requirements installed.")


# ─────────────────────────────────────────────────────────────
# STEP 3 — Patch settings.py
# ─────────────────────────────────────────────────────────────

SETTINGS_PATCH_MARKER = "# >>> easysipp-server csrf-patch >>>"

SETTINGS_PATCH_LINES = [
    "\n",
    "# >>> easysipp-server csrf-patch >>>\n",
    "# Appended by server.py — removes CSRF middleware for local/self-hosted use.\n",
    "MIDDLEWARE = [m for m in MIDDLEWARE if 'csrf' not in m.lower()]\n",
    "ALLOWED_HOSTS = ['*']\n",
    "CSRF_TRUSTED_ORIGINS = ['https://*', 'http://*']\n",
    "DEBUG = True\n",
    "# <<< easysipp-server csrf-patch <<<\n",
]


def patch_settings():
    if not os.path.isfile(SETTINGS_PY):
        log.warning("settings.py not found at %s — skipping patch.", SETTINGS_PY)
        return

    with open(SETTINGS_PY, "r") as f:
        content = f.read()

    if SETTINGS_PATCH_MARKER in content:
        log.info("settings.py already patched — skipping.")
        return

    with open(SETTINGS_PY, "a") as f:
        f.writelines(SETTINGS_PATCH_LINES)

    log.info("Patched settings.py: CSRF removed, ALLOWED_HOSTS=*, DEBUG=True.")


# ─────────────────────────────────────────────────────────────
# STEP 4 — Django setup helpers
# ─────────────────────────────────────────────────────────────

def django_manage(*args):
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "easySIPp_project.settings"
    subprocess.run(
        [sys.executable, MANAGE_PY] + list(args),
        cwd=WORK_DIR,
        env=env,
        check=True,
    )


def setup_django():
    log.info("Running migrations ...")
    django_manage("migrate", "--noinput")

    log.info("Loading initial data ...")
    try:
        django_manage("load_initial_if_empty")
    except subprocess.CalledProcessError:
        log.warning("load_initial_if_empty failed or not available — continuing.")

    log.info("Collecting static files ...")
    django_manage("collectstatic", "--noinput", "--clear")

    log.info("Django setup complete.")


# ─────────────────────────────────────────────────────────────
# STEP 5 — Run uvicorn on internal port :8080
# ─────────────────────────────────────────────────────────────

server_proc = None


def start_server():
    global server_proc
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "easySIPp_project.settings"

    cmd = [
        sys.executable, "-m", "uvicorn",
        ASGI_APP,
        "--host", "127.0.0.1",    # internal only — proxy handles :8000
        "--port", str(INTERNAL_PORT),
        "--workers", "2",
    ]
    log.info("Starting uvicorn: %s", " ".join(cmd))
    server_proc = subprocess.Popen(cmd, cwd=WORK_DIR, env=env)
    log.info("uvicorn started on :%d (internal)", INTERNAL_PORT)


def wait_for_uvicorn(timeout=120):
    url = f"http://127.0.0.1:{INTERNAL_PORT}/"
    log.info("Waiting for uvicorn to be ready at %s ...", url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            log.info("uvicorn is ready — flipping proxy to live mode.")
            ReverseProxyHandler.ready = True
            return
        except Exception:
            time.sleep(1)
    log.warning("uvicorn did not respond within %ds — enabling proxy anyway.", timeout)
    ReverseProxyHandler.ready = True


# ─────────────────────────────────────────────────────────────
# STEP 6 — Graceful shutdown
# ─────────────────────────────────────────────────────────────

def shutdown(signum=None, frame=None):
    log.info("Shutting down ...")
    if server_proc and server_proc.poll() is None:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
    sys.exit(0)

signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    # Bind :8000 IMMEDIATELY so devpush health-check always gets a response
    start_proxy()

    # Now do all the slow setup work
    ensure_packages(BOOTSTRAP_PACKAGES)
    download_easysipp()
    install_requirements()
    patch_settings()
    setup_django()

    # Start uvicorn on :8080, wait for it, then flip proxy to live mode
    start_server()
    wait_for_uvicorn()

    log.info("easySIPp is live at http://<your-server>:%d", PUBLIC_PORT)

    # Keep alive — restart uvicorn if it crashes
    while True:
        code = server_proc.poll()
        if code is not None:
            log.error("uvicorn exited with code %d — restarting ...", code)
            start_server()
            wait_for_uvicorn()
        time.sleep(3)

if __name__ == "__main__":
    main()