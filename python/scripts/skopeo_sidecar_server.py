#!/usr/bin/env python3
"""
Minimal HTTP server that runs Skopeo commands for the docker-registry-cleaner sidecar.

Used when the main app runs in a container without Skopeo; this container runs in the same
pod and exposes POST /run with JSON body: { "subcommand", "args", "creds" (optional) }.
Returns JSON: { "stdout", "stderr", "returncode" }.

Uses only Python stdlib. Listens on port from env SKOPEO_SIDECAR_PORT (default 8080).
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple


def run_skopeo(subcommand: str, args: list, creds: Optional[str]) -> Tuple[str, str, int]:
    """Run skopeo with the given subcommand and args. Returns (stdout, stderr, returncode)."""
    cmd = ["skopeo", subcommand, "--tls-verify=false"]
    if creds:
        cmd.extend(["--creds", creds])
    cmd.extend(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return (result.stdout or "", result.stderr or "", result.returncode)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/run":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError) as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"returncode": -1, "stdout": "", "stderr": str(e)}
                ).encode("utf-8")
            )
            return
        subcommand = data.get("subcommand")
        args = data.get("args")
        creds = data.get("creds")
        if not subcommand or not isinstance(args, list):
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"returncode": -1, "stdout": "", "stderr": "subcommand and args required"}
                ).encode("utf-8")
            )
            return
        try:
            stdout, stderr, returncode = run_skopeo(subcommand, args, creds)
        except subprocess.TimeoutExpired:
            stdout, stderr, returncode = "", "skopeo timed out", -1
        except FileNotFoundError:
            stdout, stderr, returncode = "", "skopeo not found", -1
        except Exception as e:
            stdout, stderr, returncode = "", str(e), -1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps({"stdout": stdout, "stderr": stderr, "returncode": returncode}).encode("utf-8")
        )

    def log_message(self, format, *args):
        # Quiet by default; override to reduce noise
        sys.stderr.write(f"[skopeo-sidecar] {format % args}\n")


def main():
    port = int(os.environ.get("SKOPEO_SIDECAR_PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Skopeo sidecar listening on 0.0.0.0:{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
