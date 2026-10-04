#!/usr/bin/env python3
"""Wall monitor collector. Runs on the server from cron once a minute.

Each run:
  * reads the new lines of the nginx access logs (per-minute traffic buckets),
  * checks every app publicly (HTTPS through nginx) and locally (port, screen),
  * asks every app's /internal/metrics endpoint (monitor_metrics gem),
  * samples host CPU / memory / disk / docker,
and writes one status.json that nginx serves to the Raspberry Pi.

Standard library only; keep it compatible with Python 3.6 (no walrus, no match).
"""

import argparse
import fcntl
import http.client
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime

SCHEMA = 1
USER_AGENT = "wallmon-collector/1"
# Latency histogram upper bounds in ms; the last bucket is "anything slower".
LAT_BOUNDS = [25, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000]
HISTORY_STEP = 600          # 10-minute points for the 24 h charts
MINUTE_POINTS = 60          # last hour at 1-minute resolution
FIRST_READ_MAX = 50 * 1024 * 1024
STATIC_RE = re.compile(
    r"^/(assets|packs|vite|uploads|system|images|fonts)/"
    r"|\.(css|js|map|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|eot|mp3|mp4|txt|xml|json)$",
    re.I)
BOT_RE = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|monitor|curl|wget|python-requests", re.I)

LOG_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<req>[^"]*)" (?P<status>\d{3}) \S+'
    r'(?: "(?P<ref>[^"]*)" "(?P<ua>[^"]*)")?(?P<rest>.*)$')
KV_RE = re.compile(r'(\w+)=(\S+)')
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


# --------------------------------------------------------------------------- utils

def expand(path):
    return os.path.expanduser(os.path.expandvars(path)) if path else path


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data, mode=0o644):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def short_error(exc):
    text = "%s: %s" % (type(exc).__name__, exc)
    return text[:200]


def parse_log_time(text):
    """'03/Oct/2026:20:04:05 +0200' -> epoch seconds (fast path, no strptime)."""
    try:
        day, mon, rest = text.split("/", 2)
        year, hh, mm, rest2 = rest.split(":", 3)
        ss, tz = rest2.split(" ")
        sign = -1 if tz[0] == "-" else 1
        offset = sign * (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60)
        days = days_from_civil(int(year), MONTHS[mon], int(day))
        return days * 86400 + int(hh) * 3600 + int(mm) * 60 + int(ss) - offset
    except (ValueError, KeyError, IndexError):
        return None


def days_from_civil(y, m, d):
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_line(line):
    """Returns a dict for one nginx access-log line, or None if it does not parse.

    Understands the stock "combined" format and, optionally, trailing key=value
    fields from the recommended format: rt=$request_time host=$host
    """
    m = LOG_RE.match(line.rstrip("\n"))
    if not m:
        return None
    ts = parse_log_time(m.group("time"))
    if ts is None:
        return None
    parts = m.group("req").split(" ")
    path = parts[1] if len(parts) >= 2 else "-"
    extra = dict(KV_RE.findall(m.group("rest") or ""))
    rt = None
    if "rt" in extra:
        try:
            rt = float(extra["rt"]) * 1000.0
        except ValueError:
            rt = None
    host = extra.get("host")
    return {
        "ts": ts,
        "ip": m.group("ip"),
        "method": parts[0] if parts else "-",
        "path": path.split("?", 1)[0][:200],
        "status": int(m.group("status")),
        "ua": m.group("ua") or "",
        "rt_ms": rt,
        "host": host.lower().split(":")[0] if host else None,
    }


def lat_bucket(ms):
    for i, bound in enumerate(LAT_BOUNDS):
        if ms <= bound:
            return i
    return len(LAT_BOUNDS)


def hist_quantile(hist, q):
    total = sum(hist)
    if total == 0:
        return None
    target = q * total
    running = 0
    for i, count in enumerate(hist):
        running += count
        if running >= target:
            return LAT_BOUNDS[i] if i < len(LAT_BOUNDS) else LAT_BOUNDS[-1] * 2
    return None


def new_bucket():
    return {"n": 0, "p": 0, "b": 0, "e4": 0, "e5": 0, "h": [0] * (len(LAT_BOUNDS) + 1)}


# ------------------------------------------------------------------- log reading

def read_new_lines(path, saved):
    """Returns (lines, new_saved, error): the complete lines appended since the
    saved {inode, offset}. Follows one rotation (path.1) and copytruncate. A
    trailing partial line is left for the next run."""
    lines = []
    try:
        st = os.stat(path)
    except OSError as e:
        return lines, saved, short_error(e)

    saved = saved or {}
    inode, offset = saved.get("inode"), saved.get("offset", 0)
    first_read = inode is None
    try:
        if not first_read and inode != st.st_ino:
            try:
                rotated = path + ".1"
                if os.stat(rotated).st_ino == inode:
                    with open(rotated, "rb") as f:
                        f.seek(offset)
                        lines.extend(decode_lines(f.read()))
            except OSError:
                pass
            offset = 0
        if first_read:
            offset = max(0, st.st_size - FIRST_READ_MAX)
        if st.st_size < offset:          # truncated in place
            offset = 0
        with open(path, "rb") as f:
            f.seek(offset)
            if first_read and offset:
                f.readline()             # drop the partial line we landed in
            start = f.tell()
            chunk = f.read()
        last_nl = chunk.rfind(b"\n")
        complete = chunk[:last_nl + 1] if last_nl >= 0 else b""
        lines.extend(decode_lines(complete))
        return lines, {"inode": st.st_ino, "offset": start + len(complete)}, None
    except OSError as e:
        return lines, saved or None, short_error(e)


def decode_lines(data):
    if not data:
        return []
    text = data.decode("utf-8", errors="replace")
    return [l for l in text.split("\n") if l]


def ingest_logs(config, apps, state, now):
    """Adds new access-log lines into state['traffic'][app_id] minute buckets."""
    files = {}
    for app in apps:
        log = expand(app.get("access_log"))
        if log:
            files.setdefault(log, []).append(app)

    traffic = state.setdefault("traffic", {})
    offsets = state.setdefault("offsets", {})
    errors = {}
    window_start = now - config.get("history_hours", 24) * 3600

    for path, apps in files.items():
        lines, new_saved, error = read_new_lines(path, offsets.get(path))
        if new_saved:
            offsets[path] = new_saved
        if error:
            for app in apps:
                errors[app["id"]] = error
            continue

        host_map = {}
        for app in apps:
            for h in app_hosts(app):
                host_map[h] = app["id"]
        only_app = apps[0]["id"] if len(apps) == 1 else None

        for line in lines:
            rec = parse_line(line)
            if rec is None or rec["ts"] < window_start:
                continue
            if rec["ua"].startswith("wallmon-") or rec["path"].startswith("/internal/"):
                continue
            app_id = host_map.get(rec["host"]) if rec["host"] else only_app
            if app_id is None:
                continue
            add_record(traffic.setdefault(app_id, {}), rec)
    return errors


def app_hosts(app):
    hosts = set(h.lower() for h in app.get("hosts", []))
    url = app.get("url", "")
    m = re.match(r"https?://([^/:]+)", url)
    if m:
        hosts.add(m.group(1).lower())
        hosts.add("www." + m.group(1).lower())
    return hosts


