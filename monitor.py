#!/usr/bin/env python3
"""
Hormuz Strait AIS Monitor
--------------------------
Monitors the full Persian Gulf (Kuwait → Strait of Hormuz → Gulf of Oman).

Zone model:
  GULF    = west of the strait (the Persian Gulf proper)
  STRAIT  = the Hormuz chokepoint
  OCEAN   = east of the strait (Gulf of Oman / Indian Ocean)

Key events detected:
  NEW ARRIVAL  — vessel appears in scan area for the first time
  DARK TRANSIT — vessel went dark in one zone, reappeared in another
  GPS SPOOF    — impossible position jump (>300km between polls)
  SIGNAL LOST  — vessel stopped transmitting (logged, no alert)

Data source: MyShipTracking free map endpoint (area scan every cycle).
"""

import json
import os
import sys
import time
import math
import socket
import argparse
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Force IPv4 — MyShipTracking rate-limits IPv6 more aggressively
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only

# ── Config ─────────────────────────────────────────────────────────────────────

# Full scan area (everything we monitor)
SCAN_BOX = {
    "lat_min": 24.0, "lat_max": 30.5,
    "lon_min": 47.0, "lon_max": 60.0,
}

# Strait of Hormuz chokepoint zone
STRAIT_ZONE = {
    "lat_min": 25.5, "lat_max": 27.0,
    "lon_min": 55.5, "lon_max": 57.0,
}

# Zone classification thresholds
# Gulf = west of this longitude, Ocean = east of this longitude
STRAIT_WEST_LON = 55.5   # west boundary of strait
STRAIT_EAST_LON = 57.0   # east boundary of strait

# Area scan tiles for vessel discovery
SCAN_TILES = [
    (24.5, 27.0, 47.5, 53.5, 8),   # Western Persian Gulf
    (27.0, 30.0, 47.5, 53.5, 8),   # Northern Gulf (Kuwait)
    (24.5, 27.0, 53.5, 59.5, 8),   # Eastern Gulf + Hormuz
    (27.0, 30.0, 53.5, 59.5, 8),   # NE Gulf (Iran coast)
    (25.5, 27.0, 55.5, 57.5, 10),  # Strait of Hormuz (high zoom)
    (24.0, 26.0, 57.0, 60.0, 9),   # Gulf of Oman
]

AREA_SCAN_URL = "https://www.myshiptracking.com/requests/vesselsonmaptempTTT.php"

DARK_THRESHOLD_MINUTES = 20
SPOOF_THRESHOLD_KM = 300
POLL_INTERVAL_MINUTES = 5
STALE_HOURS = 48  # keep vessels longer for transit tracking
MAX_HISTORY = 500  # max position records per vessel

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")

SCRAPE_URL = "https://www.myshiptracking.com/requests/vesselonmap.php"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

STATE_FILE = Path(__file__).parent / "hormuz_vessels.json"
LOG_FILE = Path(__file__).parent / "hormuz_monitor.log"

# ── MMSI → Flag State (Maritime Identification Digits) ────────────────────────

