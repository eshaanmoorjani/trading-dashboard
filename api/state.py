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
                "vessels": {}, "dark_events": [], "reappear_events": [],
                "spoof_events": [], "transit_events": [], "arrival_events": [],
                "departure_events": [],
            }

            now = time.time()
            vessels = []
            zones = {"GULF": 0, "STRAIT": 0, "OCEAN": 0}
            active_12h = 0
            active_24h = 0

            for mmsi, v in state.get("vessels", {}).items():
                lat = v.get("last_lat")
                lon = v.get("last_lon")
                if lat is None or lon is None:
                    continue

                last_seen = v.get("last_seen", now)
                age = now - last_seen
                if age < 43200: active_12h += 1
                if age < 86400: active_24h += 1

                zone = v.get("zone", "GULF")
                if zone in zones:
                    zones[zone] += 1

                vessels.append({
                    "mmsi": mmsi,
                    "name": v.get("name") or "Unknown",
                    "type": v.get("type") or "",
                    "flag": v.get("flag") or "",
                    "flag_name": v.get("flag_name") or "",
                    "destination": v.get("destination") or "",
                    "lat": lat,
                    "lon": lon,
                    "zone": zone,
                    "last_seen": last_seen,
                    "dark_since": v.get("dark_since"),
                    "dark_minutes": (now - v["dark_since"]) / 60 if v.get("dark_since") else None,
                    "minutes_ago": round(age / 60, 1),
                })

            result = {
                "vessels": vessels,
                "zones": zones,
                "total_tracked": len(vessels),
                "active_12h": active_12h,
                "active_24h": active_24h,
                "dark_events": state.get("dark_events", [])[-50:],
                "reappear_events": state.get("reappear_events", [])[-50:],
                "spoof_events": state.get("spoof_events", [])[-50:],
                "transit_events": state.get("transit_events", [])[-50:],
                "arrival_events": state.get("arrival_events", [])[-50:],
                "departure_events": state.get("departure_events", [])[-50:],
                "in_box": sum(1 for v in vessels),
                "dark_count": sum(1 for v in vessels if v.get("dark_since")),
                "started_at": state.get("started_at", now),
                "calibrating": state.get("calibrating", False),
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
            body = json.dumps({"error": str(e), "vessels": [], "zones": {"GULF": 0, "STRAIT": 0, "OCEAN": 0}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass
