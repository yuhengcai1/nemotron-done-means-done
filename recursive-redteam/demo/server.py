"""Loopback-only hackathon demo: recorded model evidence and local test replay."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_data import ROOT, RUN_ID, build_data, write_data, checked_run_id

STATIC = {
    "/": (ROOT / "demo/web/index.html", "text/html; charset=utf-8"),
    "/index.html": (ROOT / "demo/web/index.html", "text/html; charset=utf-8"),
    "/styles.css": (ROOT / "demo/web/styles.css", "text/css; charset=utf-8"),
    "/app.js": (ROOT / "demo/web/app.js", "text/javascript; charset=utf-8"),
    "/data/demo-data.js": (ROOT / "demo/data/demo-data.js", "text/javascript; charset=utf-8"),
    "/data/demo-data.json": (ROOT / "demo/data/demo-data.json", "application/json; charset=utf-8"),
}
def replay_command(run_id=RUN_ID):
    run_id = checked_run_id(run_id)
    return (sys.executable, str(ROOT / "examples/self_debug.py"),
            "--verify-run", str(ROOT / "runs" / run_id), "--json")


REPLAY_COMMAND = replay_command()
REPLAY_LOCK = threading.Lock()


def demo_data(run_id=RUN_ID):
    return build_data(run_id=checked_run_id(run_id))


def recall_command(run_id=RUN_ID):
    run_id = checked_run_id(run_id)
    run_path = ROOT / "runs" / run_id
    return (sys.executable, str(ROOT / "examples/recall_repair.py"), "--run", str(run_path),
            "--out", str(run_path / "memory-recall.json"))


def _local_operation(run_id, operation):
    """Execute one allowlisted local action; never forward provider credentials."""
    mode = "local-replay" if operation == "verify" else "local-recall"
    if not demo_data(run_id)["verification"]["available"]:
        return 409, {"ok": False, "mode": mode, "error": "The recorded run is not ready for local checks."}
    if not REPLAY_LOCK.acquire(blocking=False):
        return 409, {"ok": False, "mode": mode, "error": "A local verification or recall is already running."}
    try:
        environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR", "LANG") if key in os.environ}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        command = replay_command(run_id) if operation == "verify" else recall_command(run_id)
        child = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                               text=True, timeout=30, check=False)
        stdout, stderr = child.stdout[-100000:], child.stderr[-10000:]
        try:
            result = json.loads(stdout)
        except ValueError:
            result = None
        verified = isinstance(result, dict) and child.returncode == 0
        if operation == "verify":
            verified = verified and result.get("verified") is True
        else:
            verified = (verified and result.get("matched") is True and result.get("reusable") is True
                        and result.get("chain_valid") is True and result.get("hosted_model_calls") == 0
                        and isinstance(result.get("checks"), list) and len(result["checks"]) >= 2
                        and all(check.get("failed") == 0 and type(check.get("total")) is int
                                and check["total"] > 0 and check.get("passed") == check["total"]
                                for check in result["checks"]))
        return 200, {"ok": bool(verified), "mode": mode, "result": result,
                     "stdout": stdout, "stderr": stderr, "exit_code": child.returncode,
                     "note": ("Fresh local tests of retained artifacts; no model API call." if operation == "verify"
                              else "Fresh SQLite memory recall and local checks; no model API call.")}
    except subprocess.TimeoutExpired:
        return 504, {"ok": False, "mode": mode, "error": "Local checks exceeded 30 seconds."}
    except OSError as exc:
        return 500, {"ok": False, "mode": mode, "error": "Local checker could not start: " + str(exc)}
    finally:
        REPLAY_LOCK.release()


def local_replay(run_id=RUN_ID):
    return _local_operation(run_id, "verify")


def local_recall(run_id=RUN_ID):
    return _local_operation(run_id, "recall")


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "RecursiveDemo/1.0"

    def _run_id(self):
        return getattr(self.server, "run_id", RUN_ID)

    def _local_host(self):
        return self.headers.get("Host", "").lower() in {
            "127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}

    def _trusted_mutation(self):
        if not self._local_host():
            return False
        origin = self.headers.get("Origin")
        allowed = {"http://127.0.0.1:" + str(self.server.server_port),
                   "http://localhost:" + str(self.server.server_port)}
        return (not origin or origin in allowed) and self.headers.get("Sec-Fetch-Site") != "cross-site"

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._local_host():
            self._send(403, {"error": "Local host required"})
            return
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._send(200, {"ok": True, "mode": "recorded", "host": "127.0.0.1", "run_id": self._run_id()})
        elif path == "/api/demo":
            try:
                self._send(200, demo_data(self._run_id()))
            except (OSError, ValueError, TypeError):
                self._send(503, {"error": "Recorded run data is being written or is invalid; retry shortly."})
        elif path in STATIC:
            file_path, content_type = STATIC[path]
            try:
                if path.startswith("/data/"):
                    write_data(demo_data(self._run_id()))
                self._send(200, file_path.read_bytes(), content_type)
            except FileNotFoundError:
                self._send(404, {"error": "Demo asset is not available yet"})
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._trusted_mutation():
            self._send(403, {"error": "Same-origin local request required"})
            return
        path = urlsplit(self.path).path
        if path not in {"/api/verify", "/api/recall"}:
            self._send(404, {"error": "Not found"})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._send(415, {"error": "Use application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 1024:
                raise ValueError
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict) or set(body) - {"run_id"} or body.get("run_id", self._run_id()) != self._run_id():
                raise ValueError
        except (ValueError, TypeError):
            self._send(400, {"error": "Expected an empty object or the fixed demo run_id"})
            return
        operation = local_replay if path == "/api/verify" else local_recall
        status, response = operation(self._run_id())
        self._send(status, response)

    def log_message(self, format, *args):
        # Standard request lines only; no body, report data or provider credentials.
        sys.stderr.write("[demo] " + format % args + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--run", type=checked_run_id, default=RUN_ID, help="Explicit retained run ID; no automatic success selection")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    write_data(demo_data(args.run))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DemoHandler)
    server.run_id = args.run
    print("Recorded run: " + args.run, flush=True)
    print("Recursive demo: http://127.0.0.1:" + str(args.port), flush=True)
    print("Recorded model evidence. Verify button runs local tests without API calls.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
