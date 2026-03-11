"""Minimal HTTP server — serves dashboard.html at / only. All other paths return 404."""

import http.server
import os

DASHBOARD = os.environ.get("DASHBOARD_PATH", "/data/dashboard.html")


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/dashboard.html"):
            try:
                with open(DASHBOARD, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                msg = b"Dashboard not yet generated. Check back after first extraction."
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
        else:
            msg = b"Not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def log_message(self, fmt, *args):
        pass  # suppress per-request access logs


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8080), DashboardHandler)
    print("[serve] Dashboard available at http://0.0.0.0:8080", flush=True)
    server.serve_forever()