MID_COUNTRY = {
    "201": "AL", "202": "AD", "203": "AT", "204": "PT", "205": "BE", "206": "BY",
    "207": "BG", "208": "VA", "209": "CY", "210": "CY", "211": "DE", "212": "CY",
    "213": "GE", "214": "MD", "215": "MT", "216": "AM", "218": "DE", "219": "DK",
    "220": "DK", "224": "ES", "225": "ES", "226": "FR", "227": "FR", "228": "FR",
    "229": "MT", "230": "FI", "231": "FO", "232": "GB", "233": "GB", "234": "GB",
    "235": "GB", "236": "GI", "237": "GR", "238": "HR", "239": "GR", "240": "GR",
    "241": "GR", "242": "MA", "243": "HU", "244": "NL", "245": "NL", "246": "NL",
    "247": "IT", "248": "MT", "249": "MT", "250": "IE", "251": "IS", "252": "LI",
    "253": "LU", "254": "MC", "255": "PT", "256": "MT", "257": "NO", "258": "NO",
    "259": "NO", "261": "PL", "263": "PT", "264": "RO", "265": "SE", "266": "SE",
    "267": "SK", "268": "SM", "269": "CH", "270": "CZ", "271": "TR", "272": "UA",
    "273": "RU", "274": "MK", "275": "LV", "276": "EE", "277": "LT", "278": "SI",
    "279": "ME", "301": "AI", "303": "US", "304": "AG", "305": "AG", "306": "CW",
    "307": "AW", "308": "BS", "309": "BS", "310": "BM", "311": "BS", "312": "BZ",
    "314": "BB", "316": "CA", "319": "KY", "321": "CR", "323": "CU", "325": "DM",
    "327": "DO", "329": "GP", "330": "GD", "331": "GL", "332": "GT", "334": "HN",
    "336": "HT", "338": "US", "339": "JM", "341": "KN", "343": "LC", "345": "MX",
    "347": "MQ", "348": "MS", "350": "NI", "351": "PA", "352": "PA", "353": "PA",
    "354": "PA", "355": "PA", "356": "PA", "357": "PA", "370": "PA", "371": "PA",
    "372": "PA", "373": "PA", "374": "PA", "375": "VC", "376": "VC", "377": "VC",
    "378": "VG", "379": "VI", "401": "AF", "403": "SA", "405": "BD", "408": "BH",
    "410": "BT", "412": "CN", "413": "CN", "414": "CN", "416": "TW", "417": "LK",
    "419": "IN", "422": "IR", "423": "AZ", "425": "IQ", "428": "IL", "431": "JP",
    "432": "JP", "434": "TM", "436": "KZ", "437": "UZ", "438": "JO", "440": "KR",
    "441": "KR", "443": "PS", "445": "KP", "447": "KW", "450": "LB", "451": "KG",
    "453": "MO", "455": "MV", "457": "MN", "459": "NP", "461": "OM", "463": "PK",
    "466": "QA", "468": "SY", "470": "AE", "471": "AE", "472": "TJ", "473": "YE",
    "475": "YE", "477": "HK", "478": "BA", "501": "AQ", "503": "AU", "506": "MM",
    "508": "BN", "510": "FM", "511": "PW", "512": "NZ", "514": "KH", "515": "KH",
    "516": "CX", "518": "CK", "520": "FJ", "523": "CC", "525": "ID", "529": "KI",
    "531": "LA", "533": "MY", "536": "MP", "538": "MH", "540": "NC", "542": "NU",
    "544": "NR", "546": "PF", "548": "PH", "553": "PG", "555": "PN", "557": "SB",
    "559": "AS", "561": "WS", "563": "SG", "564": "SG", "565": "SG", "566": "SG",
    "567": "TH", "570": "TO", "572": "TV", "574": "VN", "576": "VU", "577": "VU",
    "578": "WF", "601": "ZA", "603": "AO", "605": "DZ", "607": "TF", "608": "IO",
    "609": "BI", "610": "BJ", "611": "BW", "612": "CF", "613": "CM", "615": "CG",
    "616": "KM", "617": "CV", "618": "AQ", "619": "CI", "620": "KM", "621": "DJ",
    "622": "EG", "624": "ET", "625": "ER", "626": "GA", "627": "GH", "629": "GM",
    "630": "GW", "631": "GQ", "632": "GN", "633": "BF", "634": "KE", "635": "AQ",
    "636": "LR", "637": "LR", "638": "SS", "642": "LY", "644": "LS", "645": "MU",
    "647": "MG", "649": "ML", "650": "MZ", "654": "MR", "655": "MW", "656": "NE",
    "657": "NG", "659": "NA", "660": "RE", "661": "RW", "662": "ST", "663": "SN",
    "664": "SC", "665": "SH", "666": "SO", "667": "SL", "668": "SZ", "669": "SD",
    "670": "TG", "671": "TN", "672": "TZ", "674": "UG", "675": "CD", "676": "TZ",
    "677": "TZ", "678": "ZM", "679": "ZW", "701": "AR", "710": "BR", "720": "BO",
    "725": "CL", "730": "CO", "735": "EC", "740": "FK", "745": "GF", "750": "GY",
    "755": "PY", "760": "PE", "765": "SR", "770": "UY", "775": "VE",
}

