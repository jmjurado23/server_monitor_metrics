#!/usr/bin/env python3
"""Prepares nginx for the wall monitor, for the apps the collector knows.

For every app (registered with monitor_metrics, or listed in apps.json) it
finds the nginx server blocks serving the app's domain and:

  * adds the `wallmon` log_format (combined + response time + host) and uses
    it on the access_log lines of those blocks, so traffic can be split per app;
  * adds `location ^~ /internal/ { return 404; }` to the HTTPS block, so the
    metrics endpoint is only reachable locally;
  * publishes the collector output at https://<one app>/wallmon/status.json
    behind basic auth (creating /etc/nginx/wallmon.htpasswd if needed);
  * points apps.json at the log file(s) those blocks write to.

Default is a dry run that prints the diff. With --apply (as root) it backs up
every file it changes, runs `nginx -t`, restores the backup if the test fails
and reloads nginx otherwise. Running it again changes nothing.

  python3 nginx_setup.py                 # show what would change
  sudo python3 nginx_setup.py --apply    # do it

Standard library only; keep it compatible with Python 3.6.
"""

import argparse
import difflib
import getpass
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402

FORMAT = "wallmon"
LOG_FORMAT = [
    "log_format wallmon '$remote_addr - $remote_user [$time_local] \"$request\" '",
    "                   '$status $body_bytes_sent \"$http_referer\" \"$http_user_agent\" '",
    "                   'rt=$request_time host=$host';",
]
INTERNAL = ["# wall monitor: the metrics endpoint is only for the local collector",
            "location ^~ /internal/ { return 404; }"]
STATUS_PATH = "/wallmon/status.json"


def status_location(alias, htpasswd):
    return ["# wall monitor: data for the Raspberry Pi",
            "location = %s {" % STATUS_PATH,
            "  alias %s;" % alias,
            "  auth_basic \"wallmon\";",
            "  auth_basic_user_file %s;" % htpasswd,
            "  default_type application/json;",
            "  add_header Cache-Control \"no-store\";",
            "  access_log off;",
            "}"]


# --------------------------------------------------------------------- parsing

def code_of(line):
    return line.split("#", 1)[0]


def find_blocks(lines, keyword, depth_wanted=0):
    """(start, end) line indexes of `keyword {` blocks opening at a depth."""
    blocks, depth, start = [], 0, None
    for i, line in enumerate(lines):
        code = code_of(line)
        if start is None and depth == depth_wanted and re.match(r"\s*%s\b[^;]*\{" % keyword, code):
            start, open_depth = i, depth
        depth += code.count("{") - code.count("}")
        if start is not None and depth == open_depth and "}" in code:
            blocks.append((start, i))
            start = None
    return blocks


def block_text(lines, block):
    return "".join(lines[block[0]:block[1] + 1])


def server_names(lines, block):
    names = set()
    for line in lines[block[0]:block[1] + 1]:
        m = re.match(r"\s*server_name\s+([^;]+);", code_of(line))
        if m:
            names.update(n.lower() for n in m.group(1).split())
    return names


def is_https(lines, block):
    return any(re.match(r"\s*listen\s+[^;]*\b(443|ssl)\b", code_of(l)) for l in lines[block[0]:block[1] + 1])


def indent_inside(lines, block):
    for line in lines[block[0] + 1:block[1]]:
        if line.strip():
            return re.match(r"\s*", line).group(0)
    return "  "


def access_log_lines(lines, block, direct_only=True):
    """Indexes of `access_log` lines directly in the block (not in its locations)."""
    found, depth = [], 0
    for i in range(block[0], block[1] + 1):
        code = code_of(lines[i])
        if (depth == 1 or not direct_only) and re.match(r"\s*access_log\s", code):
            found.append(i)
        depth += code.count("{") - code.count("}")
    return found


def parse_access_log(line):
    """('path', 'format' or None) for an access_log line; ('off', None) when off."""
    m = re.match(r"\s*access_log\s+([^;]+);", code_of(line))
    if not m:
        return None, None
    tokens = m.group(1).split()
    if tokens[0] == "off":
        return "off", None
    fmt = tokens[1] if len(tokens) > 1 and "=" not in tokens[1] else None
    return tokens[0], fmt


def with_format(line, fmt):
    path, current = parse_access_log(line)
    if current == fmt or path in (None, "off"):
        return line
    if current is None:
        return re.sub(r"(access_log\s+[^\s;]+)", r"\1 " + fmt, line, count=1)
    return re.sub(r"(access_log\s+[^\s;]+\s+)[^\s;]+", r"\1" + fmt, line, count=1)


# ------------------------------------------------------------------------ plan

class FileEdit(object):
    def __init__(self, path):
        self.path = path
        with open(path) as f:
            self.old = f.read()
        self.lines = self.old.splitlines(True)
        self.replace = {}
        self.insert = []   # (after_index, [lines])

    def new_text(self):
        lines = list(self.lines)
        for i, text in self.replace.items():
            lines[i] = text
        for after, block in sorted(self.insert, key=lambda x: x[0], reverse=True):
            lines[after + 1:after + 1] = block
        return "".join(lines)

    def changed(self):
        return self.new_text() != self.old


