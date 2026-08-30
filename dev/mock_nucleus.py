#!/usr/bin/env python3
"""
Mock Domino/Nucleus API for local docker-compose testing of org-scoped
access control. See docs/org-scoped-access-plan.md.

Stdlib only — no dependencies to install, so it can run in a stock
`python:*-slim` container without needing frontend/requirements.lock.

Implements just enough of Nucleus's API surface for
frontend/app.py:resolve_access_scope() to exercise all three access
scopes (admin / org-scoped / denied) end-to-end, via real HTTP calls
exactly like it makes against a real Domino instance:

  - GET /v4/auth/principal
  - GET /api/organizations/v1/organizations

Identity is selected by a `dominoAuth=<key>` cookie, which this server
reads exactly like real Nucleus would read a real session cookie — the
frontend code under test never knows it's talking to a fake. Visit this
server directly (it's exposed on localhost:9000 by the compose override)
to switch identities without needing to hand-edit cookies:

  http://localhost:9000/              — list available identities
  http://localhost:9000/login/<key>   — set the cookie, redirect to the frontend
  http://localhost:9000/logout        — clear the cookie (simulates logged out)

Usage (see docker-compose.auth.yml):
  docker compose -f docker-compose.yml -f docker-compose.auth.yml up --build
"""

import json
import os
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "9000"))
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8899")

# Fake identities, keyed by the value of a `dominoAuth` cookie.
# Shape matches a real /v4/auth/principal response (trimmed to the fields
# resolve_access_scope() actually reads — see docs/org-scoped-access-plan.md
# Appendix B for the full field inventory of a real response).
PRINCIPALS = {
    "admin": {"isAdmin": True, "canonicalId": "admin-000", "canonicalName": "admin-user"},
    "org1": {"isAdmin": False, "canonicalId": "org-111", "canonicalName": "org-member"},
    "noorg": {"isAdmin": False, "canonicalId": "noorg-222", "canonicalName": "no-org-user"},
}

# Org memberships per identity, shaped like a real GET
# /api/organizations/v1/organizations response (see Appendix C). Only ever
# consulted for non-admin identities.
ORGS = {
    "org1": {
        "orgs": [
            {
                "id": "org-1",
                "name": "Test Org",
                "members": [{"userId": "org-111", "organizationRole": "Member"}],
            }
        ],
        "metadata": {"requestId": "mock", "notices": [], "offset": 0, "limit": 10},
    },
    "noorg": {"orgs": [], "metadata": {"requestId": "mock", "notices": [], "offset": 0, "limit": 10}},
}

_EMPTY_ORGS = {"orgs": [], "metadata": {"requestId": "mock", "notices": [], "offset": 0, "limit": 10}}


def _identity_key(headers) -> str:
    jar = cookies.SimpleCookie()
    jar.load(headers.get("Cookie", ""))
    morsel = jar.get("dominoAuth")
    return morsel.value if morsel else ""


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status, body: str):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect_with_cookie(self, cookie_header: str):
        self.send_response(302)
        self.send_header("Set-Cookie", cookie_header)
        self.send_header("Location", FRONTEND_URL + "/")
        self.end_headers()

    def do_GET(self):
        if self.path == "/v4/auth/principal":
            principal = PRINCIPALS.get(_identity_key(self.headers))
            if principal is None:
                return self._json(401, {"error": "not authenticated"})
            return self._json(200, principal)

        if self.path == "/api/organizations/v1/organizations":
            return self._json(200, ORGS.get(_identity_key(self.headers), _EMPTY_ORGS))

        if self.path.startswith("/login/"):
            key = self.path[len("/login/") :]
            if key not in PRINCIPALS:
                return self._json(404, {"error": f"unknown identity {key!r}", "known": list(PRINCIPALS)})
            return self._redirect_with_cookie(f"dominoAuth={key}; Path=/")

        if self.path == "/logout":
            return self._redirect_with_cookie("dominoAuth=; Path=/; Max-Age=0")

        if self.path == "/":
            rows = "".join(
                f"<li><a href='/login/{key}'>log in as {key}</a> — "
                f"{'admin' if p['isAdmin'] else 'non-admin'}, {ORGS.get(key, _EMPTY_ORGS)['orgs'] or 'no orgs'}</li>"
                for key, p in PRINCIPALS.items()
            )
            return self._html(
                200,
                f"<h1>Mock Nucleus</h1><p>Pick an identity, then use the app at {FRONTEND_URL}</p>"
                f"<ul>{rows}<li><a href='/logout'>log out (simulate unauthenticated)</a></li></ul>",
            )

        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # noqa: A002 - matches BaseHTTPRequestHandler's signature
        print(f"[mock-nucleus] {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Mock Nucleus listening on :{PORT} — visit http://localhost:{PORT}/ to switch identities")
    server.serve_forever()
