#!/usr/bin/env python3
"""
server.py
---------
Self-bootstrapping easySIPp launcher — NO Docker, NO sudo, NO apt required.

What it does:
  1. Installs all Python dependencies via `python -m pip` only.
  2. Downloads the easySIPp Django/ASGI source from GitHub (pure Python urllib).
  3. Patches settings to disable CSRF (local-use tool — safe and intended).
  4. Runs migrations + load_initial_if_empty + collectstatic.
  5. Starts the app with uvicorn (ASGI) on 0.0.0.0:8000.

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

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

EASYSIPP_REPO_ZIP  = "https://github.com/kiran-daware/easySIPp/archive/refs/heads/main.zip"
EASYSIPP_DIR_NAME  = "easySIPp-main"   # top-level folder name inside the zip
WORK_DIR           = os.path.join(os.path.dirname(os.path.abspath(__file__)), "easysipp_app")
MANAGE_PY          = os.path.join(WORK_DIR, "manage.py")
SETTINGS_PY        = os.path.join(WORK_DIR, "easySIPp_project", "settings.py")
LOCAL_SETTINGS_PY  = os.path.join(WORK_DIR, "easySIPp_project", "local_settings.py")
ASGI_APP           = "easySIPp_project.asgi:application"
HOST               = "0.0.0.0"
PORT               = 8000

# Minimal packages needed before we can read requirements.txt
BOOTSTRAP_PACKAGES = {
    "django":   "django==4.2.21",
    "requests": "requests",
}

# ─────────────────────────────────────────────────────────────
# STEP 1 — Bootstrap pip + install packages
# ─────────────────────────────────────────────────────────────

def pip_install(*pkg_args: str) -> None:
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + list(pkg_args)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        subprocess.run(cmd, check=True)


def ensure_packages(packages: dict) -> None:
    subprocess.run(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for import_name, pip_spec in packages.items():
        try:
            importlib.import_module(import_name)
            print(f"[bootstrap] '{import_name}' OK")
        except ImportError:
            print(f"[bootstrap] Installing '{pip_spec}' ...")
            pip_install(pip_spec)
            import site; importlib.reload(site)
    print("[bootstrap] Bootstrap complete.")


ensure_packages(BOOTSTRAP_PACKAGES)

# ─────────────────────────────────────────────────────────────
# STEP 2 — Imports safe now
# ─────────────────────────────────────────────────────────────

import time
import signal
import logging
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("easysipp")

# ─────────────────────────────────────────────────────────────
# STEP 3 — Download & extract easySIPp source
# ─────────────────────────────────────────────────────────────

def download_easysipp() -> None:
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


def install_requirements() -> None:
    req = os.path.join(WORK_DIR, "requirements.txt")
    if not os.path.isfile(req):
        log.warning("No requirements.txt — skipping.")
        return
    log.info("Installing easySIPp requirements ...")
    pip_install("-r", req)
    log.info("Requirements installed.")


# ─────────────────────────────────────────────────────────────
# STEP 4 — Patch settings to disable CSRF entirely
#
# easySIPp is a local-only tool (ALLOWED_HOSTS = ['*'] upstream).
# CSRF protection is meaningless without authentication and adds
# friction for any non-localhost origin.  We write a
# local_settings.py that removes CsrfViewMiddleware and opens
# CSRF_TRUSTED_ORIGINS to all origins, then ensure settings.py
# imports it at the end.  Both operations are idempotent.
# ─────────────────────────────────────────────────────────────

LOCAL_SETTINGS_CONTENT = """
# local_settings.py — written by server.py (auto-generated, do not edit manually)
# Disables CSRF for local/self-hosted use where any IP may connect.

# Remove CSRF middleware from whatever the base settings defined
MIDDLEWARE = [m for m in MIDDLEWARE if 'csrf' not in m.lower()]

# Accept requests from any origin
ALLOWED_HOSTS = ['*']
CSRF_TRUSTED_ORIGINS = []  # irrelevant — middleware is removed above

# Show errors clearly during self-hosted use
DEBUG = True
"""

SETTINGS_IMPORT_LINE = "\ntry:\n    from .local_settings import *  # noqa: F401,F403\nexcept ImportError:\n    pass\n"


def patch_settings() -> None:
    if not os.path.isfile(SETTINGS_PY):
        log.warning("settings.py not found at %s — skipping patch.", SETTINGS_PY)
        return

    # Always (re)write local_settings.py so it stays current
    with open(LOCAL_SETTINGS_PY, "w") as f:
        f.write(LOCAL_SETTINGS_CONTENT)
    log.info("Wrote local_settings.py (CSRF disabled, DEBUG=True).")

    # Inject the import into settings.py exactly once
    with open(SETTINGS_PY, "r") as f:
        content = f.read()

    if "local_settings" not in content:
        with open(SETTINGS_PY, "a") as f:
            f.write(SETTINGS_IMPORT_LINE)
        log.info("Injected local_settings import into settings.py.")
    else:
        log.info("settings.py already imports local_settings — skipping injection.")


# ─────────────────────────────────────────────────────────────
# STEP 5 — Django setup helpers
# ─────────────────────────────────────────────────────────────

def django_manage(*args) -> None:
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "easySIPp_project.settings"
    subprocess.run(
        [sys.executable, MANAGE_PY] + list(args),
        cwd=WORK_DIR,
        env=env,
        check=True,
    )


def setup_django() -> None:
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
# STEP 6 — Run uvicorn (ASGI server)
# ─────────────────────────────────────────────────────────────

server_proc: subprocess.Popen = None


def start_server() -> None:
    global server_proc
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "easySIPp_project.settings"

    cmd = [
        sys.executable, "-m", "uvicorn",
        ASGI_APP,
        "--host", HOST,
        "--port", str(PORT),
        "--workers", "2",
    ]
    log.info("Starting uvicorn: %s", " ".join(cmd))
    server_proc = subprocess.Popen(cmd, cwd=WORK_DIR, env=env)
    log.info(
        "\n"
        "  ┌────────────────────────────────────────────────┐\n"
        "  │  easySIPp is running                           │\n"
        "  │  Open: http://<your-server-ip>:%d             │\n"
        "  └────────────────────────────────────────────────┘",
        PORT,
    )


def wait_for_server(timeout: int = 60) -> None:
    url = f"http://127.0.0.1:{PORT}/"
    log.info("Waiting for server to be ready at %s ...", url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            log.info("Server is ready.")
            return
        except Exception:
            time.sleep(1)
    log.warning("Server did not respond within %ds — it may still be starting.", timeout)

# ─────────────────────────────────────────────────────────────
# STEP 7 — Graceful shutdown
# ───────────────────────────────────────────────────────────���─

def shutdown(signum=None, frame=None) -> None:
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

def main() -> None:
    download_easysipp()
    install_requirements()
    patch_settings()       # disable CSRF before starting
    setup_django()
    start_server()
    wait_for_server()

    # Watchdog: restart if uvicorn dies
    while True:
        code = server_proc.poll()
        if code is not None:
            log.error("Server exited with code %d — restarting ...", code)
            start_server()
        time.sleep(3)

if __name__ == "__main__":
    main()