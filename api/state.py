"""
Vercel serverless function — reads vessel state from Upstash Redis.
GET /api/state → returns JSON vessel state for the Leaflet map.
"""

import os
import json
import time
import urllib.request

UPSTASH_URL = os.environ.get("KV_REST_API_URL", "")
UPSTASH_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")


def get_redis(key):
    url = f"{UPSTASH_URL}/get/{key}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    return data.get("result")


def handler(request):
    try:
        raw = get_redis("hormuz_state")
        if not raw:
            state = {"vessels": {}, "dark_events": [], "reappear_events": [], "spoof_events": []}
        else:
            state = json.loads(raw)

        now = time.time()
        vessels = []
        for mmsi, v in state.get("vessels", {}).items():
            if not v.get("last_lat") or not v.get("last_lon"):
                continue
            minutes_ago = (now - v.get("last_seen", now)) / 60
            vessels.append({
                "mmsi": mmsi,
                "name": v.get("name") or "Unknown",
                "type": v.get("type") or "",
                "lat": v["last_lat"],
                "lon": v["last_lon"],
                "in_box": v.get("in_box", False),
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
            "dark_count": sum(1 for v in vessels if v["dark_since"]),
            "ts": now,
        }

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(result),
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }
