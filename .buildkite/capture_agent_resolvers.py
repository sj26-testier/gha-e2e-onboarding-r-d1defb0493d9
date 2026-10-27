#!/usr/bin/env python3
"""Isolated lab wrapper. Observe an unchanged importer; never replay requests."""

import base64
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import urllib.parse


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(directory, command):
    upstream = urllib.parse.urlsplit(os.environ["BUILDKITE_AGENT_ENDPOINT"])
    if upstream.scheme != "https" or not upstream.hostname or upstream.username or upstream.query or upstream.fragment:
        raise ValueError("capture requires the original HTTPS Agent API endpoint")
    job = os.environ["BUILDKITE_JOB_ID"]
    prefix = upstream.path.rstrip("/") + "/jobs/" + job + "/"
    config = json.loads(os.environ["BUILDKITE_PLUGIN_CONFIGURATION"])
    if config != {"version": "0.104.1", "workflow": ".github/workflows/capture.yml"}:
        raise ValueError("capture only accepts the literal lab workflow configuration")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "schema": "buildkite-gha/agent-inputs/v1",
        "job_id": job,
        "build_id": os.environ["BUILDKITE_BUILD_ID"],
        "commit": os.environ["BUILDKITE_COMMIT"],
        "plugin_configuration": config,
        "complete": False,
        "exchanges": [],
        "forwarded_requests": 0,
        "capture_errors": [],
    }
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            # No user-controlled destination, redirects or cross-job requests.
            if not self.path.startswith(prefix) or "?" in self.path or ".." in self.path:
                self.send_error(403)
                return
            if self.headers.get("Transfer-Encoding"):
                self.send_error(400)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {name: value for name, value in self.headers.items()
                       if name.lower() not in {"host", "connection", "content-length"}}
            connection = http.client.HTTPSConnection(upstream.hostname, upstream.port, timeout=60)
            with lock:
                record["forwarded_requests"] += 1
                sequence = record["forwarded_requests"]
            try:
                connection.request("POST", self.path, body=body, headers=headers)
                response = connection.getresponse()
                data = response.read()
                endpoint = self.path.removeprefix(prefix)
                if endpoint in {"github-actions/runners", "github-actions/variables", "github-actions/environments"}:
                    exchange = {
                        "sequence": sequence, "path": self.path, "status": response.status,
                        "request_id": response.getheader("X-Request-Id"),
                        "response_content_encoding": response.getheader("Content-Encoding"),
                        "request_sha256": digest(body), "response_sha256": digest(data),
                    }
                    if endpoint == "github-actions/runners":
                        exchange.update(request_base64=base64.b64encode(body).decode(),
                                        response_base64=base64.b64encode(data).decode())
                    else:
                        # This literal fixture needs neither; unreviewed values must not leak.
                        exchange["omitted"] = "Unexpected variables/environment bodies; not allowlisted"
                    with lock:
                        record["exchanges"].append(exchange)
                self.send_response_only(response.status, response.reason)
                for name, value in response.getheaders():
                    if name.lower() not in {"connection", "transfer-encoding", "content-length"}:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (OSError, http.client.HTTPException) as error:
                with lock:
                    # Error messages can include request data; retain only the class.
                    record["capture_errors"].append(type(error).__name__)
                self.close_connection = True
            finally:
                connection.close()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    environment = dict(os.environ)
    environment["BUILDKITE_AGENT_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}{upstream.path}"
    child = None
    status = 1
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        child = subprocess.Popen(command, env=environment, start_new_session=True)
        status = child.wait(timeout=300)
        record["importer_exit_status"] = status
        record["complete"] = True
    except subprocess.TimeoutExpired:
        status = 124
    except KeyboardInterrupt:
        status = 130
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        server.shutdown()
        server.server_close()
        thread.join()
        signal.signal(signal.SIGTERM, previous_handler)
        record["complete"] = record["complete"] and not record["capture_errors"]
        record["exchanges"].sort(key=lambda exchange: exchange["sequence"])
        path = directory / "agent-inputs.json"
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(record, output, indent=2)
    return status


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        sys.exit("usage: capture_agent_resolvers.py DIRECTORY -- COMMAND [ARG...]")
    sys.exit(run(Path(sys.argv[1]), sys.argv[3:]))
