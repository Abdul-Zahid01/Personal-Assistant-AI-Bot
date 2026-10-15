import http.server
import socketserver
from pathlib import Path

PORT = 8080
DIRECTORY = Path(__file__).resolve().parent

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIRECTORY), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
    print(f"Serving chat UI on http://127.0.0.1:{PORT}")
    httpd.serve_forever()
