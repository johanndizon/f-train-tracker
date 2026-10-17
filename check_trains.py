"""
F Train Tracker — East Broadway Station
========================================
Checks the MTA GTFS-Realtime feed for F trains approaching East Broadway
and sends a Discord webhook notification when a train is within the
configured alert threshold.

Designed to run as a GitHub Actions scheduled job.
"""

import json
import os
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from google.transit import gtfs_realtime_pb2

# ── Paths & Constants ────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"
CACHE_PATH = Path(__file__).parent / ".notified_cache.json"

# MTA GTFS-RT feed for B/D/F/M trains
FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-bdfm"

# How long (in seconds) to remember that we already notified about a trip,
# so we don't spam the same approaching train every run.
DEDUP_TTL_SECONDS = 15 * 60  # 15 minutes

# F train color (orange)
EMBED_COLOR = 0xF57F20

# ── Helpers ──────────────────────────────────────────────────────────────────


def load_config() -> dict:
    """Load and return the YAML configuration file."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def eastern_now() -> datetime:
    """Return the current time in US/Eastern as an aware datetime."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # Python < 3.9 fallback
    return datetime.now(ZoneInfo("America/New_York"))


def is_active(config: dict) -> bool:
    """Return True if the current Eastern Time falls inside any configured
    active time window."""
    now = eastern_now()
    day_name = now.strftime("%A").lower()
    current_time = now.strftime("%H:%M")

    for window in config.get("time_windows", []):
        if day_name in window["days"]:
            if window["start_time"] <= current_time <= window["end_time"]:
                return True
    return False


# ── Deduplication cache ──────────────────────────────────────────────────────


def _load_cache() -> dict:
    """Load the JSON dedup cache from disk (or return empty dict)."""
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    """Persist the dedup cache to disk."""
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)


def _cache_key(trip_id: str, stop_id: str) -> str:
    """Deterministic key for a trip+stop combination."""
    raw = f"{trip_id}:{stop_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def was_recently_notified(trip_id: str, stop_id: str) -> bool:
    """Check whether we already notified about this trip at this stop
    within the TTL window."""
    cache = _load_cache()
    key = _cache_key(trip_id, stop_id)
    if key not in cache:
        return False
    notified_at = cache[key]
    return (datetime.now(timezone.utc).timestamp() - notified_at) < DEDUP_TTL_SECONDS


def mark_notified(trip_id: str, stop_id: str) -> None:
    """Record that we just notified about this trip at this stop."""
    cache = _load_cache()
    now_ts = datetime.now(timezone.utc).timestamp()

    # Prune expired entries while we're here
    cache = {k: v for k, v in cache.items() if (now_ts - v) < DEDUP_TTL_SECONDS}

    cache[_cache_key(trip_id, stop_id)] = now_ts
    _save_cache(cache)


# ── MTA Feed ─────────────────────────────────────────────────────────────────


