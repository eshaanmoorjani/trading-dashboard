# Hormuz Dark Ship Monitor

Monitors the Strait of Hormuz for vessels that turn off their AIS transponders during transit.

## What it detects

- 🔴 **Going dark** — vessel enters Hormuz, stops transmitting for >20 min
- 🚢 **Dark transit complete** — vessel reappears on the other side after dark period
- ⚠️ **GPS spoofing** — position jumps impossibly far (ships "appearing in Russia")

## Setup

```bash
pip install websockets

# Get a free API key at https://aisstream.io (sign up in browser, JS-rendered)
export AISSTREAM_API_KEY=your_key_here

python monitor.py
```

## Check current state

```bash
python monitor.py --status
```

## Config (top of monitor.py)

- `HORMUZ_BOX` — bounding box lat/lon for the strait
- `DARK_THRESHOLD_MINUTES` — how long before flagging a vessel as dark (default: 20)
- `SPOOF_THRESHOLD_KM` — max plausible position jump in one update (default: 300km)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — alert destination

## State files

- `vessels.json` — live vessel state (position, dark status, event history)
- `monitor.log` — running log of all events

## Data source

[aisstream.io](https://aisstream.io) — free WebSocket AIS feed, global coverage.
