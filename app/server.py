"""Deproi demo app: a tiny web service with a health check (standard library only)."""

import http.server
import json
import os
import pathlib

VERSION = os.environ.get("APP_VERSION", "dev")
DATA = pathlib.Path(os.environ.get("APP_DATA", "/data"))
# Commit a file named BROKEN next to this one to ship a version that never becomes healthy.
BROKEN = pathlib.Path(__file__).with_name("BROKEN").exists()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.reply(500 if BROKEN else 200, {"healthy": not BROKEN})
        else:
            schema = DATA / "schema_version"
            self.reply(
                200,
                {
                    "version": VERSION,
                    "schema": schema.read_text().strip() if schema.exists() else None,
                    "greeting": os.environ.get("GREETING", "hello"),
                },
            )

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()  # noqa: S104 — inside a container
