#!/usr/bin/env python3
"""OAuth PKCE login for Shengcai MCP (public client).

Usage:
  cd /workspace/someone-listening/ondemand
  /workspace/.venv-ondemand/bin/python mcp_login.py

Prints the authorize URL, listens on http://127.0.0.1:8767/callback,
exchanges the code for tokens, stores them in secrets/mcp_tokens.json.
Does not print access/refresh tokens.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import string
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx

from mcp_client import (
    AUTHORIZE_URL,
    DEFAULT_REDIRECT,
    REGISTER_URL,
    RESOURCE,
    SCOPE,
    TOKEN_URL,
    TOKENS_PATH,
    load_tokens,
    save_tokens,
)

CLIENT_NAME = "someone-listening-ondemand"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8767


def _pkce() -> tuple[str, str]:
    verifier = "".join(secrets.choice(string.ascii_letters + string.digits + "-._~") for _ in range(64))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _ensure_client(data: dict[str, Any]) -> dict[str, Any]:
    """Dynamic client registration if no client_id yet."""
    if data.get("client_id"):
        return data
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [DEFAULT_REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }
    with httpx.Client(timeout=30.0) as client:
        r = client.post(REGISTER_URL, json=body, headers={"Accept": "application/json"})
    if r.status_code >= 400:
        raise SystemExit(f"client registration failed: HTTP {r.status_code} {r.text[:300]}")
    reg = r.json()
    client_id = reg.get("client_id")
    if not client_id:
        raise SystemExit("registration response missing client_id")
    data["client_id"] = client_id
    data["client_name"] = reg.get("client_name") or CLIENT_NAME
    data["redirect_uris"] = reg.get("redirect_uris") or [DEFAULT_REDIRECT]
    data["token_endpoint_auth_method"] = reg.get("token_endpoint_auth_method") or "none"
    data["scope"] = reg.get("scope") or SCOPE
    save_tokens(data)
    print(f"Registered public client_id={client_id[:12]}…")
    return data


def main() -> None:
    data = load_tokens()
    data = _ensure_client(data)
    client_id = str(data["client_id"])
    redirect_uri = DEFAULT_REDIRECT

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    result: dict[str, Any] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
            # keep quiet (no tokens in query should be logged anyway)
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.rstrip("/") != "/callback":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("error"):
                result["error"] = qs.get("error", ["unknown"])[0]
                result["error_description"] = (qs.get("error_description") or [""])[0]
            else:
                result["code"] = (qs.get("code") or [""])[0]
                result["state"] = (qs.get("state") or [""])[0]
            body = (
                b"<html><body><h3>Authorization received.</h3>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print()
    print("=== Shengcai MCP OAuth (PKCE S256, public client) ===")
    print(f"Listening for callback on {redirect_uri}")
    print()
    print("Open this URL in a browser (log in to 生财 if asked):")
    print()
    print(auth_url)
    print()
    try:
        opened = webbrowser.open(auth_url)
        if opened:
            print("(Tried to open your default browser.)")
    except Exception:
        pass
    print("Waiting for callback (timeout 5 minutes)…")

    if not done.wait(timeout=300):
        server.shutdown()
        raise SystemExit("Timed out waiting for OAuth callback")

    server.shutdown()

    if result.get("error"):
        raise SystemExit(f"OAuth error: {result.get('error')} {result.get('error_description')}")
    if result.get("state") != state:
        raise SystemExit("OAuth state mismatch — aborting")
    code = result.get("code")
    if not code:
        raise SystemExit("No authorization code in callback")

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": RESOURCE,
    }
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            TOKEN_URL,
            data=form,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code >= 400:
        raise SystemExit(f"token exchange failed: HTTP {r.status_code}")
    tok = r.json()
    access = tok.get("access_token")
    if not access:
        raise SystemExit("token response missing access_token")

    data["access_token"] = access
    if tok.get("refresh_token"):
        data["refresh_token"] = tok["refresh_token"]
    data["token_type"] = tok.get("token_type") or "Bearer"
    if tok.get("scope"):
        data["scope"] = tok["scope"]
    expires_in = tok.get("expires_in")
    if expires_in is not None:
        try:
            data["expires_at"] = time.time() + float(expires_in)
        except (TypeError, ValueError):
            pass
    save_tokens(data)

    print()
    print(f"OK — tokens saved to {TOKENS_PATH}")
    print("access_token: present" + ("; refresh_token: present" if data.get("refresh_token") else ""))
    print("You can now use the ondemand API (topic fetch / prefetch).")
    print("Restart not required if server already imported mcp_client.is_configured dynamically.")


if __name__ == "__main__":
    main()