# Country code → emoji flag + short name for common maritime nations
COUNTRY_INFO = {
    "PA": ("🇵🇦", "Panama"), "LR": ("🇱🇷", "Liberia"), "MH": ("🇲🇭", "Marshall Is."),
    "HK": ("🇭🇰", "Hong Kong"), "SG": ("🇸🇬", "Singapore"), "MT": ("🇲🇹", "Malta"),
    "BS": ("🇧🇸", "Bahamas"), "CY": ("🇨🇾", "Cyprus"), "GB": ("🇬🇧", "UK"),
    "GR": ("🇬🇷", "Greece"), "CN": ("🇨🇳", "China"), "JP": ("🇯🇵", "Japan"),
    "KR": ("🇰🇷", "S. Korea"), "IN": ("🇮🇳", "India"), "IR": ("🇮🇷", "Iran"),
    "SA": ("🇸🇦", "Saudi Arabia"), "AE": ("🇦🇪", "UAE"), "BH": ("🇧🇭", "Bahrain"),
    "QA": ("🇶🇦", "Qatar"), "KW": ("🇰🇼", "Kuwait"), "OM": ("🇴🇲", "Oman"),
    "IQ": ("🇮🇶", "Iraq"), "EG": ("🇪🇬", "Egypt"), "TR": ("🇹🇷", "Turkey"),
    "US": ("🇺🇸", "USA"), "NO": ("🇳🇴", "Norway"), "DK": ("🇩🇰", "Denmark"),
    "DE": ("🇩🇪", "Germany"), "NL": ("🇳🇱", "Netherlands"), "FR": ("🇫🇷", "France"),
    "IT": ("🇮🇹", "Italy"), "ES": ("🇪🇸", "Spain"), "SE": ("🇸🇪", "Sweden"),
    "RU": ("🇷🇺", "Russia"), "TW": ("🇹🇼", "Taiwan"), "PH": ("🇵🇭", "Philippines"),
    "ID": ("🇮🇩", "Indonesia"), "MY": ("🇲🇾", "Malaysia"), "TH": ("🇹🇭", "Thailand"),
    "VN": ("🇻🇳", "Vietnam"), "BD": ("🇧🇩", "Bangladesh"), "PK": ("🇵🇰", "Pakistan"),
    "LK": ("🇱🇰", "Sri Lanka"), "AU": ("🇦🇺", "Australia"), "NZ": ("🇳🇿", "New Zealand"),
    "BR": ("🇧🇷", "Brazil"), "CL": ("🇨🇱", "Chile"), "CO": ("🇨🇴", "Colombia"),
    "MX": ("🇲🇽", "Mexico"), "CA": ("🇨🇦", "Canada"), "AG": ("🇦🇬", "Antigua"),
    "VC": ("🇻🇨", "St. Vincent"), "CK": ("🇨🇰", "Cook Is."), "KH": ("🇰🇭", "Cambodia"),
    "TZ": ("🇹🇿", "Tanzania"), "MM": ("🇲🇲", "Myanmar"), "FI": ("🇫🇮", "Finland"),
    "IL": ("🇮🇱", "Israel"), "JO": ("🇯🇴", "Jordan"), "SY": ("🇸🇾", "Syria"),
    "YE": ("🇾🇪", "Yemen"), "NG": ("🇳🇬", "Nigeria"), "ZA": ("🇿🇦", "South Africa"),
    "KE": ("🇰🇪", "Kenya"), "GH": ("🇬🇭", "Ghana"), "DZ": ("🇩🇿", "Algeria"),
    "LY": ("🇱🇾", "Libya"), "TN": ("🇹🇳", "Tunisia"), "MA": ("🇲🇦", "Morocco"),
    "PT": ("🇵🇹", "Portugal"), "IE": ("🇮🇪", "Ireland"), "PL": ("🇵🇱", "Poland"),
    "HR": ("🇭🇷", "Croatia"), "RO": ("🇷🇴", "Romania"), "BG": ("🇧🇬", "Bulgaria"),
    "UA": ("🇺🇦", "Ukraine"), "GE": ("🇬🇪", "Georgia"), "AZ": ("🇦🇿", "Azerbaijan"),
    "FJ": ("🇫🇯", "Fiji"), "VU": ("🇻🇺", "Vanuatu"), "TO": ("🇹🇴", "Tonga"),
    "PE": ("🇵🇪", "Peru"), "AR": ("🇦🇷", "Argentina"), "EC": ("🇪🇨", "Ecuador"),
    "TV": ("🇹🇻", "Tuvalu"), "KN": ("🇰🇳", "St. Kitts"), "KM": ("🇰🇲", "Comoros"),
    "NR": ("🇳🇷", "Nauru"), "PW": ("🇵🇼", "Palau"), "WS": ("🇼🇸", "Samoa"),
    "FM": ("🇫🇲", "Micronesia"), "KI": ("🇰🇮", "Kiribati"), "SB": ("🇸🇧", "Solomon Is."),
    "PG": ("🇵🇬", "Papua NG"), "NC": ("🇳🇨", "New Caledonia"), "PF": ("🇵🇫", "Fr. Polynesia"),
    "BZ": ("🇧🇿", "Belize"), "HN": ("🇭🇳", "Honduras"), "CR": ("🇨🇷", "Costa Rica"),
    "GT": ("🇬🇹", "Guatemala"), "NI": ("🇳🇮", "Nicaragua"), "CU": ("🇨🇺", "Cuba"),
    "DO": ("🇩🇴", "Dominican Rep."), "TT": ("🇹🇹", "Trinidad"), "JM": ("🇯🇲", "Jamaica"),
    "BB": ("🇧🇧", "Barbados"), "DM": ("🇩🇲", "Dominica"), "GD": ("🇬🇩", "Grenada"),
    "SR": ("🇸🇷", "Suriname"), "GY": ("🇬🇾", "Guyana"), "UY": ("🇺🇾", "Uruguay"),
    "BO": ("🇧🇴", "Bolivia"), "PY": ("🇵🇾", "Paraguay"), "VE": ("🇻🇪", "Venezuela"),
    "GI": ("🇬🇮", "Gibraltar"), "IS": ("🇮🇸", "Iceland"), "FO": ("🇫🇴", "Faroe Is."),
    "AD": ("🇦🇩", "Andorra"), "AL": ("🇦🇱", "Albania"), "AT": ("🇦🇹", "Austria"),
    "SM": ("🇸🇲", "San Marino"), "LI": ("🇱🇮", "Liechtenstein"), "LU": ("🇱🇺", "Luxembourg"),
    "MC": ("🇲🇨", "Monaco"), "ME": ("🇲🇪", "Montenegro"), "BA": ("🇧🇦", "Bosnia"),
    "SK": ("🇸🇰", "Slovakia"), "CZ": ("🇨🇿", "Czechia"), "MK": ("🇲🇰", "N. Macedonia"),
    "SI": ("🇸🇮", "Slovenia"), "HU": ("🇭🇺", "Hungary"), "LV": ("🇱🇻", "Latvia"),
    "EE": ("🇪🇪", "Estonia"), "LT": ("🇱🇹", "Lithuania"), "BY": ("🇧🇾", "Belarus"),
    "MD": ("🇲🇩", "Moldova"), "AM": ("🇦🇲", "Armenia"), "TM": ("🇹🇲", "Turkmenistan"),
    "KZ": ("🇰🇿", "Kazakhstan"), "UZ": ("🇺🇿", "Uzbekistan"), "KG": ("🇰🇬", "Kyrgyzstan"),
    "TJ": ("🇹🇯", "Tajikistan"), "MN": ("🇲🇳", "Mongolia"), "NP": ("🇳🇵", "Nepal"),
    "BT": ("🇧🇹", "Bhutan"), "AF": ("🇦🇫", "Afghanistan"), "SS": ("🇸🇸", "S. Sudan"),
    "DJ": ("🇩🇯", "Djibouti"), "ER": ("🇪🇷", "Eritrea"), "ET": ("🇪🇹", "Ethiopia"),
    "SO": ("🇸🇴", "Somalia"), "SD": ("🇸🇩", "Sudan"), "PS": ("🇵🇸", "Palestine"),
    "KP": ("🇰🇵", "N. Korea"), "MO": ("🇲🇴", "Macau"), "BN": ("🇧🇳", "Brunei"),
    "MV": ("🇲🇻", "Maldives"), "LA": ("🇱🇦", "Laos"),
}

