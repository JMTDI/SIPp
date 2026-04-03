#!/usr/bin/env python3
"""
server.py
---------
Self-bootstrapping reverse proxy that:
  1. Installs all Python dependencies using only `python -m pip` (no sudo, no apt).
  2. Pulls and starts the krndwr/easysipp:latest Docker container
     (docker pull krndwr/easysipp:latest && docker run -dt --network host --name easysipp krndwr/easysipp)
     The container's internal web UI defaults to port 8080.
  3. Exposes a reverse proxy on port 8000 → forwards to the easySIPp container at localhost:8080.

Requirements on the host:
  - python3 (3.6+)
  - docker CLI must be available in PATH (the server calls it as a subprocess)
  - Internet access (to pull the Docker image and pip packages)

Usage:
  python server.py
"""

import sys
import subprocess
import importlib


# ─────────────────────────────────────────────────────────────
# STEP 1 — Bootstrap: install all dependencies via `python -m pip`
# ───────────────────────────────────────────────────────────���─

REQUIRED_PACKAGES = {
    "flask": "flask",
    "requests": "requests",
    "werkzeug": "werkzeug",
}


def pip_install(package_name: str) -> None:
    """Install a package using `python -m pip install` (no sudo needed)."""
    print(f"[bootstrap] Installing '{package_name}' ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--user", package_name],
        check=False,
    )
    if result.returncode != 0:
        # Retry without --user (some envs like venvs don't want --user)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", package_name],
            check=True,
        )


def ensure_dependencies() -> None:
    """Check each required package; install it if missing."""
    # Make sure pip itself is available
    subprocess.run(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(import_name)
            print(f"[bootstrap] '{import_name}' already available.")
        except ImportError:
            pip_install(pip_name)
            # Reload site packages so the newly installed module is found
            import site
            importlib.reload(site)


ensure_dependencies()


# ─────────────────────────────────────────────────────────────
# STEP 2 — Now that deps are installed, import them
# ─────────────────────────────────────────────────────────────

import time
import signal
import logging
import threading
from urllib.parse import urlparse

import requests
from flask import Flask, request, Response, stream_with_context
from werkzeug.serving import make_server

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

EASYSIPP_CONTAINER_NAME = "easysipp"
EASYSIPP_IMAGE           = "krndwr/easysipp:latest"
EASYSIPP_INTERNAL_PORT   = 8080          # port easySIPp listens on inside the container
PROXY_PORT               = 8000          # the port THIS server exposes
EASYSIPP_BASE_URL        = f"http://127.0.0.1:{EASYSIPP_INTERNAL_PORT}"
DOCKER_START_TIMEOUT     = 60            # seconds to wait for easySIPp to become ready

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("easysipp-proxy")


# ─────────────────────────────────────────────────────────────
# STEP 3 — Docker helpers
# ─────────────────────────────────────────────────────────────

def docker(*args, check=True, capture=False) -> subprocess.CompletedProcess:
    """Run a docker command."""
    cmd = ["docker"] + list(args)
    log.info("Running: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )


def container_exists(name: str) -> bool:
    result = docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}", capture=True, check=False)
    return name in (result.stdout or "")


def container_running(name: str) -> bool:
    result = docker("ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}", capture=True, check=False)
    return name in (result.stdout or "")


def pull_image() -> None:
    log.info("Pulling Docker image: %s", EASYSIPP_IMAGE)
    docker("pull", EASYSIPP_IMAGE)


def start_container() -> None:
    """Pull image (if needed) and start the easySIPp container."""
    pull_image()

    if container_running(EASYSIPP_CONTAINER_NAME):
        log.info("Container '%s' is already running.", EASYSIPP_CONTAINER_NAME)
        return

    if container_exists(EASYSIPP_CONTAINER_NAME):
        log.info("Container '%s' exists but is stopped — removing it.", EASYSIPP_CONTAINER_NAME)
        docker("rm", "-f", EASYSIPP_CONTAINER_NAME, check=False)

    log.info("Starting container '%s' ...", EASYSIPP_CONTAINER_NAME)
    docker(
        "run", "-dt",
        "--network", "host",
        "--name", EASYSIPP_CONTAINER_NAME,
        EASYSIPP_IMAGE,
    )
    log.info("Container started.")


def wait_for_easysipp(timeout: int = DOCKER_START_TIMEOUT) -> None:
    """Poll until easySIPp HTTP endpoint is reachable or timeout expires."""
    log.info("Waiting for easySIPp to be ready at %s ...", EASYSIPP_BASE_URL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(EASYSIPP_BASE_URL, timeout=2)
            if r.status_code < 500:
                log.info("easySIPp is ready (HTTP %s).", r.status_code)
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(2)
    log.warning(
        "easySIPp did not respond within %ds — proxy will start anyway and retry on each request.",
        timeout,
    )


# ─────────────────────────────────────────────────────────────
# STEP 4 — Flask reverse proxy
# ─────────────────────────────────────────────────────────���───

app = Flask(__name__)

# Headers that must NOT be forwarded (hop-by-hop)
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
}


def filter_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.route("/<path:path>",            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def proxy(path: str) -> Response:
    target_url = f"{EASYSIPP_BASE_URL}/{path}"
    if request.query_string:
        target_url += "?" + request.query_string.decode("utf-8")

    req_headers = filter_headers(dict(request.headers))
    req_headers["Host"] = f"127.0.0.1:{EASYSIPP_INTERNAL_PORT}"

    try:
        upstream = requests.request(
            method=request.method,
            url=target_url,
            headers=req_headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=30,
        )
    except requests.exceptions.ConnectionError as exc:
        log.error("Cannot reach easySIPp: %s", exc)
        return Response(
            "<h2>easySIPp is not reachable yet. Is the container running?</h2>"
            f"<pre>Target: {target_url}</pre>",
            status=502,
            content_type="text/html",
        )

    resp_headers = filter_headers(dict(upstream.headers))

    return Response(
        stream_with_context(upstream.iter_content(chunk_size=4096)),
        status=upstream.status_code,
        headers=resp_headers,
        content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
    )


# ─────────────────────────────────────────────────────────────
# STEP 5 — Graceful shutdown
# ─────────────────────────────────────────────────────────────

class ProxyServer:
    def __init__(self):
        self._server = make_server("0.0.0.0", PROXY_PORT, app)
        self._thread  = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        log.info("Reverse proxy listening on http://0.0.0.0:%d", PROXY_PORT)

    def stop(self):
        log.info("Shutting down proxy server ...")
        self._server.shutdown()


def main():
    # ── 1. Start Docker container
    start_container()

    # ── 2. Wait for easySIPp web UI to be reachable
    wait_for_easysipp()

    # ── 3. Start reverse proxy
    proxy_server = ProxyServer()
    proxy_server.start()

    # ── 4. Handle Ctrl-C / SIGTERM gracefully
    def _shutdown(signum, frame):
        log.info("Signal %d received — stopping.", signum)
        proxy_server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "\n"
        "  ┌─────────────────────────────────────────────┐\n"
        "  │  easySIPp proxy is running                  │\n"
        "  │  Open:  http://<your-server-ip>:8000        │\n"
        "  │  Proxies to easySIPp container on :8080     │\n"
        "  └─────────────────────────────────────────────┘"
    )

    # Keep the main thread alive
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
