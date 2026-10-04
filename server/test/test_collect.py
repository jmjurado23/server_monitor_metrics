import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import collect  # noqa: E402

TOKEN = "tkn"


def log_line(ts, path="/recipes", status=200, host=None, rt=None, ip="1.2.3.4", ua="Mozilla/5.0"):
    t = time.strftime("%d/%b/%Y:%H:%M:%S +0000", time.gmtime(ts))
    line = '%s - - [%s] "GET %s HTTP/1.1" %d 512 "-" "%s"' % (ip, t, path, status, ua)
    if rt is not None:
        line += " rt=%.3f" % rt
    if host:
        line += " host=%s" % host
    return line + "\n"


class FakeHandler(http.server.BaseHTTPRequestHandler):
    EXTRA = {}  # merged into the metrics response by tests

    def do_GET(self):  # noqa: N802
        if self.path == "/internal/metrics":
            if self.headers.get("X-Monitor-Token") != TOKEN:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"schema": 1, "app": {"name": "Fake", "rss_mb": 200},
                               "databases": [{"name": "mongodb", "ok": True, "ms": 2}],
                               "metrics": [{"key": "x", "label": "X", "type": "number", "value": 3}],
                               **FakeHandler.EXTRA}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/broken":
            self.send_response(502)
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class ParseTest(unittest.TestCase):
    def test_combined_line(self):
        rec = collect.parse_line(log_line(1_790_000_000, "/a?b=1", 404))
        self.assertEqual(rec["ts"], 1_790_000_000)
        self.assertEqual(rec["path"], "/a")
        self.assertEqual(rec["status"], 404)
        self.assertIsNone(rec["rt_ms"])
        self.assertIsNone(rec["host"])

    def test_extended_line(self):
        rec = collect.parse_line(log_line(1_790_000_000, rt=0.25, host="Cocina-Tradicional.es"))
        self.assertAlmostEqual(rec["rt_ms"], 250.0)
        self.assertEqual(rec["host"], "cocina-tradicional.es")

    def test_timezone_offset(self):
        line = '1.1.1.1 - - [03/Oct/2026:20:00:00 +0200] "GET / HTTP/1.1" 200 1 "-" "x"'
        self.assertEqual(collect.parse_line(line)["ts"], collect.parse_line(
            '1.1.1.1 - - [03/Oct/2026:18:00:00 +0000] "GET / HTTP/1.1" 200 1 "-" "x"')["ts"])

    def test_garbage(self):
        self.assertIsNone(collect.parse_line("not a log line"))

    def test_quantile(self):
        hist = [0] * (len(collect.LAT_BOUNDS) + 1)
        hist[collect.lat_bucket(40)] = 90
        hist[collect.lat_bucket(1200)] = 10
        self.assertEqual(collect.hist_quantile(hist, 0.5), 50)
        self.assertEqual(collect.hist_quantile(hist, 0.95), 1500)


class LogReaderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "access.log")

    def tearDown(self):
        self.dir.cleanup()

    def test_partial_lines_and_rotation(self):
        with open(self.path, "w") as f:
            f.write("one\ntwo\nthr")
        lines, saved, err = collect.read_new_lines(self.path, None)
        self.assertIsNone(err)
        self.assertEqual(lines, ["one", "two"])
        with open(self.path, "a") as f:
            f.write("ee\nfour\n")
        lines, saved, _ = collect.read_new_lines(self.path, saved)
        self.assertEqual(lines, ["three", "four"])
        # logrotate: move to .1 after one more line, start a fresh file
        with open(self.path, "a") as f:
            f.write("five\n")
        os.rename(self.path, self.path + ".1")
        with open(self.path, "w") as f:
            f.write("six\n")
        lines, saved, _ = collect.read_new_lines(self.path, saved)
        self.assertEqual(lines, ["five", "six"])

    def test_missing_file_reports_error(self):
        lines, saved, err = collect.read_new_lines(self.path, None)
        self.assertEqual(lines, [])
        self.assertIn("FileNotFoundError", err)


class CollectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.log = os.path.join(d, "access.log")
        with open(os.path.join(d, "token"), "w") as f:
            f.write(TOKEN + "\n")
        base = "http://127.0.0.1:%d" % self.port
        self.config = {
            "output": os.path.join(d, "out", "status.json"),
            "token_file": os.path.join(d, "token"),
            "disk_paths": [d],
            "registry_dir": os.path.join(d, "apps.d"),
            "apps": [
                {"id": "a", "name": "App A", "url": base, "port": self.port, "access_log": self.log,
                 "hosts": ["a.test"]},
                {"id": "b", "name": "App B", "url": base, "health_path": "/broken", "port": self.port,
                 "access_log": self.log, "hosts": ["b.test"]},
                {"id": "c", "name": "App C", "url": "http://127.0.0.1:1", "port": 1},
            ],
        }

    def tearDown(self):
        self.dir.cleanup()

    def test_full_run(self):
        now = int(time.time())
        with open(self.log, "w") as f:
            for i in range(30):
                f.write(log_line(now - 120 + i, "/recipes", 200, host="a.test", rt=0.08, ip="10.0.0.%d" % (i % 3)))
            f.write(log_line(now - 60, "/assets/app.css", 200, host="a.test", rt=0.01))
            f.write(log_line(now - 60, "/boom", 500, host="a.test", rt=1.2))
            f.write(log_line(now - 60, "/", 200, host="a.test", ua="Googlebot/2.1"))
            f.write(log_line(now - 60, "/internal/metrics", 200, host="a.test"))
            f.write(log_line(now - 60, "/x", 200, host="b.test"))
            f.write(log_line(now - 90000, "/old", 200, host="a.test"))

        state = {}
        status = collect.collect(self.config, state, now)
        apps = {a["id"]: a for a in status["apps"]}

        a = apps["a"]
        self.assertEqual(a["state"], "up", a["reasons"])
        t = a["traffic"]
        self.assertEqual(t["req_24h"], 33)          # 30 + css + 500 + bot; not /internal, not old
        self.assertEqual(t["pages_24h"], 31)        # css and bot excluded
        self.assertEqual(t["bots_24h"], 1)
        self.assertEqual(t["err5_1h"], 1)
        self.assertEqual(t["visitors_24h"], 4)      # 3 IPs + the 1.2.3.4 default IP
        self.assertEqual(t["top_pages"][0], ["/recipes", 30])
        self.assertEqual(t["top_errors"], [["500 /boom", 1]])
        self.assertEqual(t["p50_ms_1h"], 100)
        self.assertEqual(len(t["history"]["req"]), 144)
        self.assertEqual(sum(t["history"]["req"]), 33)
        self.assertEqual(a["metrics"]["status"], "ok")
        self.assertEqual(a["metrics"]["metrics"][0]["value"], 3)

        self.assertEqual(apps["b"]["state"], "down")
        self.assertEqual(apps["b"]["traffic"]["req_24h"], 1)
        self.assertEqual(apps["c"]["state"], "down")
        self.assertIn("nothing listening on port 1", apps["c"]["reasons"])
        self.assertFalse(apps["c"]["traffic"]["available"])

        # second run: no new lines must not double count; transitions recorded
        with open(self.log, "a") as f:
            f.write(log_line(now + 10, "/recipes", 200, host="a.test"))
        self.config["apps"][1]["health_path"] = "/"
        status = collect.collect(self.config, state, now + 60)
        apps = {a["id"]: a for a in status["apps"]}
        self.assertEqual(apps["a"]["traffic"]["req_24h"], 34)
        self.assertEqual(apps["b"]["state"], "up")
        self.assertEqual(status["events"][0]["app"], "b")
        self.assertEqual(status["events"][0]["to"], "up")
        json.dumps(status)  # serializable

    def test_wrong_token_means_not_installed(self):
        with open(self.config["token_file"], "w") as f:
            f.write("wrong")
        status = collect.collect(self.config, {}, int(time.time()))
        self.assertEqual(status["apps"][0]["metrics"]["status"], "not_installed")

    def test_main_writes_output(self):
        cfg_path = os.path.join(self.dir.name, "apps.json")
        self.config["state"] = os.path.join(self.dir.name, "state", "state.json")
        with open(cfg_path, "w") as f:
            json.dump(self.config, f)
        self.assertEqual(collect.main(["-c", cfg_path]), 0)
        with open(self.config["output"]) as f:
            self.assertEqual(json.load(f)["schema"], 1)
        self.assertEqual(oct(os.stat(self.config["state"]).st_mode & 0o777), "0o600")