def mmsi_to_flag(mmsi):
    """Derive flag state (country code) from MMSI's Maritime ID Digits."""
    if not mmsi or len(mmsi) < 3:
        return None
    # Special MMSIs: 99MIDXXXX = AIS aids/nav, 97MIDXXXX = SAR
    if len(mmsi) == 9 and mmsi[:2] in ('99', '97', '98'):
        mid = mmsi[2:5]
        return MID_COUNTRY.get(mid)
    mid = mmsi[:3]
    return MID_COUNTRY.get(mid)

def mmsi_to_country(mmsi):
    """Return (country_code, country_name) from MMSI."""
    cc = mmsi_to_flag(mmsi)
    if cc and cc in COUNTRY_INFO:
        return cc, COUNTRY_INFO[cc][1]
    if cc:
        return cc, cc
    return None, None

# ── Helpers ────────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def get_zone(lat, lon):
    """Classify position into GULF, STRAIT, or OCEAN."""
    if STRAIT_ZONE["lat_min"] <= lat <= STRAIT_ZONE["lat_max"] and \
       STRAIT_ZONE["lon_min"] <= lon <= STRAIT_ZONE["lon_max"]:
        return "STRAIT"
    if lon < STRAIT_WEST_LON:
        return "GULF"
    if lon >= STRAIT_EAST_LON:
        return "OCEAN"
    # In between but not in the strait box (north/south of it)
    if lat > STRAIT_ZONE["lat_max"]:
        return "GULF"  # Iran coast, still gulf side
    return "OCEAN"  # UAE east coast

