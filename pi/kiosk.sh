#!/usr/bin/env bash
# Started from ~/.bash_profile on tty1 (console autologin). Runs Chromium full
# screen inside cage (a minimal Wayland kiosk compositor) and restarts it if it
# ever dies.
SCREEN_ID="${WALLMON_SCREEN:-1}"
URL="http://localhost:8080/?screen=${SCREEN_ID}"
BROWSER="$(command -v chromium-browser || command -v chromium)"

# wait for the local display server
for _ in $(seq 1 30); do curl -fs -o /dev/null http://localhost:8080/ && break; sleep 1; done

while true; do
  cage -s -- "$BROWSER" \
    --kiosk --noerrdialogs --disable-infobars --no-first-run \
    --incognito --disk-cache-size=1 --disable-session-crashed-bubble \
    --disable-features=Translate,MediaRouter --disable-component-update \
    --check-for-update-interval=31536000 --renderer-process-limit=2 \
    --ozone-platform=wayland "$URL"
  sleep 5
done