def register(directory, **entry):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, entry["id"] + ".json"), "w") as f:
        json.dump(dict({"schema": 2}, **entry), f)


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.reg = os.path.join(self.dir.name, "apps.d")
        self.config = {"registry_dir": self.reg, "access_log": "/var/log/nginx/access.log"}

    def tearDown(self):
        self.dir.cleanup()

    def test_registry_apps_sorted_by_order(self):
        register(self.reg, id="radio", name="I Love Radio", url="https://iloveradio.es", port=3003, order=3,
                 root="/home/deploy/Projects/iloveradio", port_source="puma")
        register(self.reg, id="cocina", name="Cocina", url="https://cocina-tradicional.es", port=3002, order=1)
        apps, problems = collect.discover_apps(self.config)
        self.assertEqual([], problems)
        self.assertEqual(["cocina", "radio"], [a["id"] for a in apps])
        radio = apps[1]
        self.assertEqual("iloveradio", radio["project"])
        self.assertEqual("puma", radio["port_source"])
        self.assertEqual("registry", radio["source"])
        self.assertEqual("/var/log/nginx/access.log", radio["access_log"])   # global default

    def test_apps_json_fills_gaps_and_registry_wins(self):
        register(self.reg, id="cocina", name="Cocina Tradicional", url="https://cocina-tradicional.es", port=3002)
        self.config["apps"] = [
            {"id": "cocina", "name": "Old name", "url": "https://old.example", "access_log": "/var/log/nginx/cocina.log"},
            {"id": "static", "name": "Static", "url": "https://example.org"},
        ]
        apps = {a["id"]: a for a in collect.discover_apps(self.config)[0]}
        self.assertEqual("Cocina Tradicional", apps["cocina"]["name"])
        self.assertEqual("https://cocina-tradicional.es", apps["cocina"]["url"])
        self.assertEqual("/var/log/nginx/cocina.log", apps["cocina"]["access_log"])
        self.assertEqual("registry+config", apps["cocina"]["source"])
        self.assertEqual("config", apps["static"]["source"])

    def test_disabled_invalid_and_urlless_entries(self):
        register(self.reg, id="off", url="https://off.example", enabled=False)
        register(self.reg, id="nourl", port=3000)
        register(self.reg, id="BAD ID", url="https://x.example")
        with open(os.path.join(self.reg, "broken.json"), "w") as f:
            f.write("{not json")
        apps, problems = collect.discover_apps(self.config)
        self.assertEqual([], apps)
        self.assertEqual(3, len(problems), problems)
        self.assertTrue(any("nourl" in p for p in problems))

    def test_missing_registry_dir_is_fine(self):
        self.config["registry_dir"] = os.path.join(self.dir.name, "nope")
        self.assertEqual(([], []), collect.discover_apps(self.config))

    def test_live_description_overrides(self):
        app = {"id": "cocina", "name": "Old", "url": "https://cocina-tradicional.es", "order": 9}
        metrics = {"status": "ok", "monitor": {"id": "cocina", "name": "New", "order": 1, "port": None}}
        updated = collect.apply_self_description(app, metrics)
        self.assertEqual(("New", 1), (updated["name"], updated["order"]))
        other = {"status": "ok", "monitor": {"id": "someone-else", "name": "X"}}
        self.assertEqual("Old", collect.apply_self_description(app, other)["name"])

    def test_threshold_reasons(self):
        m = {"label": "Failed imports", "value": 14, "thresholds": {"warn_above": 0, "critical_above": 10}}
        self.assertEqual("Failed imports: 14 > 10", collect.threshold_reason(m, "critical"))
        m = {"label": "Stock", "value": 2, "unit": "u", "thresholds": {"warn_below": 5}}
        self.assertEqual("Stock: 2 u < 5", collect.threshold_reason(m, "warn"))


