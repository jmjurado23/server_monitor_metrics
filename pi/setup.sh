#!/usr/bin/env bash
# Raspberry Pi setup. Run on a fresh Raspberry Pi OS Lite, as user pi:
#   git clone <this repo> ~/server_monitor && cd ~/server_monitor/pi && ./setup.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

echo ">> packages (cage, chromium, wlr-randr)"
sudo apt-get update
sudo apt-get install -y --no-install-recommends cage wlr-randr curl python3 fonts-noto-core
sudo apt-get install -y --no-install-recommends chromium-browser 2>/dev/null \
  || sudo apt-get install -y --no-install-recommends chromium

echo ">> app files in /opt/wallmon"
sudo install -d -o "$USER" /opt/wallmon
cp -r "$here/wallmon.py" "$here/static" "$here/kiosk.sh" "$here/screen.sh" /opt/wallmon/
[ -f /opt/wallmon/dashboard.json ] || cp "$here/dashboard.example.json" /opt/wallmon/dashboard.json
chmod +x /opt/wallmon/*.sh

if [ ! -f /etc/wallmon.env ]; then
  sudo install -m 600 -o "$USER" "$here/wallmon.env.example" /etc/wallmon.env
  echo ">> EDIT /etc/wallmon.env (URL, user, password) before rebooting"
fi

echo ">> systemd service"
sed "s/^User=pi/User=$USER/" "$here/systemd/wallmon.service" | sudo tee /etc/systemd/system/wallmon.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now wallmon

echo ">> console autologin + kiosk on tty1"
sudo raspi-config nonint do_boot_behaviour B2
if ! grep -q "wallmon kiosk" "$HOME/.bash_profile" 2>/dev/null; then
  cat >> "$HOME/.bash_profile" <<'PROFILE'
# wallmon kiosk
if [ "$(tty)" = "/dev/tty1" ]; then exec /opt/wallmon/kiosk.sh; fi
PROFILE
fi

echo ">> cron: screen off 00:00-07:30, daily reboot 07:25 (just before the screen turns on)"
( crontab -l 2>/dev/null | grep -v /opt/wallmon/screen.sh
  echo "0 0 * * * /opt/wallmon/screen.sh off"
  echo "30 7 * * * /opt/wallmon/screen.sh on" ) | crontab -
( sudo crontab -l 2>/dev/null | grep -v "wallmon-reboot"
  echo "25 7 * * * /sbin/shutdown -r now # wallmon-reboot" ) | sudo crontab -

echo ">> done. Edit /etc/wallmon.env and /opt/wallmon/dashboard.json, then: sudo reboot"
