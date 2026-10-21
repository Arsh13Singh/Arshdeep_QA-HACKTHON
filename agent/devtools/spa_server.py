"""DEV ONLY: static server with SPA fallback (unknown paths -> index.html), like `vite preview`."""
import http.server
import sys
from functools import partial
from pathlib import Path

root = Path(sys.argv[1]).resolve()
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5173


class Handler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        if self.path.endswith(".js") and (root / self.path.lstrip("/")).is_file():
            self.extensions_map[".js"] = "text/javascript"
        if not (root / self.path.split("?")[0].lstrip("/")).is_file():
            self.path = "/index.html"
        return super().send_head()

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(root))).serve_forever()
