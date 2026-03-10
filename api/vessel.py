"""
Vercel serverless function — returns detail for a single vessel by MMSI.
GET /api/vessel?mmsi=123456789
"""

import os
import json
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

UPSTASH_URL = os.environ.get("KV_REST_API_URL", "")
UPSTASH_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")


def get_state():
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    url = f"{UPSTASH_URL}/get/hormuz_state"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    raw = data.get("result")
    return json.loads(raw) if raw else None


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        mmsi = params.get("mmsi", [None])[0]

        # Also support path-style: /api/vessel/123456789
        if not mmsi:
            parts = parsed.path.rstrip("/").split("/")
            if parts:
                mmsi = parts[-1]

        if not mmsi:
            self._respond(400, {"error": "mmsi required"})
            return

        try:
            state = get_state()
            if not state:
                self._respond(404, {"error": "no state"})
                return

            vessel = state.get("vessels", {}).get(mmsi)
            if not vessel:
                self._respond(404, {"error": f"vessel {mmsi} not found"})
                return

            result = {
                "mmsi": mmsi,
                "name": vessel.get("name") or "Unknown",
                "type": vessel.get("type") or "",
                "flag": vessel.get("flag") or "",
                "flag_name": vessel.get("flag_name") or "",
                "destination": vessel.get("destination") or "",
                "zone": vessel.get("zone") or "",
                "first_seen": vessel.get("first_seen"),
                "last_seen": vessel.get("last_seen"),
                "last_lat": vessel.get("last_lat"),
                "last_lon": vessel.get("last_lon"),
                "dark_since": vessel.get("dark_since"),
                "history": vessel.get("history", []),
            }
            self._respond(200, result)

        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass
