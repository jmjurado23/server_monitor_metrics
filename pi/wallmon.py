#!/usr/bin/env python3
"""Wall monitor display server for the Raspberry Pi.

Polls the server's status.json (HTTPS + basic auth) in a background thread and
serves the dashboard plus /api/status and /api/config to the local kiosk
browser. Standard library only.

  WALLMON_URL=https://your-domain.example/wallmon/status.json \
  WALLMON_USER=wallmon WALLMON_PASS=... python3 wallmon.py

  python3 wallmon.py --demo        # fake data, for trying the screens anywhere
"""

import argparse
import base64
import json
import math
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.realpath(os.path.join(HERE, "static"))
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Poller(object):
    def __init__(self, url, user, password, interval):
        self.url = url
        self.auth = None
        if user:
            token = base64.b64encode(("%s:%s" % (user, password or "")).encode()).decode()
            self.auth = "Basic " + token
        self.interval = interval
        self.lock = threading.Lock()
        self.payload = json.dumps({"data": None, "fetched_at": None, "error": "not fetched yet"}).encode()

    def run(self):
        last_good = None
        fetched_at = None
        while True:
            error = None
            try:
                req = urllib.request.Request(self.url, headers={"User-Agent": "wallmon-pi/1"})
                if self.auth:
                    req.add_header("Authorization", self.auth)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    last_good = json.loads(resp.read().decode("utf-8"))
                    fetched_at = int(time.time())
            except urllib.error.HTTPError as e:
                error = "server answered HTTP %s" % e.code
                e.close()
            except Exception as e:  # noqa: BLE001 - shown on screen
                error = "%s: %s" % (type(e).__name__, str(e)[:150])
            body = json.dumps({"data": last_good, "fetched_at": fetched_at, "error": error},
                              separators=(",", ":")).encode()
            with self.lock:
                self.payload = body
            time.sleep(self.interval)

    def get(self):
        with self.lock:
            return self.payload


class DemoPoller(object):
    """Synthetic status shaped exactly like collect.py output."""

    def get(self):
        return json.dumps({"data": demo_status(int(time.time())), "fetched_at": int(time.time()),
                           "error": None}).encode()