def in_scan_area(lat, lon):
    return (SCAN_BOX["lat_min"] <= lat <= SCAN_BOX["lat_max"] and
            SCAN_BOX["lon_min"] <= lon <= SCAN_BOX["lon_max"])

def crossed_strait(zone_from, zone_to):
    """Did the vessel cross from one side to the other?"""
    if zone_from == "GULF" and zone_to == "OCEAN":
        return "GULF→OCEAN"
    if zone_from == "OCEAN" and zone_to == "GULF":
        return "OCEAN→GULF"
    if zone_from == "GULF" and zone_to == "STRAIT":
        return "GULF→STRAIT"
    if zone_from == "STRAIT" and zone_to == "OCEAN":
        return "STRAIT→OCEAN"
    if zone_from == "OCEAN" and zone_to == "STRAIT":
        return "OCEAN→STRAIT"
    if zone_from == "STRAIT" and zone_to == "GULF":
        return "STRAIT→GULF"
    return None

def now_ts():
    return time.time()

def now_pt():
    pt = timezone(timedelta(hours=-7))
    return datetime.now(pt).strftime("%b %d %I:%M %p PT")

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "vessels": {},
        "dark_events": [],      # signal lost
        "transit_events": [],   # dark transits (crossed strait while dark)
        "arrival_events": [],   # new vessels entering scan area
        "departure_events": [], # vessels that left / went stale
        "spoof_events": [],     # GPS spoofing
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Telegram failed: {e}")

def send_slack(text):
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        return
    import json as _json
    payload = _json.dumps({"channel": SLACK_CHANNEL, "text": text}).encode()
    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            }
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Slack failed: {e}")

def alert(text):
    """Send to both Telegram and Slack."""
    send_telegram(text)
    send_slack(text)

# ── Scraping ───────────────────────────────────────────────────────────────────

def scan_area_tile(minlat, maxlat, minlon, maxlon, zoom=8):
    """Scan an area for all vessels using the free map endpoint."""
    params = urllib.parse.urlencode({
        "type": "json", "minlat": minlat, "maxlat": maxlat,
        "minlon": minlon, "maxlon": maxlon, "zoom": zoom,
    })
    url = f"{AREA_SCAN_URL}?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": "https://www.myshiptracking.com/",
    })
    resp = urllib.request.urlopen(req, timeout=30)
    text = resp.read().decode().strip()
    vessels = []
    for line in text.split("\n")[1:]:  # skip timestamp line
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        try:
            vtype = int(parts[0]) if parts[0].isdigit() else 0
            mmsi = parts[2]
            name = parts[3] if parts[3] != mmsi else None
            lat = float(parts[4])
            lon = float(parts[5])
            speed = float(parts[6]) if len(parts) > 6 else 0
            heading = float(parts[7]) if len(parts) > 7 else 0
            # Index 13 = last AIS ping timestamp from MyShipTracking
            ais_ts = int(parts[13]) if len(parts) > 13 and parts[13].isdigit() else 0
            destination = parts[14].strip() if len(parts) > 14 else ""
            vessels.append({
                "mmsi": mmsi, "name": name, "vtype": vtype,
                "lat": lat, "lon": lon, "speed": speed, "heading": heading,
                "ais_ts": ais_ts, "destination": destination,
            })
        except (ValueError, IndexError):
            continue
    return vessels