def build_plan(config, nginx_conf, sites_dir, htpasswd, status_domain=None):
    apps, problems = collect.discover_apps(config)
    plan = {"edits": {}, "logs": {}, "status_url": None, "problems": list(problems), "notes": []}

    def edit_for(path):
        real = os.path.realpath(path)
        if real not in plan["edits"]:
            plan["edits"][real] = FileEdit(real)
        return plan["edits"][real]

    # nginx.conf: the log format, and the http-level default access_log
    main = edit_for(nginx_conf)
    http = find_blocks(main.lines, "http")
    if not http:
        plan["problems"].append("%s: no http { } block" % nginx_conf)
        return plan
    http = http[0]
    default_log, default_lines = None, []
    for i in access_log_lines(main.lines, http):
        path, fmt = parse_access_log(main.lines[i])
        if path != "off":
            default_log = default_log or path
            default_lines.append(i)
    if not re.search(r"^\s*log_format\s+%s\b" % FORMAT, main.old, re.M):
        pad = indent_inside(main.lines, http)
        main.insert.append((http[0], [pad + l + "\n" for l in LOG_FORMAT]))

    sites = []
    for name in sorted(os.listdir(sites_dir)):
        path = os.path.join(sites_dir, name)
        if os.path.isfile(path) and not name.startswith("."):
            sites.append(edit_for(path))
    status_exists = any(re.search(r"location\s*=\s*%s\b" % re.escape(STATUS_PATH), s.old) for s in sites)

    status_target = None
    for app in apps:
        hosts = collect.app_hosts(app)
        matched = [(site, b) for site in sites for b in find_blocks(site.lines, "server")
                   if server_names(site.lines, b) & hosts]
        if not matched:
            plan["problems"].append("%s: no nginx server block for %s" % (app["id"], app["url"]))
            continue
        https = [(s, b) for s, b in matched if is_https(s.lines, b)] or matched
        log = None
        for site, block in matched:
            for i in access_log_lines(site.lines, block):
                path, fmt = parse_access_log(site.lines[i])
                if path == "off":
                    continue
                if fmt not in (None, "combined", FORMAT):
                    plan["notes"].append("%s: %s uses log format '%s'; switched to %s"
                                         % (app["id"], site.path, fmt, FORMAT))
                site.replace[i] = with_format(site.lines[i], FORMAT)
                if (site, block) in https:
                    log = log or path
        if log is None:
            # The app's blocks have no access_log of their own: they write to
            # the http-level default, which then needs the format too (it also
            # applies to every other site without its own access_log).
            for i in default_lines:
                main.replace[i] = with_format(main.lines[i], FORMAT)
        plan["logs"][app["id"]] = log or default_log
        for site, block in https:
            if "location ^~ /internal/" in block_text(site.lines, block):
                continue
            at = next(i for i in range(block[0], block[1] + 1) if code_of(site.lines[i]).strip().startswith("server_name"))
            pad = indent_inside(site.lines, block)
            site.insert.append((at, [pad + l + "\n" for l in INTERNAL]))
        wanted = status_domain is None or status_domain.lower() in hosts
        if status_target is None and wanted and is_https(https[0][0].lines, https[0][1]):
            status_target = (app, https[0])

    if status_exists:
        plan["notes"].append("%s already published; left as is" % STATUS_PATH)
    elif status_target is None:
        plan["problems"].append("no HTTPS block to publish %s%s" % (
            STATUS_PATH, " for %s" % status_domain if status_domain else ""))
    else:
        app, (site, block) = status_target
        at = next(i for i in range(block[0], block[1] + 1) if code_of(site.lines[i]).strip().startswith("server_name"))
        pad = indent_inside(site.lines, block)
        alias = collect.expand(config.get("output", "/var/www/wallmon/status.json"))
        site.insert.append((at, [pad + l + "\n" for l in status_location(alias, htpasswd)]))
        plan["status_url"] = app["url"].rstrip("/") + STATUS_PATH
    if status_exists:
        for site in sites:
            for b in find_blocks(site.lines, "server"):
                if re.search(r"location\s*=\s*%s\b" % re.escape(STATUS_PATH), block_text(site.lines, b)):
                    names = sorted(server_names(site.lines, b))
                    plan["status_url"] = "https://%s%s" % (names[0], STATUS_PATH) if names else None
    return plan


def new_config(config, logs):
    """apps.json pointing at the log file(s) the apps' server blocks use."""
    updated = json.loads(json.dumps(config))
    paths = set(p for p in logs.values() if p)
    if len(paths) == 1:
        updated["access_log"] = paths.pop()
    elif paths:
        entries = {e.get("id"): e for e in updated.get("apps", []) or []}
        for app_id, path in logs.items():
            if path:
                entry = entries.setdefault(app_id, {"id": app_id})
                entry["access_log"] = path
        updated["apps"] = list(entries.values())
    return updated