def make_handler(poller, config_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                return self.send(200, poller.get(), "application/json")
            if path == "/api/config":
                try:
                    with open(config_path, "rb") as f:
                        return self.send(200, f.read(), "application/json")
                except OSError:
                    return self.send(200, b"{}", "application/json")
            if path == "/" or path.startswith("/app/"):
                path = "/index.html"
            full = os.path.realpath(os.path.join(STATIC, path.lstrip("/")))
            if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
                return self.send(404, b"not found", "text/plain")
            with open(full, "rb") as f:
                self.send(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# --------------------------------------------------------------------- demo data

def wave(ts, base, amp, phase=0.0):
    hour = (time.localtime(ts).tm_hour + time.localtime(ts).tm_min / 60.0)
    return max(0.0, base + amp * math.sin((hour - 9 + phase) / 24.0 * 2 * math.pi))


def demo_traffic(now, base, amp, seed, latency):
    rnd = random.Random(seed + now // 600)
    step = 600
    end = now // step * step + step
    start = end - 86400
    req, pages, err5, p95 = [], [], [], []
    for ts in range(start, end, step):
        r = int(wave(ts, base, amp) * 10 * (0.85 + rnd.random() * 0.3))
        req.append(r)
        pages.append(int(r * 0.6))
        err5.append(1 if rnd.random() < 0.05 else 0)
        p95.append(int(latency * (0.8 + rnd.random() * 0.6)) if r else None)
    mstart = now // 60 * 60 - 59 * 60
    mreq = [int(wave(now, base, amp) * (0.6 + rnd.random() * 0.8)) for _ in range(60)]
    return {
        "available": True, "error": None, "has_latency": True,
        "rpm": round(sum(mreq[-6:-1]) / 5.0, 1), "req_1h": sum(mreq), "req_24h": sum(req),
        "pages_24h": sum(pages), "bots_24h": int(sum(req) * 0.2), "err4_1h": rnd.randint(0, 30),
        "err5_1h": sum(err5[-6:]), "err5_24h": sum(err5), "err5_ratio_15m": 0.0, "req_15m": sum(mreq[-15:]),
        "p50_ms_1h": int(latency * 0.4), "p95_ms_1h": latency,
        "visitors_1h": int(sum(mreq) * 0.15), "visitors_24h": int(sum(pages) * 0.12),
        "minute": {"start": mstart, "step": 60, "req": mreq, "err5": [0] * 57 + [1, 0, 0]},
        "history": {"start": start, "step": step, "req": req, "pages": pages, "err5": err5, "p95": p95},
        "top_pages": [["/", 812], ["/recetas/paella-valenciana", 344], ["/recetas/gazpacho", 201],
                      ["/categorias/postres", 150], ["/buscar", 97]],
        "top_errors": [["500 /api/v1/import", 3]] if sum(err5) else [],
    }


def demo_app_metrics(now, name, rails, metrics, db="postgresql"):
    return {"status": "ok", "ms": 84, "schema": 1, "gem_version": "0.1.0",
            "app": {"name": name, "env": "production", "rails": rails, "ruby": "3.0.3", "pid": 4242,
                    "booted_at": None, "uptime_s": 6 * 86400 + 3 * 3600, "rss_mb": 412.5, "threads": 9,
                    "revision": "a1b2c3d"},
            "databases": [{"name": db, "ok": True, "ms": 1.8, "error": None}],
            "metrics": metrics}


def series(now, every, last, fn):
    end = now // every * every
    return [[end - (last - 1 - i) * every, fn(i)] for i in range(last)]


def demo_status(now):
    rnd = random.Random(now // 60)

    def m(key, label, typ, value, unit=None):
        return {"key": key, "label": label, "type": typ, "unit": unit, "value": value,
                "error": None, "stale": False, "ms": 12.0, "computed_at": None}

    apps = [
        {"id": "cocina", "name": "Cocina Tradicional", "url": "https://cocina-tradicional.es",
         "project": "cooking_rails", "state": "up", "reasons": [],
         "public": {"code": 200, "ms": 270, "error": None}, "cert_days": 61, "port_open": True, "screen": True,
         "traffic": demo_traffic(now, 14, 10, 1, 320),
         "metrics": demo_app_metrics(now, "CookingRails", "5.2.4.3", [
             m("recipes", "Recipes", "number", 2318),
             m("users_today", "Sign-ups today", "number", rnd.randint(3, 12)),
             m("signups", "Sign-ups per day", "series",
               series(now, 86400, 14, lambda i: 5 + (i * 7) % 11)),
             m("top_recipes", "Most voted this week", "table", {
                 "columns": ["recipe", "votes"],
                 "rows": [["Paella valenciana", 41], ["Cocido madrileño", 33], ["Gazpacho", 28],
                          ["Fabada asturiana", 19]]}),
         ], db="mongodb")},
        {"id": "agroroute", "name": "AgroRoute", "url": "https://agroroute.es",
         "project": "optimal_path/backend", "state": "up", "reasons": [],
         "public": {"code": 200, "ms": 150, "error": None}, "cert_days": 44, "port_open": True, "screen": True,
         "traffic": demo_traffic(now, 6, 5, 2, 210),
         "metrics": demo_app_metrics(now, "OptimalPath", "7.0.10", [
             m("paths_today", "Routes computed today", "number", rnd.randint(40, 90)),
             m("vehicles", "Vehicles", "number", 128),
             m("paths_hour", "Routes per hour", "series",
               series(now, 3600, 24, lambda i: max(0, int(8 * math.sin(i / 3.8)) + 6))),
         ])},
        {"id": "iloveradio", "name": "I Love Radio", "url": "https://iloveradio.es",
         "project": "iloveradio", "state": "degraded", "reasons": ["slow: 2140 ms"],
         "public": {"code": 200, "ms": 2140, "error": None}, "cert_days": 9, "port_open": True, "screen": True,
         "traffic": demo_traffic(now, 22, 12, 3, 1450),
         "metrics": demo_app_metrics(now, "Iloveradio", "7.0.1", [
             m("played_today", "Songs played today", "number", rnd.randint(900, 1300)),
             m("stations", "Stations", "number", 37),
             m("played_hour", "Songs played per hour", "series",
               series(now, 3600, 24, lambda i: 40 + int(20 * math.sin(i / 4.0)))),
             m("last_import", "Last import", "text", "today 06:00 - 1,204 songs"),
         ])},
        {"id": "makeyourapp", "name": "Make Your App", "url": "https://makeyourapp.es",
         "project": "make_your_app_rails", "state": "down",
         "reasons": ["HTTP 502 from https://makeyourapp.es", "nothing listening on port 3004"],
         "public": {"code": 502, "ms": 115, "error": None}, "cert_days": 77, "port_open": False, "screen": False,
         "since": now - 47 * 60,
         "traffic": demo_traffic(now, 3, 2, 4, 400),
         "metrics": {"status": "error", "error": "URLError: <urlopen error [Errno 111] Connection refused>"}},
    ]
    for app in apps:
        app.setdefault("since", now - 6 * 86400)
    hist_end = now // 600 * 600 + 600
    return {
        "schema": 1, "generated_at": now - 20, "interval_s": 60, "collector_ms": 912,
        "host": {"hostname": "servidor", "cpu_pct": round(18 + rnd.random() * 10, 1), "load": [0.62, 0.71, 0.66],
                 "cpus": 4, "uptime_s": 41 * 86400, "mem": {"total_mb": 15934, "used_pct": 71.4},
                 "swap_used_pct": 3.1, "disks": [{"path": "/", "used_pct": 83.2, "free_gb": 31.4}],
                 "docker": {"ok": True, "error": None, "containers": [
                     {"name": "postgres", "status": "Up 41 days", "image": "postgres:14", "running": True},
                     {"name": "mongo", "status": "Up 41 days", "image": "mongo:4.4", "running": True}]},
                 "history": {"start": hist_end - 86400, "step": 600,
                             "cpu": [round(15 + 10 * math.sin(i / 12.0) + rnd.random() * 5, 1) for i in range(144)],
                             "mem": [round(68 + 3 * math.sin(i / 30.0), 1) for i in range(144)]}},
        "apps": apps,
        "events": [
            {"ts": now - 47 * 60, "app": "makeyourapp", "name": "Make Your App", "from": "up", "to": "down",
             "reasons": ["HTTP 502 from https://makeyourapp.es"]},
            {"ts": now - 3 * 3600, "app": "iloveradio", "name": "I Love Radio", "from": "up", "to": "degraded",
             "reasons": ["slow: 2140 ms"]},
            {"ts": now - 26 * 3600, "app": "cocina", "name": "Cocina Tradicional", "from": "down", "to": "up",
             "reasons": []},
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Wall monitor display server")
    parser.add_argument("--demo", action="store_true", help="serve synthetic data")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WALLMON_PORT", "8080")))
    parser.add_argument("--bind", default=os.environ.get("WALLMON_BIND", "127.0.0.1"))
    parser.add_argument("--config", default=os.environ.get("WALLMON_DASHBOARD", os.path.join(HERE, "dashboard.json")))
    args = parser.parse_args(argv)

    if args.demo:
        poller = DemoPoller()
    else:
        url = os.environ.get("WALLMON_URL")
        if not url:
            print("WALLMON_URL is not set (or use --demo)", file=sys.stderr)
            return 2
        poller = Poller(url, os.environ.get("WALLMON_USER"), os.environ.get("WALLMON_PASS"),
                        int(os.environ.get("WALLMON_POLL", "30")))
        threading.Thread(target=poller.run, daemon=True).start()

    server = Server((args.bind, args.port), make_handler(poller, args.config))
    print("wallmon on http://%s:%d/%s" % (args.bind, args.port, " (demo)" if args.demo else ""))
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
