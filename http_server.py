# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0.0", "uvicorn>=0.30"]
# ///
"""Serve the iMessage MCP tools over streamable HTTP behind a bearer token.

Binds to loopback only. Put a tunnel in front of it for remote access; the
tunnel terminates TLS, so nothing here listens on a public interface.

Environment:
  IMESSAGE_TOKEN        required, the bearer token clients must present
  IMESSAGE_PUBLIC_HOST  public hostname the tunnel serves, for Host validation
  IMESSAGE_PORT         loopback port, default 8765
  IMESSAGE_ALLOW_SEND   set to 0 to expose the read-only tools only
"""

import os
import secrets
import subprocess
import sys

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from server import server

def keychain_token() -> str:
    """Read the bearer token from Keychain so no shell wrapper is needed.

    launchd must exec the interpreter directly: Full Disk Access is granted per
    executable and does not inherit, and granting it to /bin/zsh would hand
    every script on the machine access to chat.db.
    """
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", "imessage-mcp-token", "-w"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


TOKEN = os.environ.get("IMESSAGE_TOKEN") or keychain_token()
PUBLIC_HOST = os.environ.get("IMESSAGE_PUBLIC_HOST", "")
PORT = int(os.environ.get("IMESSAGE_PORT", "8765"))

if not TOKEN:
    sys.exit("IMESSAGE_TOKEN is required; refusing to serve message history unauthenticated")
if len(TOKEN) < 32:
    sys.exit("IMESSAGE_TOKEN must be at least 32 characters")


def presented_token(header: str) -> str:
    """The credential out of an Authorization header, or "".

    RFC 7235 makes the scheme case-insensitive and allows extra whitespace
    between it and the credential, so matching the whole header verbatim
    rejects "bearer <token>" from clients that lowercase their headers. The
    credential itself is still compared exactly.
    """
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return credential.strip()


def require_token(app):
    """Reject any request without the bearer token, before MCP sees it."""

    async def middleware(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        presented = presented_token(
            headers.get(b"authorization", b"").decode(errors="replace")
        )
        if not secrets.compare_digest(presented, TOKEN):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await app(scope, receive, send)

    return middleware


# Host/Origin validation guards against DNS rebinding; the tunnel's hostname
# has to be allowed explicitly or every proxied request is refused.
allowed = ["127.0.0.1", f"127.0.0.1:{PORT}", "localhost", f"localhost:{PORT}"]
origins = []
if PUBLIC_HOST:
    allowed.append(PUBLIC_HOST)
    origins.append(f"https://{PUBLIC_HOST}")

app = server.streamable_http_app(
    host="127.0.0.1",
    transport_security=TransportSecuritySettings(
        allowed_hosts=allowed, allowed_origins=origins
    ),
)

if __name__ == "__main__":
    send = "enabled" if os.environ.get("IMESSAGE_ALLOW_SEND", "1") != "0" else "disabled"
    print(f"serving /mcp on 127.0.0.1:{PORT}, send {send}", file=sys.stderr)
    uvicorn.run(require_token(app), host="127.0.0.1", port=PORT, log_level="warning")
