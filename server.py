#!/usr/bin/env python3
"""
server.py
---------
Self-bootstrapping easySIPp launcher — NO Docker, NO sudo, NO apt required.

What it does:
  1. Installs all Python dependencies via `python -m pip` only.
  2. Clones the easySIPp Django source from GitHub (using Python's urllib — no git needed).
  3. Runs Django migrations and starts the Django dev server on port 8000.

Requirements on the host:
  - python3 (3.6+)
  - Internet access (to pip-install packages and download easySIPp source)
  - The 'unzip' binary OR pure-Python zipfile extraction (handled automatically).

Usage:
  python server.py
"""

import sys
import subprocess
import importlib
import os
import zipfile
import shutil

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

EASYSIPP_REPO_ZIP   = "https://github.com/kiran-daware/easySIPp/archive/refs/heads/main.zip"
EASYSIPP_DIR_NAME   = "easySIPp-main"       # name inside the zip
EASYSIPP_WORK_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "easysipp_app")
MANAGE_PY           = os.path.join(EASYSIPP_WORK_DIR, "manage.py")
HOST                = "0.0.0.0"
PORT                = 8000

# Packages to install before anything else
BOOTSTRAP_PACKAGES = {
    "django":    "django",
    "requests":  "requests",
}

# ─────────────────────────────────────────────────────────────
# STEP 1 — Bootstrap pip + install dependencies
# ─────────────────────────────────────────────────────────────

def pip_install(package_name: str) -> None:
    print(f"[bootstrap] Installing '{package_name}' ...")
    # Try --user first (works outside venvs), fall back to plain install
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--user", package_name],
        check=False,
    )
    if result.returncode != 0:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", package_name],
            check=True,
        )


def ensure_dependencies(packages: dict) -> None:
    # Ensure pip is present
    subprocess.run(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for import_name, pip_name in packages.items():
        try:
            importlib.import_module(import_name)
            print(f"[bootstrap] '{import_name}' already available.")
        except ImportError:
            pip_install(pip_name)
            import site
            importlib.reload(site)
    print("[bootstrap] All dependencies satisfied.")


ensure_dependencies(BOOTSTRAP_PACKAGES)

# ─────────────────────────────────────────────────────────────
# STEP 2 — Real imports (safe now that deps are installed)
# ─────────────────────────────────────────────────────────────

import time
import signal
import logging
import threading
import urllib.request
import io

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("easysipp-server")

# ─────────────────────────────────────────────────────────────
# STEP 3 — Download & extract easySIPp source (if needed)
# ─────────────────────────────────────────────────────────────

def download_and_extract_easysipp() -> None:
    if os.path.isfile(MANAGE_PY):
        log.info("easySIPp source already present at: %s", EASYSIPP_WORK_DIR)
        return

    log.info("Downloading easySIPp source from GitHub ...")
    zip_bytes = urllib.request.urlopen(EASYSIPP_REPO_ZIP).read()
    log.info("Download complete (%d KB). Extracting ...", len(zip_bytes) // 1024)

    # Extract to a temp location first
    tmp_dir = EASYSIPP_WORK_DIR + "_tmp"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(tmp_dir)

    # The zip contains a single top-level folder (easySIPp-main)
    extracted = os.path.join(tmp_dir, EASYSIPP_DIR_NAME)
    if not os.path.isdir(extracted):
        # Fallback: find whatever directory is in tmp_dir
        contents = os.listdir(tmp_dir)
        if contents:
            extracted = os.path.join(tmp_dir, contents[0])

    shutil.move(extracted, EASYSIPP_WORK_DIR)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    log.info("easySIPp source extracted to: %s", EASYSIPP_WORK_DIR)


def install_easysipp_requirements() -> None:
    req_file = os.path.join(EASYSIPP_WORK_DIR, "requirements.txt")
    if not os.path.isfile(req_file):
        log.warning("No requirements.txt found in easySIPp source — skipping.")
        return

    log.info("Installing easySIPp requirements from requirements.txt ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--user", "-r", req_file],
        check=False,
    )
    if result.returncode != 0:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "-r", req_file],
            check=True,
        )
    log.info("easySIPp requirements installed.")


def run_migrations() -> None:
    log.info("Running Django migrations ...")
    subprocess.run(
        [sys.executable, MANAGE_PY, "migrate", "--run-syncdb"],
        cwd=EASYSIPP_WORK_DIR,
        check=True,
    )
    log.info("Migrations complete.")


# ─────────────────────────────────────────────────────────────
# STEP 4 — Run Django dev server
# ─────────────────────────────────────────────────────────────

django_process: subprocess.Popen = None


def start_django() -> None:
    global django_process
    bind_addr = f"{HOST}:{PORT}"
    log.info("Starting Django server on %s ...", bind_addr)
    django_process = subprocess.Popen(
        [sys.executable, MANAGE_PY, "runserver", bind_addr, "--noreload"],
        cwd=EASYSIPP_WORK_DIR,
    )
    log.info(
        "\n"
        "  ┌───────────────────────────────────────────────────┐\n"
        "  │  easySIPp is running (Django)                     │\n"
        "  │  Open:  http://<your-server-ip>:%d               │\n"
        "  └───────────────────────────────────────────────────┘",
        PORT,
    )


def wait_for_django(timeout: int = 30) -> None:
    import urllib.error
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{PORT}/"
    log.info("Waiting for Django to become ready at %s ...", url)
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            log.info("Django is ready.")
            return
        except Exception:
            time.sleep(1)
    log.warning("Django did not respond within %ds — it may still be starting.", timeout)


# ─────────────────────────────────────────────────────────────
# STEP 5 — Graceful shutdown
# ─────────────────────────────────────────────────────────────

def shutdown(signum=None, frame=None) -> None:
    global django_process
    log.info("Shutting down ...")
    if django_process and django_process.poll() is None:
        django_process.terminate()
        try:
            django_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            django_process.kill()
    sys.exit(0)

signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main() -> None:
    # 1. Download easySIPp source
    download_and_extract_easysipp()

    # 2. Install its Python dependencies
    install_easysipp_requirements()

    # 3. Run Django migrations (safe to run repeatedly)
    run_migrations()

    # 4. Launch Django on port 8000
    start_django()

    # 5. Wait until it's accepting requests
    wait_for_django()

    # 6. Keep alive and monitor the child process
    while True:
        ret = django_process.poll()
        if ret is not None:
            log.error("Django process exited with code %d — restarting ...", ret)
            start_django()
        time.sleep(2)


if __name__ == "__main__":
    main()