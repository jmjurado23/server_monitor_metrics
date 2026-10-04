#!/usr/bin/env bash
# One-time setup of the collector on the server. Run as the user the apps run as;
# it uses sudo only for the steps that need root, and prints each one first.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$here/apps.json" ]; then
  cp "$here/apps.example.json" "$here/apps.json"
  echo ">> created $here/apps.json (global settings; apps with the gem register themselves)"
fi

echo ">> registry folder ~/.wallmon/apps.d (apps write their entry there when they boot)"
install -d -m 700 "$HOME/.wallmon" "$HOME/.wallmon/apps.d"

if [ ! -s "$HOME/.monitor_metrics_token" ]; then
  echo ">> generating the shared token at ~/.monitor_metrics_token (read by the gem and the collector)"
  umask 077; head -c 32 /dev/urandom | base64 | tr -d '=+/\n' > "$HOME/.monitor_metrics_token"
fi

echo ">> sudo: output directory /var/www/wallmon owned by $USER"
sudo install -d -o "$USER" -g "$USER" -m 755 /var/www/wallmon

if ! id -nG "$USER" | grep -qw adm; then
  echo ">> sudo: adding $USER to group adm so it can read /var/log/nginx (takes effect at next login / cron start)"
  sudo usermod -aG adm "$USER"
fi

python3 "$here/collect.py" -c "$here/apps.json" --stdout > /dev/null && echo ">> test run OK"
echo ">> apps found so far (restart an app with gem >= 0.2 to make it appear):"
python3 "$here/collect.py" -c "$here/apps.json" --list

line="* * * * * /usr/bin/python3 $here/collect.py -c $here/apps.json 2>> \$HOME/.local/state/wallmon/collect.log"
if ! crontab -l 2>/dev/null | grep -qF "$here/collect.py"; then
  (crontab -l 2>/dev/null; echo "$line") | crontab -
  echo ">> cron installed: runs every minute"
fi
echo ">> next: nginx snippet in $here/nginx-wallmon.conf, then: sudo nginx -t && sudo systemctl reload nginx"
