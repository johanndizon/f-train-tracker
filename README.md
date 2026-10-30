# 🚇 F Train Monitor — East Broadway

Get Discord notifications for **approaching F trains** and **service disruptions** at East Broadway. Two independent features, each with its own on/off switch and schedule.

---

## Features

| Feature | What It Does | Toggle |
|---|---|---|
| **Train Tracker** | Pings when an F train is ≤ X minutes from the station | `train_tracker.enabled` |
| **Service Alerts** | Notifies about delays, suspensions, service changes, rerouted trains | `service_alerts.enabled` |

Both features run from the same script and cron job. Each has its own time windows — e.g., ETA alerts only during your commute, disruption alerts all day.

---

## Setup

1. **MTA API key** — free at [api.mta.info](https://api.mta.info/)
2. **Discord webhook** — Channel Settings → Integrations → Webhooks → copy URL
3. **Discord role ID** *(optional)* — Developer Mode on → right-click role → Copy Role ID
4. **GitHub repo** — push the code
5. **Repository secrets** — add `DISCORD_WEBHOOK_URL` and `MTA_API_KEY`
6. **GitHub PAT** — [fine-grained token](https://github.com/settings/tokens?type=beta) with Contents read/write for this repo
7. **cron-job.org** — POST to `https://api.github.com/repos/YOU/REPO/dispatches` every 5 min with body `{"event_type":"check-trains-cron"}`

See the full setup guide for step-by-step instructions.

---

## Configuration

Edit `config.yaml` to control everything:

```yaml
# Turn each feature on/off independently
train_tracker:
  enabled: true               # ETA countdown alerts
  direction: "both"           # "uptown", "downtown", or "both"
  alert_threshold_minutes: 5
  time_windows:               # only during commute
    - days: ["monday", "tuesday", "wednesday", "thursday", "friday"]
      start_time: "07:00"
      end_time: "09:30"

service_alerts:
  enabled: true               # delays / disruptions
  delays_and_alerts: true     # MTA service alerts
  unusual_routes: true        # non-F trains at the station
  time_windows:               # broader coverage
    - days: ["monday", "tuesday", "wednesday", "thursday", "friday"]
      start_time: "05:00"
      end_time: "23:59"
```

---

## License

MIT