def fetch_feed(api_key: str | None = None) -> gtfs_realtime_pb2.FeedMessage:
    """Download and parse the GTFS-RT protobuf feed for BDFM trains."""
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    resp = requests.get(FEED_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    return feed


def find_approaching_trains(feed: gtfs_realtime_pb2.FeedMessage, config: dict) -> list[dict]:
    """Scan the feed for F trains arriving at East Broadway within the
    alert threshold. Returns a list of dicts with train details."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now = datetime.now(et)

    station_id = config["station_stop_id"]
    direction = config["direction"]
    threshold = config["alert_threshold_minutes"]

    # Build the set of platform stop IDs we care about
    watch_ids: set[str] = set()
    if direction in ("uptown", "both"):
        watch_ids.add(f"{station_id}N")
    if direction in ("downtown", "both"):
        watch_ids.add(f"{station_id}S")

    results: list[dict] = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        trip = entity.trip_update
        route_id = trip.trip.route_id

        # Only F trains
        if route_id != "F":
            continue

        for stu in trip.stop_time_update:
            if stu.stop_id not in watch_ids:
                continue

            # Prefer arrival time, fall back to departure time
            ts = 0
            if stu.HasField("arrival") and stu.arrival.time:
                ts = stu.arrival.time
            elif stu.HasField("departure") and stu.departure.time:
                ts = stu.departure.time

            if ts == 0:
                continue

            arrival_dt = datetime.fromtimestamp(ts, tz=et)
            eta_seconds = (arrival_dt - now).total_seconds()
            eta_minutes = eta_seconds / 60

            if eta_minutes < 0 or eta_minutes > threshold:
                continue

            dir_label = (
                "⬆️ Uptown (Queens-bound)"
                if stu.stop_id.endswith("N")
                else "⬇️ Downtown (Brooklyn-bound)"
            )

            results.append(
                {
                    "trip_id": trip.trip.trip_id,
                    "stop_id": stu.stop_id,
                    "direction": dir_label,
                    "eta_minutes": round(eta_minutes, 1),
                    "arrival_time": arrival_dt.strftime("%I:%M %p"),
                }
            )

    # Sort by soonest first
    results.sort(key=lambda t: t["eta_minutes"])
    return results


# ── Discord ──────────────────────────────────────────────────────────────────


def send_discord_alert(webhook_url: str, trains: list[dict], station_name: str, role_id: str = "") -> None:
    """Post one embed per approaching train to the Discord webhook.
    If role_id is set, the message will @mention that role."""
    for train in trains:
        # Skip if we already notified about this exact trip recently
        if was_recently_notified(train["trip_id"], train["stop_id"]):
            print(f"  ↳ Skipping trip {train['trip_id']} (already notified)")
            continue

        eta = train["eta_minutes"]
        eta_display = f"**{eta} min**" if eta >= 1 else "**< 1 min** 🚨"

        embed = {
            "title": f"🚇  F Train → {station_name}",
            "description": f"An F train is **{eta} minute{'s' if eta != 1 else ''}** away!",
            "color": EMBED_COLOR,
            "fields": [
                {"name": "Direction", "value": train["direction"], "inline": True},
                {"name": "ETA", "value": eta_display, "inline": True},
                {"name": "Arrives At", "value": train["arrival_time"], "inline": True},
            ],
            "footer": {"text": f"Trip {train['trip_id']}  •  MTA GTFS-RT"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Role mention must be in `content` (not the embed) to trigger a ping.
        payload: dict = {"embeds": [embed]}
        if role_id:
            payload["content"] = f"<@&{role_id}>"

        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp.raise_for_status()

        mark_notified(train["trip_id"], train["stop_id"])
        print(f"  ✓ Notified: {train['direction']}  ETA {train['eta_minutes']} min")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    print("═" * 50)
    print("F Train Tracker — East Broadway")
    print("═" * 50)

    # Load config
    config = load_config()
    station_name = config.get("station_name", "East Broadway")
    direction = config["direction"]
    threshold = config["alert_threshold_minutes"]
    role_id = config.get("discord_role_id", "")

    print(f"Station:   {station_name}  ({config['station_stop_id']})")
    print(f"Direction: {direction}")
    print(f"Threshold: {threshold} min")
    print(f"Role ping: {'<@&' + role_id + '>' if role_id else '(none)'}")
    print()

    # Check active schedule
    now_et = eastern_now()
    print(f"Current ET: {now_et.strftime('%A %I:%M %p')}")

    if not is_active(config):
        print("⏸  Outside active time window — exiting.")
        return

    print("▶  Inside active window — checking MTA feed…")
    print()

    # Read secrets from environment
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable is not set.")
        sys.exit(1)

    api_key = os.environ.get("MTA_API_KEY")  # optional but recommended

    # Fetch GTFS-RT data
    try:
        feed = fetch_feed(api_key)
    except requests.RequestException as exc:
        print(f"ERROR: Failed to fetch MTA feed: {exc}")
        sys.exit(1)

    print(f"Feed timestamp: {datetime.fromtimestamp(feed.header.timestamp, tz=timezone.utc).isoformat()}")
    print(f"Entities in feed: {len(feed.entity)}")
    print()

    # Search for approaching F trains
    trains = find_approaching_trains(feed, config)

    if not trains:
        print("No F trains approaching within threshold. ✅")
        return

    print(f"🚇 Found {len(trains)} approaching F train(s)!")
    send_discord_alert(webhook_url, trains, station_name, role_id)
    print()
    print("Done. ✅")


if __name__ == "__main__":
    main()
