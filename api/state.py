"""
Vercel serverless function — reads vessel state from Upstash Redis.
GET /api/state → returns JSON vessel state for the Leaflet map.
"""

import os
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

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
        try:
            state = get_state() or {
                "vessels": {}, "dark_events": [], "reappear_events": [], "spoof_events": []
            }

            now = time.time()
            vessels = []
            for mmsi, v in state.get("vessels", {}).items():
                lat = v.get("last_lat")
                lon = v.get("last_lon")
                if lat is None or lon is None:
                    continue
                minutes_ago = (now - v.get("last_seen", now)) / 60
                zone = v.get("zone", "")
                vessels.append({
                    "mmsi": mmsi,
                    "name": v.get("name") or "Unknown",
                    "type": v.get("type") or "",
                    "flag": v.get("flag_name") or "",
                    "lat": lat,
                    "lon": lon,
                    "zone": zone,
                    "in_box": bool(zone),  # any vessel with a zone is in our coverage area
                    "dark_since": v.get("dark_since"),
                    "dark_minutes": (now - v["dark_since"]) / 60 if v.get("dark_since") else None,
                    "minutes_ago": round(minutes_ago, 1),
                })

            result = {
                "vessels": vessels,
                "dark_events": state.get("dark_events", [])[-50:],
                "reappear_events": state.get("reappear_events", [])[-50:],
                "spoof_events": state.get("spoof_events", [])[-50:],
                "total_tracked": len(state.get("vessels", {})),
                "in_box": sum(1 for v in vessels if v["in_box"]),
                "dark_count": sum(1 for v in vessels if v.get("dark_since")),
                "ts": now,
            }

            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress default logging
