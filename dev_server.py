"""Local preview of the live-demo UI: python dev_server.py [port]  ->  http://localhost:8000"""
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler

from api.run import handler


class H(SimpleHTTPRequestHandler):
    do_POST, _send = handler.do_POST, handler._send

    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(os.path.dirname(__file__), "public"), **k)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"http://localhost:{port}")
    HTTPServer(("", port), H).serve_forever()
