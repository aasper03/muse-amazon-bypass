"""Cookies a response sets must reach the next request and the jar file on disk.

Akamai's interstitial solver depends on this: the clearance cookie from /_sec/verify has to be
sent on the retry. Uses a local HTTP server; Amazon is never contacted.
"""

import asyncio
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from amazon_cart import app as api


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        if self.path == "/set":
            self.send_header("Set-Cookie", "bm_verified=yes; Path=/; Max-Age=3600")
        self.end_headers()
        self.wfile.write((self.headers.get("Cookie") or "").encode())


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_response_cookies_persist_between_fetches(server):
    jar = os.path.join(tempfile.mkdtemp(), "cookies-test.txt")
    token = api._jar_var.set(jar)
    try:
        asyncio.run(api._curl(f"{server}/set"))
        _, seen = asyncio.run(api._curl(f"{server}/check"))
    finally:
        api._jar_var.reset(token)
    assert "bm_verified=yes" in seen
    assert "bm_verified" in open(jar).read()
