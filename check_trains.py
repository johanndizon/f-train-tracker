"""
F Train Monitor — East Broadway
=================================
Combined monitor with two independent features:

  1. TRAIN TRACKER  — Pings when an F train is approaching the station
                      within a configurable ETA threshold.

  2. SERVICE ALERTS — Notifies about delays, suspensions, service changes,
                      planned work, and non-F trains rerouted through
                      the station.

Each feature has its own on/off switch and active time windows.
Designed to run as a GitHub Actions job triggered by cron-job.org.
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

# MTA GTFS-RT feed URLs
BDFM_FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-bdfm"
ALERTS_FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts"

# Dedup TTLs (seconds)
DEDUP_TTL_TRAIN = 15 * 60       # 15 min — same approaching train
DEDUP_TTL_ALERT = 6 * 60 * 60   # 6 hrs  — same service alert
DEDUP_TTL_ROUTE = 6 * 60 * 60   # 6 hrs  — same unusual route

# F train embed color (orange)
COLOR_F_TRAIN = 0xF57F20

# Service alert embed colors
COLOR_RED = 0xE74C3C
COLOR_ORANGE = 0xF57F20
COLOR_YELLOW = 0xF1C40F
COLOR_BLUE = 0x3498DB
COLOR_GREEN = 0x2ECC71

# GTFS-RT effect enum → (label, emoji, color)
EFFECT_MAP = {
    1: ("No Service", "🚫", COLOR_RED),
    2: ("Reduced Service", "⚠️", COLOR_YELLOW),
    3: ("Significant Delays", "🐢", COLOR_RED),
    4: ("Detour", "↪️", COLOR_ORANGE),
    5: ("Additional Service", "➕", COLOR_BLUE),
    6: ("Modified Service", "🔀", COLOR_ORANGE),
    7: ("Other", "ℹ️", COLOR_BLUE),
    8: ("Unknown", "❓", COLOR_BLUE),
    9: ("Stop Moved", "📍", COLOR_ORANGE),
}


# ── Timezone ─────────────────────────────────────────────────────────────────

def _et_zone():
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


def eastern_now() -> datetime:
    return datetime.now(_et_zone())


# ── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def is_in_window(time_windows: list[dict]) -> bool:
    """Check if current Eastern Time falls inside any of the given windows."""
    now = eastern_now()
    day_name = now.strftime("%A").lower()
    current_time = now.strftime("%H:%M")
    for w in time_windows:
        if day_name in w["days"] and w["start_time"] <= current_time <= w["end_time"]:
            return True
    return False


# ── Dedup Cache ──────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)


def _key(identifier: str) -> str:
    return hashlib.sha256(identifier.encode()).hexdigest()[:20]


def was_recently_notified(identifier: str, ttl: int) -> bool:
    cache = _load_cache()
    k = _key(identifier)
    if k not in cache:
        return False
    return (datetime.now(timezone.utc).timestamp() - cache[k]) < ttl


def mark_notified(identifier: str) -> None:
    cache = _load_cache()
    now_ts = datetime.now(timezone.utc).timestamp()
    # Prune anything older than the longest TTL
    max_ttl = max(DEDUP_TTL_TRAIN, DEDUP_TTL_ALERT, DEDUP_TTL_ROUTE)
    cache = {k: v for k, v in cache.items() if (now_ts - v) < max_ttl}
    cache[_key(identifier)] = now_ts
    _save_cache(cache)


# ── MTA Feed ─────────────────────────────────────────────────────────────────

def fetch_feed(url: str, api_key: str | None = None) -> gtfs_realtime_pb2.FeedMessage:
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    return feed


def _translated_text(ts) -> str:
    if not ts or not ts.translation:
        return ""
    for t in ts.translation:
        if t.language in ("en", ""):
            return t.text
    return ts.translation[0].text


# =============================================================================
# FEATURE 1 — Train Tracker
# =============================================================================

def find_approaching_trains(feed: gtfs_realtime_pb2.FeedMessage, config: dict) -> list[dict]:
    """Find F trains arriving at the station within the ETA threshold."""
    et = _et_zone()
    now = datetime.now(et)

    station_id = config["station_stop_id"]
    tracker = config["train_tracker"]
    direction = tracker["direction"]
    threshold = tracker["alert_threshold_minutes"]

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
        if trip.trip.route_id != config["primary_route"]:
            continue

        for stu in trip.stop_time_update:
            if stu.stop_id not in watch_ids:
                continue

            ts = 0
            if stu.HasField("arrival") and stu.arrival.time:
                ts = stu.arrival.time
            elif stu.HasField("departure") and stu.departure.time:
                ts = stu.departure.time
            if ts == 0:
                continue

            arrival_dt = datetime.fromtimestamp(ts, tz=et)
            eta_min = (arrival_dt - now).total_seconds() / 60

            if eta_min < 0 or eta_min > threshold:
                continue

            dir_label = (
                "⬆️ Uptown (Queens-bound)"
                if stu.stop_id.endswith("N")
                else "⬇️ Downtown (Brooklyn-bound)"
            )
            results.append({
                "trip_id": trip.trip.trip_id,
                "stop_id": stu.stop_id,
                "direction": dir_label,
                "eta_minutes": round(eta_min, 1),
                "arrival_time": arrival_dt.strftime("%I:%M %p"),
            })

    results.sort(key=lambda t: t["eta_minutes"])
    return results


def send_train_notification(webhook_url: str, train: dict, role_id: str, station_name: str) -> None:
    dedup_id = f"train:{train['trip_id']}:{train['stop_id']}"
    if was_recently_notified(dedup_id, DEDUP_TTL_TRAIN):
        print(f"    ↳ Skipping trip {train['trip_id']} (already notified)")
        return

    eta = train["eta_minutes"]
    eta_display = f"**{eta} min**" if eta >= 1 else "**< 1 min** 🚨"

    embed = {
        "title": f"🚇  F Train → {station_name}",
        "description": f"An F train is **{eta} minute{'s' if eta != 1 else ''}** away!",
        "color": COLOR_F_TRAIN,
        "fields": [
            {"name": "Direction", "value": train["direction"], "inline": True},
            {"name": "ETA", "value": eta_display, "inline": True},
            {"name": "Arrives At", "value": train["arrival_time"], "inline": True},
        ],
        "footer": {"text": f"Trip {train['trip_id']}  •  MTA GTFS-RT"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    payload: dict = {"embeds": [embed]}
    if role_id:
        payload["content"] = f"<@&{role_id}>"

    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()
    mark_notified(dedup_id)
    print(f"    ✓ ETA alert: {train['direction']}  {train['eta_minutes']} min")


# =============================================================================
# FEATURE 2 — Service Alerts
# =============================================================================

def find_relevant_alerts(feed: gtfs_realtime_pb2.FeedMessage, config: dict) -> list[dict]:
    """Find active MTA service alerts for the configured route/station."""
    station_id = config["station_stop_id"]
    primary_route = config["primary_route"]
    now_ts = datetime.now(timezone.utc).timestamp()

    results: list[dict] = []

    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert

        # Check if currently active
        is_current = not alert.active_period
        for period in alert.active_period:
            start = period.start if period.start else 0
            end = period.end if period.end else float("inf")
            if start <= now_ts <= end:
                is_current = True
                break
        if not is_current:
            continue

        # Check relevance
        matches_route = False
        matches_station = False
        affected_routes: set[str] = set()

        for ie in alert.informed_entity:
            if ie.route_id:
                affected_routes.add(ie.route_id)
                if ie.route_id == primary_route:
                    matches_route = True
            if ie.stop_id and ie.stop_id.startswith(station_id):
                matches_station = True

        if not (matches_route or matches_station):
            continue

        header = _translated_text(alert.header_text)
        description = _translated_text(alert.description_text)
        effect_val = alert.effect if alert.effect else 7
        effect_label, effect_emoji, color = EFFECT_MAP.get(effect_val, ("Other", "ℹ️", COLOR_BLUE))

        if matches_station and matches_route:
            relevance = f"{primary_route} train at {config['station_name']}"
        elif matches_station:
            relevance = f"{config['station_name']} station"
        else:
            relevance = f"{primary_route} train line"

        results.append({
            "alert_id": entity.id,
            "header": header,
            "description": description,
            "effect_label": effect_label,
            "effect_emoji": effect_emoji,
            "color": color,
            "affected_routes": sorted(affected_routes),
            "relevance": relevance,
        })

    return results


def find_unusual_routes(feed: gtfs_realtime_pb2.FeedMessage, config: dict) -> list[dict]:
    """Detect non-primary-route trains stopping at the station."""
    station_id = config["station_stop_id"]
    primary_route = config["primary_route"]
    unusual: dict[str, dict] = {}

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip = entity.trip_update
        route_id = trip.trip.route_id
        if route_id == primary_route:
            continue

        for stu in trip.stop_time_update:
            if stu.stop_id.startswith(station_id) and route_id not in unusual:
                direction = (
                    "Northbound" if stu.stop_id.endswith("N") else
                    "Southbound" if stu.stop_id.endswith("S") else
                    "Unknown"
                )
                unusual[route_id] = {
                    "route_id": route_id,
                    "stop_id": stu.stop_id,
                    "direction": direction,
                    "trip_id": trip.trip.trip_id,
                }

    return list(unusual.values())


def send_service_alert_notification(webhook_url: str, alert: dict, role_id: str, station_name: str) -> None:
    dedup_id = f"alert:{alert['alert_id']}"
    if was_recently_notified(dedup_id, DEDUP_TTL_ALERT):
        print(f"    ↳ Skipping alert {alert['alert_id']} (already notified)")
        return

    desc = alert["description"]
    if len(desc) > 1000:
        desc = desc[:997] + "…"

    routes_str = ", ".join(alert["affected_routes"]) if alert["affected_routes"] else "—"

    embed = {
        "title": f"{alert['effect_emoji']}  {alert['effect_label']} — {station_name}",
        "description": alert["header"],
        "color": alert["color"],
        "fields": [
            {"name": "Relevance", "value": alert["relevance"], "inline": True},
            {"name": "Affected Lines", "value": routes_str, "inline": True},
        ],
        "footer": {"text": f"Alert ID: {alert['alert_id']}  •  MTA Service Alerts"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if desc and desc != alert["header"]:
        embed["fields"].append({"name": "Details", "value": desc, "inline": False})

    payload: dict = {"embeds": [embed]}
    if role_id:
        payload["content"] = f"<@&{role_id}>"

    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()
    mark_notified(dedup_id)
    print(f"    ✓ Alert: {alert['effect_emoji']} {alert['effect_label']} — {alert['header'][:60]}")


def send_unusual_route_notification(webhook_url: str, route_info: dict, role_id: str, station_name: str) -> None:
    dedup_id = f"unusual:{route_info['route_id']}:{route_info['direction']}"
    if was_recently_notified(dedup_id, DEDUP_TTL_ROUTE):
        print(f"    ↳ Skipping unusual route {route_info['route_id']} (already notified)")
        return

    embed = {
        "title": f"🔀  Unusual Train at {station_name}",
        "description": (
            f"A **{route_info['route_id']} train** is currently scheduled to stop at "
            f"{station_name} — this station is normally only served by the F."
        ),
        "color": COLOR_GREEN,
        "fields": [
            {"name": "Route", "value": route_info["route_id"], "inline": True},
            {"name": "Direction", "value": route_info["direction"], "inline": True},
        ],
        "footer": {"text": "MTA GTFS-RT  •  Possible reroute"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    payload: dict = {"embeds": [embed]}
    if role_id:
        payload["content"] = f"<@&{role_id}>"

    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()
    mark_notified(dedup_id)
    print(f"    ✓ Unusual route: {route_info['route_id']} train ({route_info['direction']})")


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    print("═" * 55)
    print("F Train Monitor — East Broadway")
    print("═" * 55)

    config = load_config()
    station_name = config.get("station_name", "East Broadway")
    primary_route = config["primary_route"]
    role_id = config.get("discord_role_id", "")

    tracker_cfg = config.get("train_tracker", {})
    alerts_cfg = config.get("service_alerts", {})

    tracker_on = tracker_cfg.get("enabled", False)
    alerts_on = alerts_cfg.get("enabled", False)

    print(f"Station:        {station_name} ({config['station_stop_id']})")
    print(f"Primary line:   {primary_route}")
    print(f"Role ping:      {'<@&' + role_id + '>' if role_id else '(none)'}")
    print(f"Train tracker:  {'ON' if tracker_on else 'OFF'}")
    print(f"Service alerts: {'ON' if alerts_on else 'OFF'}")
    print()

    if not tracker_on and not alerts_on:
        print("Both features are disabled. Nothing to do.")
        return

    now_et = eastern_now()
    print(f"Current ET: {now_et.strftime('%A %I:%M %p')}")
    print()

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable is not set.")
        sys.exit(1)

    api_key = os.environ.get("MTA_API_KEY")
    bdfm_feed = None  # lazy-loaded, shared between features

    # ─────────────────────────────────────────────────────────────────────
    # FEATURE 1: Train Tracker
    # ─────────────────────────────────────────────────────────────────────
    if tracker_on:
        tracker_windows = tracker_cfg.get("time_windows", [])
        if is_in_window(tracker_windows):
            print("🚇 TRAIN TRACKER — active")
            try:
                if bdfm_feed is None:
                    bdfm_feed = fetch_feed(BDFM_FEED_URL, api_key)
                direction = tracker_cfg.get("direction", "both")
                threshold = tracker_cfg.get("alert_threshold_minutes", 5)
                print(f"   Direction: {direction}  |  Threshold: {threshold} min")

                trains = find_approaching_trains(bdfm_feed, config)
                if trains:
                    print(f"   Found {len(trains)} approaching train(s):")
                    for t in trains:
                        send_train_notification(webhook_url, t, role_id, station_name)
                else:
                    print("   No trains approaching within threshold. ✅")
            except requests.RequestException as exc:
                print(f"   ERROR fetching BDFM feed: {exc}")
        else:
            print("🚇 TRAIN TRACKER — outside time window, skipping")
        print()

    # ─────────────────────────────────────────────────────────────────────
    # FEATURE 2: Service Alerts
    # ─────────────────────────────────────────────────────────────────────
    if alerts_on:
        alerts_windows = alerts_cfg.get("time_windows", [])
        if is_in_window(alerts_windows):
            print("🚨 SERVICE ALERTS — active")

            # 2a. MTA service alerts
            if alerts_cfg.get("delays_and_alerts", True):
                print("   ── Checking alerts feed…")
                try:
                    alerts_feed = fetch_feed(ALERTS_FEED_URL, api_key)
                    print(f"      Entities: {len(alerts_feed.entity)}")
                    alerts = find_relevant_alerts(alerts_feed, config)
                    if alerts:
                        print(f"      Found {len(alerts)} relevant alert(s):")
                        for a in alerts:
                            send_service_alert_notification(webhook_url, a, role_id, station_name)
                    else:
                        print("      No active alerts. ✅")
                except requests.RequestException as exc:
                    print(f"      ERROR fetching alerts feed: {exc}")

            # 2b. Unusual routes
            if alerts_cfg.get("unusual_routes", True):
                print("   ── Checking for unusual routes…")
                try:
                    if bdfm_feed is None:
                        bdfm_feed = fetch_feed(BDFM_FEED_URL, api_key)
                    unusual = find_unusual_routes(bdfm_feed, config)
                    if unusual:
                        print(f"      Found {len(unusual)} unusual route(s):")
                        for r in unusual:
                            send_unusual_route_notification(webhook_url, r, role_id, station_name)
                    else:
                        print("      No unusual routes. ✅")
                except requests.RequestException as exc:
                    print(f"      ERROR fetching BDFM feed: {exc}")
        else:
            print("🚨 SERVICE ALERTS — outside time window, skipping")
        print()

    print("Done. ✅")


if __name__ == "__main__":
    main()
