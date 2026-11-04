from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _support import ROOT  # noqa: F401

from scripts.growth.rpc import RPCError, call


class RPCTests(unittest.TestCase):
    def serve(self, response: dict):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                self.assert_request = request
                body = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_direct_worldrpc_returns_result_without_account_files(self):
        port = self.serve({"jsonrpc": "2.0", "id": 1, "result": {
            "guid": 42, "name": "Growth1", "ready": True}})
        result = call(port, "sense.get_self", timeout=2)
        self.assertEqual(result["guid"], 42)
        self.assertTrue(result["ready"])

    def test_direct_worldrpc_surfaces_server_errors(self):
        port = self.serve({"jsonrpc": "2.0", "id": 1,
                           "error": {"code": -1, "message": "unavailable"}})
        with self.assertRaises(RPCError):
            call(port, "sense.get_self", timeout=2)


if __name__ == "__main__":
    unittest.main()
