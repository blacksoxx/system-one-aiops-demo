"""A microservice that does nothing, on purpose.

Every service in the demo shop runs this file. It answers JSON on every path, checks its
dependencies on /healthz, and carries three fault switches so an incident is one `kubectl`
command away:

  CONFIG       JSON string. An unknown field makes the service exit(1) at start-up, which
               Kubernetes turns into CrashLoopBackOff.          -> incident "bad-config"
  MEM_HOG_MB   allocate this many MiB at start-up; with a small memory limit the kernel
               kills the container.                               -> incident "oom"
  DEPENDS_ON   comma list of name=url. If a dependency is unreachable, /healthz returns 503
               (readiness fails) and requests return 504.        -> incident "db-down"

Standard library only.
"""

import json
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME = os.environ.get("SERVICE_NAME", "service")
PORT = int(os.environ.get("PORT", "8080"))
DEPENDS_ON = [d for d in os.environ.get("DEPENDS_ON", "").split(",") if d.strip()]
ALLOWED_CONFIG = {"timeout_ms", "retries", "retry_policy", "feature_flags"}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {NAME}: {msg}", flush=True)


# ── start-up: config validation, then optional memory hog ──────────────────
config = json.loads(os.environ.get("CONFIG", "{}"))
unknown = sorted(set(config) - ALLOWED_CONFIG)
if unknown:
    log(f"invalid config: unknown field '{unknown[0]}' in config.yaml line 14")
    log("exiting")
    sys.exit(1)

_hog = []
mem_hog_mb = int(os.environ.get("MEM_HOG_MB", "0"))
if mem_hog_mb:
    log(f"warming in-memory cache: allocating {mem_hog_mb} MiB")
    for i in range(mem_hog_mb):
        _hog.append(bytearray(1024 * 1024))
        _hog[-1][0] = 1  # touch the page so it is really resident
        if i % 50 == 49:
            log(f"cache warm-up {i + 1}/{mem_hog_mb} MiB")
        time.sleep(0.02)


def check_dependencies():
    results = {}
    for dep in DEPENDS_ON:
        name, url = dep.split("=", 1)
        try:
            urllib.request.urlopen(url, timeout=2)
            results[name] = "ok"
        except Exception:
            results[name] = "unreachable"
            log(f"connection timed out after 2000 ms ({name})")
    return results


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        deps = check_dependencies()
        down = [n for n, s in deps.items() if s != "ok"]
        if self.path == "/healthz":
            self._send(503 if down else 200, {"service": NAME, "ok": not down, "dependencies": deps})
            return
        if down:
            log(f"GET {self.path} 504 (dependency {down[0]} unreachable)")
            self._send(504, {"service": NAME, "ok": False, "error": f"dependency {down[0]} unreachable"})
            return
        self._send(200, {"service": NAME, "ok": True, "dependencies": deps, "ts": int(time.time())})

    do_POST = do_GET

    def log_message(self, fmt, *args):  # keep the pod logs quiet on success
        pass


if __name__ == "__main__":
    log(f"listening on :{PORT}" + (f", depends on {', '.join(d.split('=')[0] for d in DEPENDS_ON)}" if DEPENDS_ON else ""))
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
