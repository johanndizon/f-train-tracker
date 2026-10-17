# 🚇 F Train Tracker — East Broadway

Get Discord pings when an **F train** is approaching the **East Broadway** subway station in NYC. Powered by the MTA's real-time GTFS feed and GitHub Actions.

---

## Features

| Feature | Details |
|---|---|
| **Real-time MTA data** | Uses the official GTFS-RT protobuf feed for B/D/F/M trains |
| **Configurable direction** | Watch for uptown (Queens-bound), downtown (Brooklyn-bound), or both |
| **Time windows** | Only sends alerts during your chosen days and hours (Eastern Time) |
| **Alert threshold** | Customize how many minutes out a train must be to trigger a ping |
| **Deduplication** | Won't spam you about the same approaching train on consecutive checks |
| **Discord embeds** | Rich notifications with direction, ETA, and arrival time |

---

## Quick Start

### 1. Get an MTA API Key (free)

1. Go to **[api.mta.info](https://api.mta.info/)** and create an account.
2. After signing in, navigate to your **Access Keys** and generate a new key.
3. Copy the key — you'll need it for the next step.

### 2. Create a Discord Webhook

1. In your Discord server, go to **Server Settings → Integrations → Webhooks**.
2. Click **New Webhook**, name it something like `F Train Tracker`, and choose the target channel.
3. Click **Copy Webhook URL**.

### 3. Add Repository Secrets

In your GitHub repository, go to **Settings → Secrets and variables → Actions** and add two secrets:

| Secret Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | The full Discord webhook URL |
| `MTA_API_KEY` | Your MTA API key |

### 4. Configure the Tracker

Edit [`config.yaml`](config.yaml) to set your preferences:

```yaml
# Watch for uptown, downtown, or both directions
direction: "both"

# Alert when a train is ≤ this many minutes away
alert_threshold_minutes: 5

# Active time windows (Eastern Time)
time_windows:
  - days: ["monday", "tuesday", "wednesday", "thursday", "friday"]
    start_time: "07:00"
    end_time: "09:30"
  - days: ["monday", "tuesday", "wednesday", "thursday", "friday"]
    start_time: "17:00"
    end_time: "19:30"
```

### 5. Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit — F Train Tracker"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

The GitHub Actions workflow will start running automatically on the cron schedule. You can also trigger it manually from the **Actions** tab → **F Train Tracker** → **Run workflow**.

---

## Configuration Reference

### `config.yaml`

| Key | Type | Default | Description |
|---|---|---|---|
| `station_stop_id` | string | `"F16"` | GTFS stop ID for the station ([MTA stop list](https://data.ny.gov/Transportation/MTA-Subway-Stations/39hk-dx4f)) |
| `station_name` | string | `"East Broadway"` | Human-readable name (used in Discord embeds) |
| `direction` | string | `"both"` | `"uptown"`, `"downtown"`, or `"both"` |
| `alert_threshold_minutes` | number | `5` | Send alert when train is ≤ this many minutes away |
| `time_windows` | list | *(see file)* | Each entry has `days`, `start_time`, `end_time` |

### Direction Mapping at East Broadway

| Direction Value | Platform | Heading Toward |
|---|---|---|
| `"uptown"` | Northbound (`F16N`) | Manhattan → Midtown → Queens (Jamaica–179 St) |
| `"downtown"` | Southbound (`F16S`) | Brooklyn → Coney Island–Stillwell Av |

---

## Saving GitHub Actions Minutes

The default cron (`*/5 * * * *`) runs every 5 minutes around the clock. Runs outside your configured time windows exit in under 5 seconds and consume minimal compute, but they still count toward your monthly quota.

To optimize, tighten the cron in `.github/workflows/train-tracker.yml` to roughly match your time windows (in **UTC**):

```yaml
# Weekdays, 11:00–14:00 UTC and 21:00–00:00 UTC
# ≈ 7–10 AM ET (morning) and 5–8 PM ET (evening)
schedule:
  - cron: "*/5 11-13 * * 1-5"
  - cron: "*/5 21-23 * * 1-5"
```

> **Note:** Eastern Time is UTC−5 (EST) or UTC−4 (EDT). Account for daylight saving when setting UTC hours.

**Free-tier budget:** GitHub gives 2,000 minutes/month for private repos and unlimited for public repos. Each tracker run uses ~15–30 seconds.

---

## Tracking a Different Station

1. Find the station's GTFS stop ID from the [MTA GTFS static data](https://data.ny.gov/Transportation/MTA-Subway-Stations/39hk-dx4f) or the `stops.txt` file in the MTA GTFS bundle.
2. Update `station_stop_id` and `station_name` in `config.yaml`.
3. If the station is on a different line, also update the `FEED_URL` in `check_trains.py` and the route filter from `"F"` to the appropriate line letter.

---

## Local Testing

```bash
pip install -r requirements.txt

export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export MTA_API_KEY="your-key-here"

python check_trains.py
```

---

## How It Works

```
GitHub Actions (cron: */5 * * * *)
        │
        ▼
  check_trains.py
        │
        ├─ Load config.yaml
        ├─ Is current ET inside a time window? ──No──▶ Exit
        │       │
        │      Yes
        │       │
        ├─ Fetch MTA GTFS-RT feed (BDFM protobuf)
        ├─ Filter for route_id = "F" at stop F16N / F16S
        ├─ Calculate ETA for each matching trip
        ├─ ETA ≤ threshold AND not recently notified?
        │       │
        │      Yes
        │       │
        └─ POST Discord webhook with embed
```

---

## License

MIT
