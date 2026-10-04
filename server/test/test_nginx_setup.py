import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import nginx_setup  # noqa: E402

NGINX_CONF = """user www-data;
http {
\t##
\t# Logging Settings
\t##

\taccess_log /var/log/nginx/access.log;
\terror_log /var/log/nginx/error.log;

\tinclude /etc/nginx/sites-enabled/*;
}
"""

SITE = """upstream shop-rails {
  server unix:///tmp/shop.socket;
}

server {
  listen 80;
  server_name shop.example www.shop.example;
  return 301 https://$server_name$request_uri;
}

server {
  listen 443 ssl http2;
  access_log /var/log/nginx/sites.access.log;
  server_name shop.example www.shop.example;
  location / {
    proxy_pass http://shop-rails;
  }
}

server {
  server_name radio.example www.radio.example;
  access_log /var/log/nginx/sites.access.log;
  location /api/ { proxy_pass http://radio; }
    listen 443 ssl; # managed by Certbot
}

server {
  listen 9001;
  access_log /var/log/nginx/other.log;
  server_name unrelated.example;
}
"""


class NginxSetupTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.conf = os.path.join(d, "nginx.conf")
        self.sites = os.path.join(d, "sites-enabled")
        os.makedirs(self.sites)
        with open(self.conf, "w") as f:
            f.write(NGINX_CONF)
        real_site = os.path.join(d, "sites-available-main")
        with open(real_site, "w") as f:
            f.write(SITE)
        os.symlink(real_site, os.path.join(self.sites, "main"))   # sites-enabled -> sites-available
        self.real_site = real_site
        self.config = {"registry_dir": os.path.join(d, "apps.d"), "output": "/var/www/wallmon/status.json",
                       "apps": [{"id": "shop", "url": "https://shop.example", "order": 1},
                                {"id": "radio", "url": "https://radio.example", "order": 2}]}

    def tearDown(self):
        self.dir.cleanup()

    def plan(self, **kw):
        return nginx_setup.build_plan(self.config, self.conf, self.sites, "/etc/nginx/wallmon.htpasswd", **kw)

    def apply(self, plan, test_cmd=("true",)):
        return nginx_setup.apply_plan(plan, self.dir.name, list(test_cmd), ["true"])

    def test_plan_changes(self):
        plan = self.plan()
        self.assertEqual([], plan["problems"])
        self.assertEqual({"shop": "/var/log/nginx/sites.access.log", "radio": "/var/log/nginx/sites.access.log"},
                         plan["logs"])
        self.assertEqual("https://shop.example/wallmon/status.json", plan["status_url"])
        conf = plan["edits"][os.path.realpath(self.conf)].new_text()
        self.assertIn("\tlog_format wallmon '$remote_addr", conf)
        self.assertIn("\taccess_log /var/log/nginx/access.log;", conf)    # no app relies on the default
        site = plan["edits"][os.path.realpath(self.real_site)].new_text()
        self.assertEqual(2, site.count("location ^~ /internal/ { return 404; }"))   # both HTTPS blocks
        self.assertEqual(1, site.count("location = /wallmon/status.json {"))
        self.assertEqual(2, site.count("access_log /var/log/nginx/sites.access.log wallmon;"))
        self.assertIn("access_log /var/log/nginx/other.log;", site)      # unrelated site untouched
        # the HTTP redirect block gets nothing
        redirect = site.split("server {")[1]
        self.assertNotIn("internal", redirect)

    def test_apply_writes_through_symlink_and_is_idempotent(self):
        out = io.StringIO()
        sys.stdout, saved = out, sys.stdout
        try:
            self.assertEqual(0, self.apply(self.plan()))
        finally:
            sys.stdout = saved
        self.assertTrue(os.path.islink(os.path.join(self.sites, "main")))
        with open(self.real_site) as f:
            self.assertIn("location = /wallmon/status.json", f.read())
        again = self.plan()
        self.assertFalse(any(e.changed() for e in again["edits"].values()))
        self.assertEqual("https://shop.example/wallmon/status.json", again["status_url"])
        self.assertTrue(any("already published" in n for n in again["notes"]))

    def test_failed_nginx_test_restores_files(self):
        out = io.StringIO()
        sys.stdout, sys.stderr, saved = out, out, (sys.stdout, sys.stderr)
        try:
            self.assertEqual(1, self.apply(self.plan(), test_cmd=("false",)))
        finally:
            sys.stdout, sys.stderr = saved
        with open(self.real_site) as f:
            self.assertEqual(SITE, f.read())
        with open(self.conf) as f:
            self.assertEqual(NGINX_CONF, f.read())

    def test_status_domain_choice_and_missing_block(self):
        self.assertEqual("https://radio.example/wallmon/status.json",
                         self.plan(status_domain="radio.example")["status_url"])
        self.config["apps"].append({"id": "ghost", "url": "https://ghost.example"})
        plan = self.plan()
        self.assertTrue(any("ghost" in p for p in plan["problems"]))

    def test_config_points_at_the_used_log(self):
        updated = nginx_setup.new_config({"access_log": "/var/log/nginx/access.log"},
                                         {"shop": "/var/log/nginx/sites.access.log",
                                          "radio": "/var/log/nginx/sites.access.log"})
        self.assertEqual("/var/log/nginx/sites.access.log", updated["access_log"])
        split = nginx_setup.new_config({"apps": [{"id": "shop", "url": "https://shop.example"}]},
                                       {"shop": "/var/log/a.log", "radio": "/var/log/b.log"})
        by_id = {e["id"]: e for e in split["apps"]}
        self.assertEqual("/var/log/a.log", by_id["shop"]["access_log"])
        self.assertEqual("https://shop.example", by_id["shop"]["url"])
        self.assertEqual("/var/log/b.log", by_id["radio"]["access_log"])

    def test_access_log_format_handling(self):
        self.assertEqual("  access_log /x.log wallmon;\n", nginx_setup.with_format("  access_log /x.log;\n", "wallmon"))
        self.assertEqual("  access_log /x.log wallmon;\n", nginx_setup.with_format("  access_log /x.log combined;\n", "wallmon"))
        self.assertEqual("  access_log /x.log wallmon buffer=32k;\n",
                         nginx_setup.with_format("  access_log /x.log buffer=32k;\n", "wallmon"))
        self.assertEqual("  access_log off;\n", nginx_setup.with_format("  access_log off;\n", "wallmon"))

    def test_main_dry_run(self):
        cfg = os.path.join(self.dir.name, "apps.json")
        with open(cfg, "w") as f:
            json.dump(self.config, f)
        out = io.StringIO()
        sys.stdout, saved = out, sys.stdout
        try:
            code = nginx_setup.main(["-c", cfg, "--nginx-conf", self.conf, "--sites-dir", self.sites])
        finally:
            sys.stdout = saved
        self.assertEqual(0, code)
        self.assertIn("+  location ^~ /internal/ { return 404; }", out.getvalue())
        self.assertIn("dry run", out.getvalue())
        with open(self.real_site) as f:
            self.assertEqual(SITE, f.read())


    def test_default_log_gets_the_format_only_when_an_app_uses_it(self):
        with open(self.real_site) as f:
            text = f.read()
        with open(self.real_site, "w") as f:   # radio block without its own access_log
            f.write(text.replace("  server_name radio.example www.radio.example;\n  access_log /var/log/nginx/sites.access.log;\n",
                                 "  server_name radio.example www.radio.example;\n"))
        plan = self.plan()
        self.assertEqual("/var/log/nginx/access.log", plan["logs"]["radio"])
        conf = plan["edits"][os.path.realpath(self.conf)].new_text()
        self.assertIn("\taccess_log /var/log/nginx/access.log wallmon;", conf)
        cfg = nginx_setup.new_config(self.config, plan["logs"])
        self.assertEqual({"shop": "/var/log/nginx/sites.access.log", "radio": "/var/log/nginx/access.log"},
                         {e["id"]: e["access_log"] for e in cfg["apps"]})


if __name__ == "__main__":
    unittest.main()