def scan_full_area():
    """Scan all tiles to discover vessels across the full Persian Gulf."""
    all_vessels = {}
    for minlat, maxlat, minlon, maxlon, zoom in SCAN_TILES:
        try:
            found = scan_area_tile(minlat, maxlat, minlon, maxlon, zoom)
            for v in found:
                all_vessels[v["mmsi"]] = v
            time.sleep(1)
        except Exception as e:
            log(f"Tile scan failed ({minlat}-{maxlat}, {minlon}-{maxlon}): {e}")
    return all_vessels

# ── Core logic ─────────────────────────────────────────────────────────────────

def process_scan(state, discovered, first_cycle=False):
    """Process area scan results: detect arrivals, transits, spoofs, dark vessels."""
    vessels = state["vessels"]
    now = now_ts()
    seen_mmsis = set()

    for mmsi, data in discovered.items():
        lat, lon = data["lat"], data["lon"]
        if lat == 0.0 and lon == 0.0:
            continue

        seen_mmsis.add(mmsi)
        zone = get_zone(lat, lon)
        name = data.get("name")
        speed = data.get("speed", 0)
        vtype = str(data.get("vtype", ""))
        destination = data.get("destination", "")
        # Use AIS timestamp from the website if available, otherwise use now
        ais_ts = data.get("ais_ts", 0)
        vessel_ts = ais_ts if ais_ts > 1700000000 else now  # sanity check

        # ── NEW ARRIVAL: vessel we've never seen before ──
        if mmsi not in vessels:
            flag_cc, flag_name = mmsi_to_country(mmsi)
            vessels[mmsi] = {
                "mmsi": mmsi, "name": name, "type": vtype,
                "flag": flag_cc, "flag_name": flag_name,
                "destination": destination,
                "first_seen": vessel_ts, "last_seen": vessel_ts,
                "last_lat": lat, "last_lon": lon,
                "zone": zone, "last_zone": zone,
                "dark_since": None, "dark_alerted": False,
                "spoof_alerted": False,
                "history": [{"ts": vessel_ts, "lat": lat, "lon": lon, "zone": zone, "speed": speed}],
            }
            # Only log arrivals after 1h calibration period (scan noise settles)
            calibration_done = (now - state.get("started_at", now)) >= 3600
            if not first_cycle and calibration_done:
                flag_str = f" [{flag_name}]" if flag_name else ""
                log(f"ENTERED: {name or mmsi}{flag_str} — {zone} at {lat:.3f}N {lon:.3f}E (type={vtype}, spd={speed:.1f})")
                state["arrival_events"].append({
                    "mmsi": mmsi, "name": name, "type": vtype,
                    "flag": flag_cc, "flag_name": flag_name,
                    "lat": lat, "lon": lon, "zone": zone,
                    "speed": speed, "ts": vessel_ts,
                })
            continue

        vessel = vessels[mmsi]
        if name:
            vessel["name"] = name
        prev_zone = vessel.get("zone", zone)
        prev_lat = vessel.get("last_lat")
        prev_lon = vessel.get("last_lon")

        # ── GPS SPOOF check ──
        if prev_lat and prev_lon:
            dist_km = haversine_km(prev_lat, prev_lon, lat, lon)
            time_delta_hr = max((now - vessel.get("last_seen", now)) / 3600, 0.001)
            if dist_km > SPOOF_THRESHOLD_KM and not vessel.get("spoof_alerted"):
                implied_speed_kn = (dist_km / 1.852) / time_delta_hr
                vname = vessel.get("name") or mmsi
                msg = (
                    f"GPS SPOOF DETECTED — {now_pt()}\n\n"
                    f"Vessel: {vname} (MMSI: {mmsi})\n"
                    f"Jumped {dist_km:.0f} km in {time_delta_hr*60:.0f} min\n"
                    f"Implied speed: {implied_speed_kn:.0f} kn (impossible)\n"
                    f"From: {prev_lat:.3f}N {prev_lon:.3f}E ({prev_zone})\n"
                    f"To: {lat:.3f}N {lon:.3f}E ({zone})"
                )
                log(f"SPOOF: {vname} ({mmsi}) jumped {dist_km:.0f}km")
                alert(msg)
                state["spoof_events"].append({
                    "mmsi": mmsi, "name": vname, "dist_km": dist_km, "ts": now,
                })
                vessel["spoof_alerted"] = True

        # ── DARK TRANSIT: was dark, now reappeared in a different zone ──
        if vessel.get("dark_since"):
            dark_duration_min = (now - vessel["dark_since"]) / 60
            dark_zone = vessel.get("zone", "?")
            crossing = crossed_strait(dark_zone, zone)
            vname = vessel.get("name") or mmsi

            if crossing:
                # This is the key event: crossed the strait while dark
                msg = (
                    f"DARK TRANSIT — {now_pt()}\n\n"
                    f"Vessel: {vname} (MMSI: {mmsi})\n"
                    f"Type: {vessel.get('type', '?')}\n"
                    f"Transit: {crossing}\n"
                    f"Dark for: {dark_duration_min:.0f} minutes\n"
                    f"Last seen: {vessel.get('last_lat', 0):.3f}N {vessel.get('last_lon', 0):.3f}E ({dark_zone})\n"
                    f"Now at: {lat:.3f}N {lon:.3f}E ({zone})\n\n"
                    f"Vessel crossed the strait with transponder off."
                )
                log(f"DARK-TRANSIT: {vname} ({mmsi}) — {crossing}, dark {dark_duration_min:.0f}min")
                alert(msg)
                state["transit_events"].append({
                    "mmsi": mmsi, "name": vname, "type": vessel.get("type"),
                    "crossing": crossing, "dark_minutes": dark_duration_min,
                    "from_lat": vessel.get("last_lat"), "from_lon": vessel.get("last_lon"),
                    "from_zone": dark_zone,
                    "to_lat": lat, "to_lon": lon, "to_zone": zone,
                    "ts": now,
                })
            else:
                # Reappeared in same zone — just signal was lost temporarily
                log(f"SIGNAL-RESTORED: {vname} ({mmsi}) — dark {dark_duration_min:.0f}min, same zone ({zone})")

            vessel["dark_since"] = None
            vessel["dark_alerted"] = False

        # ── ZONE CHANGE (while visible) ──
        if zone != prev_zone and not vessel.get("dark_since"):
            crossing = crossed_strait(prev_zone, zone)
            if crossing:
                vname = vessel.get("name") or mmsi
                log(f"TRANSIT: {vname} ({mmsi}) — {crossing} (visible)")

        # ── Update position + record history ──
        vessel["last_seen"] = vessel_ts
        vessel["last_lat"] = lat
        vessel["last_lon"] = lon
        vessel["zone"] = zone
        vessel["last_zone"] = prev_zone
        if destination:
            vessel["destination"] = destination
        hist = vessel.setdefault("history", [])
        hist.append({"ts": vessel_ts, "lat": lat, "lon": lon, "zone": zone, "speed": speed})
        if len(hist) > MAX_HISTORY:
            vessel["history"] = hist[-MAX_HISTORY:]
        vessel["spoof_alerted"] = False

    # ── Check for vessels that went dark (not in this scan) ──
    for mmsi, vessel in list(vessels.items()):
        if mmsi in seen_mmsis:
            continue

        last_seen = vessel.get("last_seen", now)
        minutes_since = (now - last_seen) / 60

        if minutes_since >= DARK_THRESHOLD_MINUTES and not vessel.get("dark_alerted"):
            vname = vessel.get("name") or mmsi
            zone = vessel.get("zone", "?")
            lat = vessel.get("last_lat", 0)
            lon = vessel.get("last_lon", 0)
            vessel["dark_since"] = last_seen
            vessel["dark_alerted"] = True
            # Just log — the alert fires when they reappear in a different zone
            log(f"SIGNAL-LOST: {vname} ({mmsi}) — {minutes_since:.0f}min, last in {zone} at {lat:.3f}N {lon:.3f}E")
            state["dark_events"].append({
                "mmsi": mmsi, "name": vname, "zone": zone,
                "last_lat": lat, "last_lon": lon,
                "dark_since": last_seen, "ts": now,
            })

    # ── Prune very stale vessels (record as departures) ──
    state.setdefault("departure_events", [])
    stale_cutoff = now - STALE_HOURS * 3600
    stale = [(m, v) for m, v in vessels.items() if v.get("last_seen", 0) < stale_cutoff]
    for m, v in stale:
        vname = v.get("name") or m
        state["departure_events"].append({
            "mmsi": m, "name": vname, "zone": v.get("zone", "?"),
            "last_lat": v.get("last_lat", 0), "last_lon": v.get("last_lon", 0),
            "last_seen": v.get("last_seen", 0), "ts": now,
        })
        log(f"LEFT: {vname} ({m}) — not seen for >{STALE_HOURS}h, last in {v.get('zone','?')}")
        del vessels[m]
    if stale:
        log(f"Pruned {len(stale)} stale vessels (>{STALE_HOURS}h old)")