def add_record(app_traffic, rec):
    minutes = app_traffic.setdefault("m", {})
    key = str(rec["ts"] // 60 * 60)
    b = minutes.get(key)
    if b is None:
        b = minutes[key] = new_bucket()
    is_bot = bool(BOT_RE.search(rec["ua"]))
    is_page = not STATIC_RE.search(rec["path"])
    b["n"] += 1
    if is_bot:
        b["b"] += 1
    elif is_page:
        b["p"] += 1
    if 400 <= rec["status"] < 500:
        b["e4"] += 1
    elif rec["status"] >= 500:
        b["e5"] += 1
    if rec["rt_ms"] is not None:
        b["h"][lat_bucket(rec["rt_ms"])] += 1

    hour = str(rec["ts"] // 3600 * 3600)
    if not is_bot:
        visitors = app_traffic.setdefault("v", {}).setdefault(hour, [])
        ip_hash = hashlib.sha1(rec["ip"].encode()).hexdigest()[:10]
        if ip_hash not in visitors:
            visitors.append(ip_hash)
        if is_page and rec["status"] < 400:
            pages = app_traffic.setdefault("tp", {}).setdefault(hour, {})
            pages[rec["path"]] = pages.get(rec["path"], 0) + 1
    if rec["status"] >= 500:
        errs = app_traffic.setdefault("te", {}).setdefault(hour, {})
        label = "%s %s" % (rec["status"], rec["path"])
        errs[label] = errs.get(label, 0) + 1


def prune_traffic(state, now, hours):
    cutoff = now - hours * 3600
    hour_cutoff = cutoff // 3600 * 3600
    for app_traffic in state.get("traffic", {}).values():
        for kind, limit in (("m", cutoff), ("v", hour_cutoff), ("tp", hour_cutoff), ("te", hour_cutoff)):
            data = app_traffic.get(kind, {})
            for key in [k for k in data if int(k) < limit]:
                del data[key]
        # keep per-hour page counters small
        for counter_key in ("tp", "te"):
            for hour, counts in app_traffic.get(counter_key, {}).items():
                if len(counts) > 60:
                    app_traffic[counter_key][hour] = dict(Counter(counts).most_common(60))


def summarize_traffic(app_traffic, now, error):
    minutes = (app_traffic or {}).get("m", {})
    if not minutes and error:
        return {"available": False, "error": error}

    cur_min = now // 60 * 60

    def bucket(ts):
        return minutes.get(str(ts))

    def sum_range(start, end, field):
        total = 0
        for ts in range(start // 60 * 60, end, 60):
            b = bucket(ts)
            if b:
                total += b[field]
        return total

    def hist_range(start, end):
        merged = [0] * (len(LAT_BOUNDS) + 1)
        for ts in range(start // 60 * 60, end, 60):
            b = bucket(ts)
            if b:
                merged = [x + y for x, y in zip(merged, b["h"])]
        return merged

    minute_start = cur_min - (MINUTE_POINTS - 1) * 60
    minute_req, minute_e5 = [], []
    for ts in range(minute_start, cur_min + 60, 60):
        b = bucket(ts)
        minute_req.append(b["n"] if b else 0)
        minute_e5.append(b["e5"] if b else 0)

    hist_end = now // HISTORY_STEP * HISTORY_STEP + HISTORY_STEP
    hist_start = hist_end - 24 * 3600
    h_req, h_pages, h_e5, h_p95 = [], [], [], []
    for start in range(hist_start, hist_end, HISTORY_STEP):
        end = start + HISTORY_STEP
        h_req.append(sum_range(start, end, "n"))
        h_pages.append(sum_range(start, end, "p"))
        h_e5.append(sum_range(start, end, "e5"))
        h_p95.append(hist_quantile(hist_range(start, end), 0.95))

    hour_ago = now - 3600
    day_ago = now - 86400
    hist_1h = hist_range(hour_ago, now + 60)
    visitors = (app_traffic or {}).get("v", {})
    current_hour = now // 3600 * 3600
    visitors_24h = set()
    for hour, ips in visitors.items():
        if int(hour) > current_hour - 86400:
            visitors_24h.update(ips)

    def top(kind, n):
        counts = Counter()
        for hour, data in (app_traffic or {}).get(kind, {}).items():
            if int(hour) > current_hour - 86400:
                counts.update(data)
        return [[k, v] for k, v in counts.most_common(n)]

    last5 = sum(minute_req[-6:-1]) / 5.0
    req_15m = sum_range(now - 900, now + 60, "n")
    e5_15m = sum_range(now - 900, now + 60, "e5")
    return {
        "available": True,
        "error": error,
        "has_latency": any(sum(b["h"]) for b in minutes.values()),
        "rpm": round(last5, 1),
        "req_1h": sum_range(hour_ago, now + 60, "n"),
        "req_24h": sum_range(day_ago, now + 60, "n"),
        "pages_24h": sum_range(day_ago, now + 60, "p"),
        "bots_24h": sum_range(day_ago, now + 60, "b"),
        "err4_1h": sum_range(hour_ago, now + 60, "e4"),
        "err5_1h": sum_range(hour_ago, now + 60, "e5"),
        "err5_24h": sum_range(day_ago, now + 60, "e5"),
        "err5_ratio_15m": (e5_15m / float(req_15m)) if req_15m else 0.0,
        "req_15m": req_15m,
        "p50_ms_1h": hist_quantile(hist_1h, 0.5),
        "p95_ms_1h": hist_quantile(hist_1h, 0.95),
        "visitors_1h": len(visitors.get(str(current_hour), [])),
        "visitors_24h": len(visitors_24h),
        "minute": {"start": minute_start, "step": 60, "req": minute_req, "err5": minute_e5},
        "history": {"start": hist_start, "step": HISTORY_STEP,
                    "req": h_req, "pages": h_pages, "err5": h_e5, "p95": h_p95},
        "top_pages": top("tp", 8),
        "top_errors": top("te", 5),
    }


# ------------------------------------------------------------------ discovery

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# Fields an app may declare about itself (registry file / metrics "monitor").
APP_FIELDS = ("name", "url", "hosts", "port", "socket", "screen", "health_path", "order", "slow_ms",
              "enabled", "metrics_path")


def registry_dir(config):
    return expand(config.get("registry_dir", "~/.wallmon/apps.d"))


def read_registry(config):
    """Apps that registered themselves (monitor_metrics gem >= 0.2). Returns
    (apps_by_id, problems)."""
    apps, problems = {}, []
    directory = registry_dir(config)
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return apps, problems
    for name in names:
        entry = load_json(os.path.join(directory, name), None)
        problem = validate_entry(entry)
        if problem:
            problems.append("%s: %s" % (name, problem))
            continue
        app = {k: entry[k] for k in APP_FIELDS if entry.get(k) is not None}
        app["id"] = entry["id"]
        app["source"] = "registry"
        if entry.get("root"):
            app["project"] = os.path.basename(entry["root"].rstrip("/"))
        if entry.get("port_source"):
            app["port_source"] = entry["port_source"]
        if isinstance(entry.get("listeners"), dict):
            app["listeners"] = entry["listeners"]
        apps[entry["id"]] = app
    return apps, problems


def validate_entry(entry):
    if not isinstance(entry, dict):
        return "not a JSON object"
    if not ID_RE.match(str(entry.get("id", ""))):
        return "invalid id %r" % entry.get("id")
    if entry.get("url") and not re.match(r"^https?://[^/\s]+", str(entry["url"])):
        return "invalid url %r" % entry["url"]
    port = entry.get("port")
    if port is not None and not (isinstance(port, int) and 0 < port < 65536):
        return "invalid port %r" % port
    sock = entry.get("socket")
    if sock is not None and not str(sock).startswith("/"):
        return "invalid socket %r" % sock
    return None


def discover_apps(config):
    """apps.json entries (sites without the gem, or server-side overrides such
    as access_log) merged with self-registered apps. What an app says about
    itself wins; apps.json fills the gaps. Returns (apps, problems)."""
    registered, problems = read_registry(config)
    merged = {}
    for entry in config.get("apps", []) or []:
        if not ID_RE.match(str(entry.get("id", ""))) or not entry.get("url"):
            problems.append("apps.json: entry needs a valid id and url: %r" % entry.get("id"))
            continue
        merged[entry["id"]] = dict(entry, source="config")
    for app_id, app in registered.items():
        base = merged.get(app_id, {})
        merged[app_id] = dict(base, **app)
        if base:
            merged[app_id]["source"] = "registry+config"
    apps = []
    for app in merged.values():
        if app.get("enabled") is False:
            continue
        if not app.get("url"):
            problems.append("%s: no url (set a.url in the initializer)" % app["id"])
            continue
        app.setdefault("access_log", config.get("access_log"))
        apps.append(app)
    apps.sort(key=lambda a: (a.get("order", 100), a.get("name", a["id"]).lower()))
    return apps, problems


def apply_self_description(app, metrics):
    """The live endpoint is the freshest description of the app."""
    monitor = metrics.get("monitor") if metrics.get("status") == "ok" else None
    if not isinstance(monitor, dict) or monitor.get("id") not in (None, app["id"]):
        return app
    updated = dict(app)
    for key in APP_FIELDS:
        if monitor.get(key) is not None:
            updated[key] = monitor[key]
    return updated


# ------------------------------------------------------------------- app checks

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_get(url, headers=None, timeout=10, follow=True):
    req = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT}, **(headers or {})))
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    started = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(2 * 1024 * 1024)
            return resp.status, body, (time.time() - started) * 1000, None
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, b"", (time.time() - started) * 1000, None
    except Exception as e:  # noqa: BLE001 - any network failure is a result
        return None, b"", (time.time() - started) * 1000, short_error(e)


def check_public(app):
    url = app["url"].rstrip("/") + app.get("health_path", "/")
    code, _body, ms, error = http_get(url, timeout=app.get("timeout", 10))
    return {"url": url, "code": code, "ms": round(ms), "error": error}


def cert_days(url, cache, now):
    m = re.match(r"https://([^/:]+)", url)
    if not m:
        return None
    host = m.group(1)
    entry = cache.get(host)
    if entry and now - entry["checked"] < 6 * 3600:
        return entry["days"]
    days = None
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                not_after = tls.getpeercert()["notAfter"]
        days = int((ssl.cert_time_to_seconds(not_after) - now) // 86400)
    except Exception:  # noqa: BLE001
        days = None
    cache[host] = {"checked": now, "days": days}
    return days


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a Unix socket, the way nginx reaches apps bound to
    unix:///tmp/<app>.socket."""

    def __init__(self, path, timeout=10):
        http.client.HTTPConnection.__init__(self, "localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def addresses(app):
    """Where the app can be reached locally, best first: its declared socket
    and port, then anything else it reported listening on."""
    found = []
    listeners = app.get("listeners") or {}
    for path in [app.get("socket")] + list(listeners.get("unix") or []):
        if path and ("unix", path) not in found:
            found.append(("unix", path))
    for port in [app.get("port")] + list(listeners.get("tcp") or []):
        if port and ("tcp", int(port)) not in found:
            found.append(("tcp", int(port)))
    return found


def describe(address):
    kind, where = address
    return where if kind == "unix" else "port %s" % where


def local_get(address, path, headers, timeout):
    """GET over a Unix socket or 127.0.0.1:port. Returns (code, body, ms, error)."""
    kind, where = address
    started = time.time()
    conn = UnixHTTPConnection(where, timeout=timeout) if kind == "unix" else \
        http.client.HTTPConnection("127.0.0.1", int(where), timeout=timeout)
    try:
        conn.request("GET", path, headers=dict({"User-Agent": USER_AGENT, "Host": "localhost"}, **headers))
        resp = conn.getresponse()
        body = resp.read(2 * 1024 * 1024)
        return resp.status, body, (time.time() - started) * 1000, None
    except Exception as e:  # noqa: BLE001 - any failure is a result
        return None, b"", (time.time() - started) * 1000, short_error(e)
    finally:
        conn.close()


def is_listening(address):
    kind, where = address
    try:
        if kind == "unix":
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2)
            try:
                sock.connect(where)
            finally:
                sock.close()
        else:
            with socket.create_connection(("127.0.0.1", int(where)), timeout=2):
                pass
        return True
    except OSError:
        return False


def screen_sessions():
    try:
        out = subprocess.run(["screen", "-ls"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=5, universal_newlines=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return set(re.findall(r"^\s*\d+\.(\S+)\s", out, re.M))


def fetch_app_metrics(app, token):
    """Asks each local address of the app in turn; the first one that answers
    *as this app* wins. Another app answering (two apps reporting the same
    port) is rejected instead of showing its numbers under the wrong name."""
    candidates = addresses(app)
    if not candidates:
        return {"status": "unconfigured",
                "error": "address unknown: set a.port or a.socket in the initializer, "
                         "or `port`/`socket` in apps.json"}
    if not token:
        return {"status": "error", "error": "no token file on the server"}
    path = app.get("metrics_path", "/internal/metrics")
    timeout = app.get("metrics_timeout", 20)
    failures, saw_404 = [], False
    for address in candidates:
        code, body, ms, error = local_get(address, path, {"X-Monitor-Token": token}, timeout)
        where = describe(address)
        if error:
            failures.append("%s: %s" % (where, error))
            continue
        if code == 404:
            saw_404 = True
            continue
        if code != 200:
            failures.append("%s: HTTP %s" % (where, code))
            continue
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError as e:
            failures.append("%s: %s" % (where, short_error(e)))
            continue
        other = (data.get("monitor") or {}).get("id")
        if other and other != app["id"]:
            failures.append("%s answers as app '%s'" % (where, other))
            continue
        data["status"] = "ok"
        data["ms"] = round(ms)
        data["via"] = where
        return data
    if saw_404 and not failures:
        return {"status": "not_installed"}
    return {"status": "error", "error": "; ".join(failures)[:300] or "no answer"}


def decide_state(app, result, config):
    reasons = []
    public = result["public"]
    down = False
    if public["error"]:
        down = True
        reasons.append("unreachable: " + public["error"])
    elif public["code"] is not None and public["code"] >= 500:
        down = True
        reasons.append("HTTP %s from %s" % (public["code"], app["url"]))
    elif public["code"] is not None and public["code"] >= 400:
        reasons.append("HTTP %s from %s" % (public["code"], app["url"]))
    if result.get("port_open") is False:
        down = True
        reasons.append("nothing listening on %s" % result.get("address", "its local address"))
    if result.get("screen") is False:
        reasons.append("screen session '%s' missing" % app.get("screen"))
    metric_list = [m for m in (result["metrics"].get("metrics") or []) if isinstance(m, dict)]
    for m in metric_list:
        if m.get("level") == "critical":
            down = True
            reasons.append(threshold_reason(m, "critical"))
    if down:
        return "down", reasons

    slow_ms = app.get("slow_ms", config.get("slow_ms", 2000))
    if public["ms"] and public["ms"] > slow_ms:
        reasons.append("slow: %d ms" % public["ms"])
    t = result["traffic"]
    if t.get("available") and t.get("req_15m", 0) >= 5 and t.get("err5_ratio_15m", 0) > 0.05:
        reasons.append("%d%% 5xx in last 15 min" % round(t["err5_ratio_15m"] * 100))
    for db in result["metrics"].get("databases", []) or []:
        if not db.get("ok"):
            reasons.append("database %s: %s" % (db.get("name"), (db.get("error") or "")[:80]))
    if result["metrics"].get("status") == "error":
        reasons.append("metrics endpoint: " + result["metrics"].get("error", "?"))
    for m in metric_list:
        if m.get("level") == "warning":
            reasons.append(threshold_reason(m, "warn"))
    days = result.get("cert_days")
    if days is not None and days < config.get("cert_warn_days", 14):
        reasons.append("TLS certificate expires in %d days" % days)
    return ("degraded" if reasons else "up"), reasons


def threshold_reason(metric, prefix):
    value = metric.get("value")
    limits = metric.get("thresholds") or {}
    shown = ("%g" % value) if isinstance(value, (int, float)) else str(value)
    unit = (" " + metric["unit"]) if metric.get("unit") else ""
    for side, op in (("above", ">"), ("below", "<")):
        limit = limits.get("%s_%s" % (prefix, side))
        if limit is not None and isinstance(value, (int, float)) and \
                ((side == "above" and value > limit) or (side == "below" and value < limit)):
            return "%s: %s%s %s %g" % (metric.get("label", metric.get("key")), shown, unit, op, limit)
    return "%s: %s%s" % (metric.get("label", metric.get("key")), shown, unit)


# ------------------------------------------------------------------------ host

def read_cpu_times():
    with open("/proc/stat") as f:
        fields = [int(x) for x in f.readline().split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def host_snapshot(config, state, now):
    host = {"hostname": socket.gethostname()}
    try:
        total, idle = read_cpu_times()
        prev = state.get("cpu_prev")
        if prev and total > prev[0]:
            host["cpu_pct"] = round(100.0 * (1 - (idle - prev[1]) / float(total - prev[0])), 1)
        state["cpu_prev"] = [total, idle]
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/proc/loadavg") as f:
            host["load"] = [float(x) for x in f.read().split()[:3]]
        host["cpus"] = os.cpu_count()
        with open("/proc/uptime") as f:
            host["uptime_s"] = int(float(f.read().split()[0]))
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                mem[key] = int(value.split()[0])
        total_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", 0)
        host["mem"] = {"total_mb": total_kb // 1024,
                       "used_pct": round(100.0 * (total_kb - avail_kb) / total_kb, 1) if total_kb else None}
        swap_total = mem.get("SwapTotal", 0)
        host["swap_used_pct"] = round(100.0 * (swap_total - mem.get("SwapFree", 0)) / swap_total, 1) if swap_total else 0
    except (OSError, ValueError):
        pass

    disks = []
    for path in config.get("disk_paths", ["/"]):
        try:
            usage = shutil.disk_usage(path)
            disks.append({"path": path, "used_pct": round(100.0 * usage.used / usage.total, 1),
                          "free_gb": round(usage.free / 1024.0 ** 3, 1)})
        except OSError as e:
            disks.append({"path": path, "error": short_error(e)})
    host["disks"] = disks
    host["docker"] = docker_status(config)

    samples = state.setdefault("host_samples", {})
    samples[str(now // 60 * 60)] = [host.get("cpu_pct"), (host.get("mem") or {}).get("used_pct"),
                                     (host.get("load") or [None])[0]]
    cutoff = now - 86400
    for key in [k for k in samples if int(k) < cutoff]:
        del samples[key]
    host["history"] = host_history(samples, now)
    return host


def host_history(samples, now):
    end = now // HISTORY_STEP * HISTORY_STEP + HISTORY_STEP
    start = end - 86400
    cpu, mem = [], []
    for s in range(start, end, HISTORY_STEP):
        values = [samples[str(t)] for t in range(s, s + HISTORY_STEP, 60) if str(t) in samples]
        cpu_vals = [v[0] for v in values if v[0] is not None]
        mem_vals = [v[1] for v in values if v[1] is not None]
        cpu.append(round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None)
        mem.append(round(sum(mem_vals) / len(mem_vals), 1) if mem_vals else None)
    return {"start": start, "step": HISTORY_STEP, "cpu": cpu, "mem": mem}


def docker_status(config):
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}|{{.Image}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, universal_newlines=True)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": short_error(e), "containers": []}
    if out.returncode != 0:
        return {"ok": False, "error": out.stderr.strip()[:200], "containers": []}
    containers = []
    for line in out.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            containers.append({"name": parts[0], "status": parts[1], "image": parts[2],
                               "running": parts[1].startswith("Up")})
    # Old exited containers pile up on a dev-ish host; only the watched ones
    # (the app databases) may raise an alarm. Without a watch list, show what runs.
    watch = config.get("docker_watch") or []
    if watch:
        by_name = {c["name"]: c for c in containers}
        containers = [by_name.get(n, {"name": n, "status": "missing", "image": None, "running": False})
                      for n in watch]
    else:
        containers = [c for c in containers if c["running"]]
    return {"ok": True, "error": None, "containers": containers}


# ------------------------------------------------------------------------ main

def read_token(config):
    path = expand(config.get("token_file", "~/.monitor_metrics_token"))
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def record_transitions(state, results, now):
    states = state.setdefault("app_states", {})
    events = state.setdefault("events", [])
    for r in results:
        prev = states.get(r["id"])
        if prev is None or prev["state"] != r["state"]:
            if prev is not None:
                events.append({"ts": now, "app": r["id"], "name": r["name"],
                               "from": prev["state"], "to": r["state"], "reasons": r["reasons"][:3]})
            states[r["id"]] = {"state": r["state"], "since": now}
        r["since"] = states[r["id"]]["since"]
    del events[:-50]


def collect(config, state, now=None):
    now = int(now or time.time())
    started = time.time()
    hours = config.get("history_hours", 24)
    apps, problems = discover_apps(config)
    log_errors = ingest_logs(config, apps, state, now)
    prune_traffic(state, now, hours)
    token = read_token(config)
    sessions = screen_sessions() if any(a.get("screen") for a in apps) else None
    cert_cache = state.setdefault("certs", {})

    results = []
    for app in apps:
        metrics = fetch_app_metrics(app, token)
        app = apply_self_description(app, metrics)
        if app.get("enabled") is False:
            continue
        r = {"id": app["id"], "name": app.get("name", app["id"]), "url": app["url"],
             "project": app.get("project"), "order": app.get("order", 100),
             "source": app.get("source"), "port": app.get("port"), "socket": app.get("socket"),
             "port_source": app.get("port_source")}
        r["public"] = check_public(app)
        r["cert_days"] = cert_days(app["url"], cert_cache, now)
        local = addresses(app)
        r["address"] = describe(local[0]) if local else None
        r["port_open"] = any(is_listening(a) for a in local) if local else None
        r["screen"] = (app["screen"] in sessions) if (app.get("screen") and sessions is not None) else None
        r["traffic"] = summarize_traffic(state.get("traffic", {}).get(app["id"]), now,
                                         log_errors.get(app["id"]) if app.get("access_log") else "no access_log configured")
        r["metrics"] = metrics
        r["state"], r["reasons"] = decide_state(app, r, config)
        results.append(r)

    record_transitions(state, results, now)
    return {
        "schema": SCHEMA,
        "generated_at": now,
        "generated_iso": datetime.fromtimestamp(now).astimezone().isoformat(),
        "interval_s": config.get("interval_s", 60),
        "collector_ms": round((time.time() - started) * 1000),
        "host": host_snapshot(config, state, now),
        "apps": results,
        "problems": problems,
        "events": list(reversed(state.get("events", [])))[:20],
    }


def forget(config, state, state_path, app_id):
    removed = []
    path = os.path.join(registry_dir(config), app_id + ".json")
    if os.path.exists(path):
        os.remove(path)
        removed.append(path)
    for key in ("traffic", "app_states"):
        if state.get(key, {}).pop(app_id, None) is not None:
            removed.append("state." + key)
    if any(a.get("id") == app_id for a in config.get("apps", []) or []):
        print("note: %s is also listed in apps.json; remove it there too" % app_id)
    write_json_atomic(state_path, state, mode=0o600)
    print("forgot %s: %s" % (app_id, ", ".join(removed) or "nothing found"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps.json"))
    parser.add_argument("--stdout", action="store_true", help="print status.json instead of writing it")
    parser.add_argument("--forget", metavar="APP_ID",
                        help="remove an app that no longer exists: its registry file and its history")
    parser.add_argument("--list", action="store_true", help="show the apps the collector would monitor")
    args = parser.parse_args(argv)

    config = load_json(args.config, None)
    if not config:
        print("cannot read config %s" % args.config, file=sys.stderr)
        return 2
    state_path = expand(config.get("state", "~/.local/state/wallmon/state.json"))
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    with open(state_path + ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another collector run is still in progress", file=sys.stderr)
            return 1
        state = load_json(state_path, {})
        if args.list:
            apps, problems = discover_apps(config)
            for app in apps:
                local = addresses(app)
                where = describe(local[0]) if local else "ADDRESS UNKNOWN"
                print("%-14s %-24s %-28s %-16s %s" % (app["id"], app.get("name", "")[:24], where,
                                                      app.get("source"), app["url"]))
            for problem in problems:
                print("problem: " + problem, file=sys.stderr)
            return 0
        if args.forget:
            return forget(config, state, state_path, args.forget)
        status = collect(config, state)
        write_json_atomic(state_path, state, mode=0o600)
        if args.stdout:
            json.dump(status, sys.stdout, indent=2)
            print()
        else:
            write_json_atomic(expand(config["output"]), status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