class RegisteredRunTest(unittest.TestCase):
    """End to end: an app known only through its registry file."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        FakeHandler.EXTRA = {}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        with open(os.path.join(d, "token"), "w") as f:
            f.write(TOKEN)
        self.state_path = os.path.join(d, "state.json")
        self.config = {"token_file": os.path.join(d, "token"), "disk_paths": [d],
                       "registry_dir": os.path.join(d, "apps.d"), "state": self.state_path,
                       "output": os.path.join(d, "status.json")}
        register(self.config["registry_dir"], id="fake", name="Fake App",
                 url="http://127.0.0.1:%d" % self.port, port=self.port, order=2)

    def tearDown(self):
        self.dir.cleanup()
        FakeHandler.EXTRA = {}

    def test_registered_app_is_monitored_and_thresholds_count(self):
        FakeHandler.EXTRA = {
            "monitor": {"id": "fake", "name": "Fake App (live)", "order": 2},
            "metrics": [{"key": "backlog", "label": "Backlog", "type": "number", "value": 70,
                         "level": "critical", "thresholds": {"warn_above": 10, "critical_above": 50}},
                        {"key": "slowq", "label": "Slow queries", "type": "number", "value": 4,
                         "level": "warning", "thresholds": {"warn_above": 3}}],
        }
        status = collect.collect(self.config, {}, int(time.time()))
        app = status["apps"][0]
        self.assertEqual("fake", app["id"])
        self.assertEqual("Fake App (live)", app["name"])
        self.assertEqual("registry", app["source"])
        self.assertEqual("down", app["state"])
        self.assertIn("Backlog: 70 > 50", app["reasons"])

        FakeHandler.EXTRA["metrics"][0]["level"] = "ok"
        app = collect.collect(self.config, {}, int(time.time()))["apps"][0]
        self.assertEqual("degraded", app["state"])
        self.assertIn("Slow queries: 4 > 3", app["reasons"])

    def test_registered_app_without_port(self):
        register(self.config["registry_dir"], id="fake", name="Fake App", url="http://127.0.0.1:%d" % self.port)
        app = collect.collect(self.config, {}, int(time.time()))["apps"][0]
        self.assertEqual("unconfigured", app["metrics"]["status"])
        self.assertIn("port unknown", app["metrics"]["error"])

    def test_forget_and_list(self):
        cfg_path = os.path.join(self.dir.name, "apps.json")
        with open(cfg_path, "w") as f:
            json.dump(self.config, f)
        self.assertEqual(0, collect.main(["-c", cfg_path]))
        with open(self.state_path) as f:
            self.assertIn("fake", json.load(f)["app_states"])
        self.assertEqual(0, collect.main(["-c", cfg_path, "--list"]))
        self.assertEqual(0, collect.main(["-c", cfg_path, "--forget", "fake"]))
        self.assertFalse(os.path.exists(os.path.join(self.config["registry_dir"], "fake.json")))
        with open(self.state_path) as f:
            self.assertNotIn("fake", json.load(f).get("app_states", {}))


if __name__ == "__main__":
    unittest.main()