def show_diff(old, new, name, out):
    out.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True), name, name + " (new)"))


# ----------------------------------------------------------------------- apply

def create_htpasswd(path, user="wallmon"):
    if not sys.stdin.isatty():
        sys.exit("%s does not exist and there is no terminal to ask for its password.\n"
                 "Run this from a real terminal: ssh -t <server> 'sudo python3 ...'" % path)
    password = getpass.getpass("Choose a password for user '%s' (the Pi uses it): " % user)
    if not password or password != getpass.getpass("Repeat it: "):
        sys.exit("passwords empty or different; nothing changed")
    hashed = subprocess.run(["openssl", "passwd", "-apr1", "-stdin"], input=password.encode(),
                            stdout=subprocess.PIPE, check=True).stdout.decode().strip()
    with open(path, "w") as f:
        f.write("%s:%s\n" % (user, hashed))
    try:
        shutil.chown(path, "root", "www-data")
    except (LookupError, PermissionError):
        pass
    os.chmod(path, 0o640)
    print("created %s (user %s)" % (path, user))


def apply_plan(plan, backup_root, test_cmd, reload_cmd):
    changed = [e for e in plan["edits"].values() if e.changed()]
    if not changed:
        print("nginx: nothing to change")
        return 0
    backup = os.path.join(backup_root, "wallmon-backup-%s" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(backup)
    saved = {}
    for n, edit in enumerate(changed):
        copy = os.path.join(backup, "%02d-%s" % (n, os.path.basename(edit.path)))
        shutil.copy2(edit.path, copy)
        saved[edit.path] = copy
    print("backup in %s" % backup)
    for edit in changed:
        with open(edit.path, "w") as f:
            f.write(edit.new_text())
    test = subprocess.run(test_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(test.stdout.decode().strip())
    if test.returncode != 0:
        for path, copy in saved.items():
            shutil.copy2(copy, path)
        print("nginx -t failed: original files restored, nginx not reloaded", file=sys.stderr)
        return 1
    subprocess.run(reload_cmd, check=True)
    print("nginx reloaded")
    return 0


def write_config_keeping_owner(path, config):
    st = os.stat(path)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.chown(path, st.st_uid, st.st_gid)


def use_invoking_users_home():
    """Under sudo, ~ in apps.json (registry, state) means the app user's home."""
    user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and user:
        os.environ["HOME"] = pwd.getpwnam(user).pw_dir


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Prepare nginx for the wall monitor (dry run by default).")
    parser.add_argument("-c", "--config", default=os.path.join(here, "apps.json"))
    parser.add_argument("--nginx-conf", default="/etc/nginx/nginx.conf")
    parser.add_argument("--sites-dir", default="/etc/nginx/sites-enabled")
    parser.add_argument("--htpasswd", default="/etc/nginx/wallmon.htpasswd")
    parser.add_argument("--status-domain", help="domain that publishes status.json (default: first app)")
    parser.add_argument("--apply", action="store_true", help="change the files (needs root)")
    args = parser.parse_args(argv)

    use_invoking_users_home()
    config = collect.load_json(args.config, None)
    if config is None:
        print("cannot read %s" % args.config, file=sys.stderr)
        return 2
    plan = build_plan(config, args.nginx_conf, args.sites_dir, args.htpasswd, args.status_domain)
    for edit in plan["edits"].values():
        if edit.changed():
            show_diff(edit.old, edit.new_text(), edit.path, sys.stdout)
    updated = new_config(config, plan["logs"])
    if updated != config:
        show_diff(json.dumps(config, indent=2) + "\n", json.dumps(updated, indent=2) + "\n", args.config, sys.stdout)

    print("\napps and the log they write to:")
    for app_id, path in plan["logs"].items():
        print("  %-14s %s" % (app_id, path))
    if plan["status_url"]:
        print("status.json for the Pi: %s (user wallmon)" % plan["status_url"])
    for note in plan["notes"]:
        print("note: " + note)
    for problem in plan["problems"]:
        print("problem: " + problem, file=sys.stderr)
    if plan["problems"]:
        print("fix the problems above first; nothing changed", file=sys.stderr)
        return 1

    if not args.apply:
        print("\n(dry run: nothing changed. Apply with: sudo python3 %s --apply)" % os.path.abspath(__file__))
        return 0
    if os.geteuid() != 0:
        print("--apply needs root: sudo python3 %s --apply" % os.path.abspath(__file__), file=sys.stderr)
        return 1
    if plan["status_url"] and not os.path.exists(args.htpasswd):
        create_htpasswd(args.htpasswd)
    code = apply_plan(plan, os.path.dirname(os.path.realpath(args.nginx_conf)),
                      ["nginx", "-t"], ["systemctl", "reload", "nginx"])
    if code == 0 and updated != config:
        write_config_keeping_owner(args.config, updated)
        print("updated %s" % args.config)
    return code


if __name__ == "__main__":
    sys.exit(main())