# ── Main loop ──────────────────────────────────────────────────────────────────

def monitor(poll_minutes=POLL_INTERVAL_MINUTES):
    state = load_state()
    # Ensure new event lists exist (migration from old format)
    state.setdefault("transit_events", [])
    state.setdefault("arrival_events", [])
    # Record when tracking started (persists across restarts only if not set)
    if "started_at" not in state:
        state["started_at"] = now_ts()
        save_state(state)

    total = len(state["vessels"])
    zones = {}
    for v in state["vessels"].values():
        z = v.get("zone", "?")
        zones[z] = zones.get(z, 0) + 1

    log(f"Hormuz monitor starting — {total} vessels tracked")
    log(f"Zones: {zones}")
    log(f"Poll interval: {poll_minutes}min | Dark threshold: {DARK_THRESHOLD_MINUTES}min")
    log(f"Coverage: Full Persian Gulf (Kuwait → Strait of Hormuz → Gulf of Oman)")

    cycle = 0
    while True:
        try:
            log("Running full area scan...")
            t0 = time.time()
            discovered = scan_full_area()
            elapsed = time.time() - t0
            log(f"Scan found {len(discovered)} vessels in {elapsed:.1f}s")

            process_scan(state, discovered, first_cycle=(cycle == 0))
            save_state(state)

            # Summary
            zones = {}
            dark_count = 0
            for v in state["vessels"].values():
                z = v.get("zone", "?")
                zones[z] = zones.get(z, 0) + 1
                if v.get("dark_since"):
                    dark_count += 1
            log(f"Total: {len(state['vessels'])} | Zones: {zones} | Dark: {dark_count}")
            log(f"Events: {len(state['transit_events'])} transits, {len(state['arrival_events'])} arrivals, {len(state['spoof_events'])} spoofs")

            cycle += 1

        except Exception as e:
            log(f"Poll error: {e}")

        log(f"Next poll in {poll_minutes} min...")
        time.sleep(poll_minutes * 60)

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hormuz Strait AIS Monitor")
    parser.add_argument("--status", action="store_true",
                        help="Show current state and exit")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL_MINUTES,
                        help=f"Minutes between polls (default: {POLL_INTERVAL_MINUTES})")
    args = parser.parse_args()

    if args.status:
        state = load_state()
        vessels = state["vessels"]
        zones = {}
        dark = []
        for mmsi, v in vessels.items():
            z = v.get("zone", "?")
            zones[z] = zones.get(z, 0) + 1
            if v.get("dark_since"):
                dark.append((mmsi, v))

        print(f"\nTotal vessels: {len(vessels)}")
        print(f"By zone: {zones}")
        print(f"Signal lost: {len(dark)}")

        if dark:
            print("\nDark vessels:")
            for mmsi, v in dark:
                mins = (now_ts() - v["dark_since"]) / 60
                name = v.get("name") or "Unknown"
                print(f"  {name:25s} MMSI:{mmsi} | {v.get('zone','?')} | dark {mins:.0f}min")

        print(f"\nDark transit events: {len(state.get('transit_events', []))}")
        print(f"New arrivals: {len(state.get('arrival_events', []))}")
        print(f"Spoof events: {len(state.get('spoof_events', []))}")

        # Show recent transit events
        transits = state.get("transit_events", [])[-10:]
        if transits:
            print("\nRecent dark transits:")
            for e in transits:
                print(f"  {e.get('name','?'):25s} {e['crossing']} | dark {e['dark_minutes']:.0f}min")
        return

    monitor(poll_minutes=args.poll_interval)

if __name__ == "__main__":
    main()
