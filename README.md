# server_monitor_metrics

Wall screen for the apps on `servidor`, shown by a Raspberry Pi (1 GB) on a TV.

```
┌──────────────── servidor ────────────────┐            ┌────────── Raspberry Pi ───────────┐
│ Rails apps + monitor_metrics gem          │            │ wallmon.py  (systemd, ~20 MB)     │
│   └─ /internal/metrics (localhost+token)  │            │   polls status.json every 30 s    │
│ nginx access.log                          │            │   serves dashboard on :8080       │
│ cron ▸ server/collect.py  (every minute)  │   HTTPS    │ cage + Chromium kiosk on HDMI     │
│   └─ /var/www/wallmon/status.json ────────┼─ basic ───►│   /?screen=1  rotates the views   │
│      served by nginx at /wallmon/…        │   auth     └───────────────────────────────────┘
└───────────────────────────────────────────┘
```

The Pi never logs in to the server: it only downloads one JSON file over HTTPS,
protected with basic auth.

| Part | Where | What |
|---|---|---|
| [`monitor_metrics`](https://github.com/jmjurado23/monitor_metrics) | separate gem repo, installed in each Rails app | health + business metrics endpoint |
| `server/` | `~/server_monitor/server` on servidor | collector: checks, nginx log traffic, host, docker → `status.json` |
| `pi/` | `/opt/wallmon` on the Pi | display server, dashboard, kiosk + screen schedule |

## Try the screens first (any computer)

```sh
python3 pi/wallmon.py --demo --port 8080
# http://localhost:8080/?screen=1            rotating, as on the wall
# http://localhost:8080/?view=app:cocina     one view pinned
```

← / → change view, space pauses the rotation.

## 1. In each Rails app

```ruby
# Gemfile
gem "monitor_metrics", git: "https://github.com/jmjurado23/monitor_metrics", tag: "v0.2.0"
```

The app's initializer (`config/initializers/monitor_metrics.rb`, examples in the gem's
`examples/`) is the only place an app is configured: name, URL, order, what to show and
when to alarm. On its next restart in production the app writes
`~/.wallmon/apps.d/<id>.json` and the collector picks it up. Nothing is edited on the
server or the Pi to add, rename or reorder an app.

## 2. On the server

```sh
git clone https://github.com/jmjurado23/server_monitor_metrics ~/server_monitor
cd ~/server_monitor/server
./install.sh
```

`install.sh` creates `apps.json` (global settings), the shared token
(`~/.monitor_metrics_token`, read by the gem and the collector), the registry folder
`~/.wallmon/apps.d`, `/var/www/wallmon`, adds you to group `adm` so the nginx logs are
readable, does a test run and installs the cron job.

```sh
python3 collect.py --list              # apps found: registered + apps.json
python3 collect.py --forget <app_id>   # an app removed from the server
```

Then prepare nginx. `nginx_setup.py` finds the server blocks of every app the collector
knows and shows the exact diff first:

```sh
python3 nginx_setup.py                  # dry run: shows the diff, changes nothing
sudo python3 nginx_setup.py --apply     # from a real terminal: asks for the wallmon password
```

It adds the `wallmon` log format to the apps' `access_log` lines (traffic per app and
response times), a `location ^~ /internal/ { return 404; }` to each app's HTTPS block, and
publishes `https://<first app>/wallmon/status.json` behind basic auth (`--status-domain`
picks another site). It also points `apps.json` at the log file the apps write to. Every
changed file is backed up to `/etc/nginx/wallmon-backup-<date>/`, `nginx -t` must pass or
the backup is restored, and running it again changes nothing. To do it by hand instead,
see [`server/nginx-wallmon.conf`](server/nginx-wallmon.conf).

`apps.json` options worth knowing:
- `apps`: only sites **without** the gem (public check, TLS, traffic), or server-side
  overrides for a registered app with the same id (for example its own `access_log`).
- `access_log`: the nginx log every app is read from unless it says otherwise.
- `docker_watch`: names of the database containers. If one of them stops, the screen shows it.
- `slow_ms`: response time above which an app turns WARNING (default 2000).
- `cert_warn_days`: TLS warning threshold (default 14).

App states: **DOWN** means the public check fails, returns 5xx, nothing listens on the app's port, or a metric passed its `critical_` threshold. **WARNING** means it's slow, more than 5% of requests return 5xx, the DB ping or metrics endpoint fails, a metric passed its `warn_` threshold, or the TLS certificate expires soon.

## 3. On the Raspberry Pi

1. Flash **Raspberry Pi OS Lite** (32-bit for a Pi 2, 64-bit is fine on a Pi 3) and enable SSH.
2. Then run:

```sh
git clone https://github.com/jmjurado23/server_monitor_metrics ~/server_monitor
cd ~/server_monitor/pi && ./setup.sh
sudo nano /etc/wallmon.env              # URL, user, password of status.json
nano /opt/wallmon/dashboard.json        # optional: "apps" already rotates through every app
sudo reboot
```

After the reboot the Pi logs in on tty1 and starts Chromium full screen. By default the screen turns off at 00:00 and back on at 07:30, and the Pi reboots at 07:25 every day. Change these with `crontab -e`.

## Two screens

Every view is a URL, so a screen is any browser that opens one:

- **One HDMI output (Pi 2/3):** `?screen=1` rotates through all views. When an app is DOWN, the rotation only alternates the overview with the broken apps.
- **Second screen:** set `WALLMON_BIND=0.0.0.0` on the Pi and open `http://<pi-ip>:8080/?screen=2` on the second device (another Pi, a TV stick or an old laptop). `screens["2"]` in `dashboard.json` sets what it shows.
- **Pi 4/5 (two HDMI outputs):** run a second Chromium with `?screen=2` on the second output.

## Tests

```sh
python3 -m unittest discover -s server/test
```
